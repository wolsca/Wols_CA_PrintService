import os
import json
import time
import socket
import threading
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import config
import mqtt_service

state_lock = threading.Lock()
personal_choices = {}
pending_choice = {"printer_id": None, "expires": 0.0, "token": None}
personal_options = {}
pending_options = {"copies": None, "print_mode": None, "expires": 0.0, "token": None}

def load_web_strings():
    """Loads the translated strings from the external JSON file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_strings.json")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Web] Warning: Could not load web_strings.json: {e}")
        return {"en": {}}

WEB_STRINGS = load_web_strings()

def web_language():
    language = str(config.get_config().get("web", {}).get("language", "en")).lower()[:2]
    return language if language in WEB_STRINGS else "en"

def public_url():
    """External address of the web app, used as the link in a notification."""
    url = str(config.get_config().get("web", {}).get("public_url") or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url.rstrip("/")

def printer_targets():
    """Builds the list of available target printers."""
    c = config.get_config()
    targets = []
    for entry in c.get("printers", {}).get("targets", []) or []:
        host = entry.get("host")
        if not host: continue
        targets.append({
            "id": str(entry.get("id") or host),
            "name": entry.get("name") or str(entry.get("id") or host),
            "host": host,
            "port": int(entry.get("port", 9100)),
            "duplex": bool(entry.get("duplex", False)),
            "dispatch": str(entry.get("dispatch", "raw")).lower(),
            "cups_queue": entry.get("cups_queue", ""),
            "flip_instruction": entry.get("flip_instruction", "")
        })
    return targets

def default_printer():
    configured = config.get_config().get("printers", {}).get("default")
    targets = printer_targets()
    return next((t for t in targets if t["id"] == configured), targets[0] if targets else None)

def resolve_target_printer():
    with state_lock:
        printer_id = pending_choice["printer_id"]
        expires = pending_choice["expires"]

    if printer_id and time.time() < expires:
        targets = printer_targets()
        target = next((t for t in targets if t["id"] == printer_id), None)
        if target:
            return target, "personal"
    return default_printer(), "default"

def resolve_job_options():
    with state_lock:
        copies = pending_options["copies"]
        print_mode = pending_options["print_mode"]
        expires = pending_options["expires"]
    if time.time() >= expires:
        copies, print_mode = None, None
    return {
        "copies": copies or 1,
        "print_mode": config.normalize_print_mode(
            print_mode or config.get_config()["settings"]["print_mode"])
    }

def consume_pending_options():
    with state_lock:
        pending_choice["printer_id"] = None
        pending_choice["expires"] = 0.0
        pending_options["copies"] = None
        pending_options["print_mode"] = None
        pending_options["expires"] = 0.0

def set_personal_printer(token, printer_id):
    targets = printer_targets()
    target = next((t for t in targets if t["id"] == printer_id), None)
    if not target: return None
    with state_lock:
        personal_choices[token] = printer_id
        pending_choice["printer_id"] = printer_id
        pending_choice["expires"] = time.time() + 900.0
        pending_choice["token"] = token
    return target

# --- HTML TEMPLATES ---
WEB_PAGE = """<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>__TITLE__</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; font: 17px/1.4 -apple-system, sans-serif; background: #f4f5f7; color: #1f2933; }
@media (prefers-color-scheme: dark) { body { background: #14181c; color: #e6e9ec; } .card { background: #1f262c !important; } select { background: #14181c; color: #e6e9ec; } }
h1 { font-size: 20px; margin: 4px 0 16px; }
.card { background: #fff; border-radius: 14px; padding: 16px; margin-bottom: 14px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.state { font-size: 22px; font-weight: 600; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 8px; background: #7b8794; }
.busy .dot { background: #2f80ed; } .wait .dot { background: #f2994a; }
.muted { color: #7b8794; font-size: 14px; }
button { width: 100%; min-height: 56px; font-size: 19px; font-weight: 600; border: 0; border-radius: 12px; background: #2f80ed; color: #fff; margin-top: 10px; }
button.flip { background: #f2994a; min-height: 72px; font-size: 21px; }
button:disabled { background: #c7ccd1; color: #fff; }
button.ghost { background: transparent; color: #2f80ed; border: 1px solid #c7ccd1; min-height: 48px; font-size: 17px; margin-top: 8px; }
button.danger { background: transparent; color: #eb5757; border: 1px solid #eb5757; min-height: 48px; font-size: 17px; margin-top: 8px; }
select { width: 100%; min-height: 48px; font-size: 17px; border-radius: 10px; padding: 8px; border: 1px solid #c7ccd1; margin-bottom: 10px; }
.row { display: flex; justify-content: space-between; padding: 4px 0; }
pre { white-space: pre-wrap; word-break: break-word; font: 13px/1.35 ui-monospace, monospace; max-height: 340px; overflow: auto; margin: 10px 0 0; }
.tag { display: inline-block; border-radius: 8px; padding: 2px 8px; font-size: 14px; font-weight: 600; color: #fff; background: #7b8794; }
.tag.pass { background: #27ae60; } .tag.fail { background: #eb5757; } .tag.warn { background: #f2994a; }
input[type=text], input[type=password], input[type=number] { width: 100%; min-height: 44px; font-size: 16px; border-radius: 10px; padding: 8px; border: 1px solid #c7ccd1; margin-bottom: 10px; background: transparent; color: inherit; }
.field { margin-bottom: 6px; } .field label { display: block; font-size: 14px; color: #7b8794; margin-bottom: 2px; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="card" id="statusCard">
  <div class="state"><span class="dot"></span><span id="state">...</span></div>
  <p id="detail" class="muted"></p>
  <div class="row"><span class="muted" id="lblFile"></span><span id="file">-</span></div>
  <div class="row"><span class="muted" id="lblPages"></span><span id="pages">-</span></div>
  <div class="row"><span class="muted" id="lblPrinter"></span><span id="printer">-</span></div>
</div>
<div class="card" id="flipCard" hidden>
  <div id="flipHelp" class="muted"></div>
  <button class="flip" id="resume" disabled></button>
  <button class="ghost" id="reprint"></button>
  <button class="danger" id="cancel"></button>
</div>
<div class="card" id="testCard">
  <div class="row"><span id="lblTest" class="state" style="font-size:18px"></span><span class="tag" id="testResult">-</span></div>
  <p id="testSummary" class="muted"></p>
  <button class="ghost" id="runTest"></button>
  <button class="ghost" id="runChainTest"></button>
  <button class="ghost" id="toggleTest"></button>
  <pre id="testReport" hidden></pre>
</div>
<div class="card" id="updateCard">
  <div class="row"><span id="lblUpdate" class="state" style="font-size:18px"></span><span class="tag" id="updateTag">-</span></div>
  <div class="row"><span class="muted" id="lblInstalled"></span><span id="installedVersion">-</span></div>
  <div class="row"><span class="muted" id="lblLatest"></span><span id="latestVersion">-</span></div>
  <p id="updateDetail" class="muted"></p>
  <button class="ghost" id="checkUpdate"></button>
  <button id="installUpdate" hidden></button>
  <button class="ghost" id="autoUpdate"></button>
  <p id="testBuildDetail" class="muted"></p>
  <button class="ghost" id="checkTestBuild"></button>
  <button class="ghost" id="installTestBuild"></button>
</div>
<div class="card" id="adminCard">
  <div class="row"><span id="lblAdmin" class="state" style="font-size:18px"></span><span class="tag" id="adminTag">-</span></div>
  <div id="adminLock">
    <p class="muted" id="adminHelp"></p>
    <input type="password" id="adminToken" autocomplete="current-password">
    <button class="ghost" id="adminUnlock"></button>
  </div>
  <div id="adminForm" hidden>
    <div id="adminFields"></div>
    <p id="adminDetail" class="muted"></p>
    <button id="adminSave"></button>
    <button class="ghost" id="adminRestart"></button>
    <button class="ghost" id="adminReload"></button>
    <button class="danger" id="adminLockAgain"></button>
  </div>
</div>
<script>
var T = __STRINGS__;
var byId = function(id) { return document.getElementById(id); };
byId("lblFile").textContent = T.document || "Document";
byId("lblPages").textContent = T.pagesSheets || "Pages";
byId("lblPrinter").textContent = T.printingOn || "Printer";
byId("resume").textContent = T.continueButton || "CONTINUE";
byId("reprint").textContent = T.reprintButton || "Reprint";
byId("cancel").textContent = T.cancelButton || "Cancel";
byId("lblTest").textContent = T.selfTestTitle || "Self-test";
byId("runTest").textContent = T.selfTestRun || "Run self-test";
byId("runChainTest").textContent = T.selfTestRunChain || "Run self-test incl. test print";
byId("toggleTest").textContent = T.selfTestShow || "Show report";
byId("lblUpdate").textContent = T.updateTitle || "Version and updates";
byId("lblInstalled").textContent = T.updateInstalled || "Installed version";
byId("lblLatest").textContent = T.updateLatest || "Latest version";
byId("checkUpdate").textContent = T.updateCheck || "Check for update";
byId("checkTestBuild").textContent = T.updateCheckTest || "Check for test build";
byId("installTestBuild").textContent = T.updateInstallTest || "Install test build";
byId("lblAdmin").textContent = T.adminTitle || "Administrator";
byId("adminHelp").textContent = T.adminHelp || "Enter the administrator token to edit the configuration.";
byId("adminUnlock").textContent = T.adminUnlock || "Unlock";
byId("adminSave").textContent = T.adminSave || "Save configuration";
byId("adminRestart").textContent = T.adminRestart || "Restart service";
byId("adminReload").textContent = T.adminReload || "Discard changes";
byId("adminLockAgain").textContent = T.adminLock || "Lock";

function render(s) {
  var st = s.state;
  byId("state").textContent = (T.states && T.states[st]) ? T.states[st] : st;
  byId("statusCard").className = "card " + (["PROCESSING","PRINTING"].includes(st) ? "busy" : (st === "WAITING_FOR_FLIP" ? "wait" : "idle"));
  byId("detail").textContent = s.detail || "";
  byId("file").textContent = s.filename || "-";
  byId("pages").textContent = s.pages ? s.pages + " p / " + s.sheets + " s" : "-";
  byId("printer").textContent = s.effective_printer_name || "-";
  // Exactly one place confirms the flip: when the printer asks on its own
  // panel, the buttons here are taken away instead of competing with it.
  var printerFlip = s.flip_owner === "printer";
  byId("resume").hidden = printerFlip;
  byId("resume").disabled = !s.waiting_for_flip;
  byId("flipCard").hidden = !(s.waiting_for_flip || s.busy);
  byId("reprint").hidden = printerFlip || !s.waiting_for_flip;
  byId("cancel").hidden = !s.busy;
  byId("flipHelp").textContent = printerFlip
      ? (s.waiting_for_flip ? (T.flipOnPrinter || "Put the sheets back in the tray and press the button on the printer.") : (T.flipByPrinter || "This printer asks for the flip on its own display."))
      : (s.waiting_for_flip ? (s.flip_instruction || "") : "");
}

function poll() {
  fetch("/api/status", {cache: "no-store"}).then(function(r) { return r.json(); }).then(render).catch(function(){});
}

function post(url) {
  fetch(url, {method: "POST"}).then(poll);
}

function renderTest(r) {
  var tag = byId("testResult");
  var result = r.running ? "RUNNING" : (r.result || "NONE");
  tag.textContent = result;
  tag.className = "tag " + (result === "PASS" ? "pass" : (result === "FAIL" ? "fail" : (result === "WARN" ? "warn" : "")));
  byId("testSummary").textContent = r.running ? (T.selfTestRunning || "Running...")
                                              : (r.summary ? r.summary + " - " + (r.finished || "") : (T.selfTestNever || "Not run yet"));
  byId("testReport").textContent = r.markdown || "";
  byId("runTest").disabled = !!r.running;
  byId("runChainTest").disabled = !!r.running;
}

function pollTest() {
  fetch("/api/diagnostics", {cache: "no-store"}).then(function(r) { return r.json(); }).then(renderTest).catch(function(){});
}

byId("runTest").onclick = function() {
  post("/api/diagnostics/run");
  setTimeout(pollTest, 500);
};
byId("runChainTest").onclick = function() {
  if (!confirm(T.selfTestConfirmChain || "This sends a test page to the printer. Continue?")) return;
  post("/api/diagnostics/run?phases=system,config,admin,permissions,update,cups,printer,network,chain");
  setTimeout(pollTest, 500);
};
byId("toggleTest").onclick = function() {
  var pre = byId("testReport");
  pre.hidden = !pre.hidden;
  byId("toggleTest").textContent = pre.hidden ? (T.selfTestShow || "Show report") : (T.selfTestHide || "Hide report");
};

function renderUpdate(u) {
  var tag = byId("updateTag");
  var label = u.installing ? (T.updateInstalling || "Updating...")
            : (u.checking ? (T.updateChecking || "Checking...")
            : (u.update_available ? (T.updateAvailable || "Update available")
                                  : (T.updateUpToDate || "Up to date")));
  tag.textContent = label;
  tag.className = "tag " + (u.update_available ? "warn" : (u.checking || u.installing ? "" : "pass"));
  byId("installedVersion").textContent = u.installed_version || "-";
  byId("latestVersion").textContent = u.latest_version || "-";
  byId("updateDetail").textContent = (u.last_result || "") + (u.checked ? " (" + u.checked + ")" : "");
  byId("checkUpdate").disabled = !!(u.checking || u.installing);
  byId("installUpdate").hidden = !u.update_available;
  byId("installUpdate").disabled = !!u.installing;
  byId("installUpdate").textContent = (T.updateInstall || "Update now") + " (" + (u.latest_version || "") + ")";
  byId("autoUpdate").textContent = (T.updateAuto || "Automatic updates") + ": "
      + (u.auto_update ? (T.on || "on") : (T.off || "off"));
  byId("testBuildDetail").textContent = u.test_result || (T.updateTestHint
      || "Test builds come from the branch and are only installed on request.");
  byId("installTestBuild").hidden = !u.test_version;
  byId("installTestBuild").disabled = !!u.installing;
  byId("checkTestBuild").disabled = !!(u.checking || u.installing);
}

function pollUpdate() {
  fetch("/api/update", {cache: "no-store"}).then(function(r) { return r.json(); }).then(renderUpdate).catch(function(){});
}

byId("checkUpdate").onclick = function() {
  post("/api/update/check");
  setTimeout(pollUpdate, 500);
};
byId("installUpdate").onclick = function() {
  if (!confirm(T.updateConfirm || "Install the new version now? The service restarts.")) return;
  post("/api/update/install");
  setTimeout(pollUpdate, 500);
};
byId("autoUpdate").onclick = function() {
  post("/api/update/auto");
  setTimeout(pollUpdate, 500);
};
byId("checkTestBuild").onclick = function() {
  post("/api/update/check-test");
  setTimeout(pollUpdate, 1500);
};
byId("installTestBuild").onclick = function() {
  if (!confirm(T.updateConfirmTest || "Install the untested branch build? The service restarts.")) return;
  post("/api/update/install-test");
  setTimeout(pollUpdate, 1500);
};

/* --- administrator ------------------------------------------------- */
var adminToken = "";
var adminFields = [];

function adminUrl(path) {
  return path + (path.indexOf("?") < 0 ? "?" : "&") + "token=" + encodeURIComponent(adminToken);
}

function fieldInput(f) {
  var id = "cfg_" + f.key.replace(/\\./g, "_");
  var html = '<div class="field"><label for="' + id + '">' + f.label + "</label>";
  if (f.type === "bool") {
    html += '<select id="' + id + '"><option value="true"' + (f.value ? " selected" : "") + ">"
         + (T.on || "on") + '</option><option value="false"' + (f.value ? "" : " selected") + ">"
         + (T.off || "off") + "</option></select>";
  } else if (f.type === "select") {
    html += '<select id="' + id + '">';
    f.options.forEach(function(o) {
      html += '<option value="' + o + '"' + (String(f.value) === o ? " selected" : "") + ">" + o + "</option>";
    });
    html += "</select>";
  } else if (f.type === "number") {
    html += '<input type="number" id="' + id + '" value="' + (f.value === null ? "" : f.value) + '">';
  } else {
    html += '<input type="text" id="' + id + '" value="' + String(f.value === null ? "" : f.value).replace(/"/g, "&quot;") + '">';
  }
  return html + "</div>";
}

function renderAdmin(a) {
  adminFields = a.fields || [];
  byId("adminFields").innerHTML = adminFields.map(fieldInput).join("");
  var tag = byId("adminTag");
  tag.textContent = a.restart_required ? (T.adminRestartNeeded || "Restart required")
                                       : (T.adminSaved || "Saved");
  tag.className = "tag " + (a.restart_required ? "warn" : "pass");
  byId("adminDetail").textContent = (a.last_result || "") + " " + (a.config_path || "");
  byId("adminLock").hidden = true;
  byId("adminForm").hidden = false;
}

function collectAdminValues() {
  var values = {};
  adminFields.forEach(function(f) {
    var el = byId("cfg_" + f.key.replace(/\\./g, "_"));
    if (el) values[f.key] = el.value;
  });
  return values;
}

function askRestart(result) {
  byId("adminDetail").textContent = (result.errors && result.errors.length)
      ? result.errors.join("; ") : (T.adminSavedOk || "Configuration saved.");
  if (result.saved && confirm(T.adminAskRestart || "Configuration saved. Restart the service now?")) {
    fetch(adminUrl("/api/admin/restart"), {method: "POST"});
    byId("adminDetail").textContent = T.adminRestarting || "Restarting the service...";
  }
}

byId("adminUnlock").onclick = function() {
  adminToken = byId("adminToken").value;
  fetch(adminUrl("/api/admin/config"), {cache: "no-store"}).then(function(r) {
    if (!r.ok) throw new Error("denied");
    return r.json();
  }).then(renderAdmin).catch(function() {
    byId("adminTag").textContent = T.adminDenied || "Access denied";
    byId("adminTag").className = "tag fail";
  });
};
byId("adminSave").onclick = function() {
  fetch(adminUrl("/api/admin/config"), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({values: collectAdminValues()})
  }).then(function(r) { return r.json(); }).then(askRestart).catch(function(){});
};
byId("adminRestart").onclick = function() {
  if (!confirm(T.adminConfirmRestart || "Restart the service now?")) return;
  fetch(adminUrl("/api/admin/restart"), {method: "POST"});
  byId("adminDetail").textContent = T.adminRestarting || "Restarting the service...";
};
byId("adminReload").onclick = function() {
  fetch(adminUrl("/api/admin/reload"), {method: "POST"})
    .then(function(r) { return r.json(); }).then(renderAdmin).catch(function(){});
};
byId("adminLockAgain").onclick = function() {
  adminToken = "";
  byId("adminToken").value = "";
  byId("adminForm").hidden = true;
  byId("adminLock").hidden = false;
  byId("adminTag").textContent = "-";
  byId("adminTag").className = "tag";
};

byId("resume").onclick = function() { post("/api/resume"); };
byId("reprint").onclick = function() { post("/api/reprint"); };
byId("cancel").onclick = function() { if(confirm(T.confirmCancel || "Cancel?")) post("/api/cancel"); };

poll();
setInterval(poll, 2000);
pollTest();
setInterval(pollTest, 4000);
pollUpdate();
setInterval(pollUpdate, 10000);
</script>
</body>
</html>
"""

def render_web_page():
    language = web_language()
    strings_json = json.dumps(WEB_STRINGS.get(language, WEB_STRINGS.get("en", {})))
    title = config.get_config().get("web", {}).get("title", "Wols CA Booklet Printer")
    return WEB_PAGE.replace("__TITLE__", title).replace("__LANG__", language).replace("__STRINGS__", strings_json)

class WebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def query_value(self, name):
        return parse_qs(urlparse(self.path).query).get(name, [""])[0]

    def request_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def admin_authorised(self, body=None):
        """The admin token from the query string or the JSON body."""
        import admin
        token = self.query_value("token") or (body or {}).get("token") \
            or self.headers.get("X-Admin-Token", "")
        if admin.token_valid(token):
            return True
        print("[Web] Administrator access denied.")
        self.send_json({"error": "unauthorised"}, 403)
        return False

    def client_token(self):
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "wolsca_client" and value:
                return value, False
        return secrets.token_hex(8), True

    def do_GET(self):
        token, is_new = self.client_token()
        path = urlparse(self.path).path

        if path == "/":
            data = render_web_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if is_new:
                self.send_header("Set-Cookie", f"wolsca_client={token}; Path=/; Max-Age=31536000")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/status":
            with mqtt_service.state_lock:
                snapshot = dict(mqtt_service.job_state)
            eff_target, _ = resolve_target_printer()
            payload = {
                "state": snapshot["state"],
                "detail": snapshot["detail"],
                "filename": snapshot["filename"],
                "pages": snapshot["pages"],
                "sheets": snapshot["sheets"],
                "waiting_for_flip": snapshot["waiting_for_flip"],
                "flip_owner": snapshot["flip_owner"],
                "busy": snapshot["state"] in ("PROCESSING", "PRINTING", "WAITING_FOR_FLIP"),
                "effective_printer_name": snapshot["printer_name"] or eff_target["name"],
                "flip_instruction": snapshot["flip_instruction"]
            }
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/update":
            import updater
            payload = dict(updater.state)
            payload["auto_update"] = bool(updater.update_config().get("auto_update"))
            payload.pop("release_notes", None)   # keep the payload small for the browser
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/admin/config":
            import admin
            if not self.admin_authorised():
                return
            self.send_json(admin.fields_payload())
        elif path == "/api/diagnostics":
            import diagnostics
            report = dict(diagnostics.last_report or {"result": "NONE"})
            report.pop("steps", None)          # keep the payload small for the browser
            report["running"] = diagnostics.run_lock.locked()
            data = json.dumps(report).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/resume":
            with mqtt_service.state_lock:
                owner = mqtt_service.job_state["flip_owner"]
            if owner == "printer":
                print("[Web] 'CONTINUE' ignored: the flip is confirmed on the printer itself.")
            elif mqtt_service.waiting_for_user_action:
                print("[Web] 'CONTINUE' pressed in the web app.")
                mqtt_service.waiting_for_user_action = False
        elif path == "/api/cancel":
            mqtt_service.request_cancel()
        elif path == "/api/reprint":
            mqtt_service.request_reprint_front()
        elif path == "/api/diagnostics/run":
            import diagnostics
            phases = parse_qs(urlparse(self.path).query).get("phases", [""])[0]
            selected = [p.strip() for p in phases.split(",") if p.strip()] or None
            print(f"[Web] Self-test requested ({selected or 'default phases'}).")
            diagnostics.run_async(selected)
        elif path == "/api/update/check":
            import updater
            print("[Web] Update check requested.")
            updater.check_async()
        elif path == "/api/update/install":
            import updater
            print("[Web] Update installation requested.")
            updater.install_async()
        elif path == "/api/update/check-test":
            import updater
            print("[Web] Test build check requested.")
            updater.check_test_build_async()
        elif path == "/api/update/install-test":
            import updater
            print("[Web] Test build installation requested.")
            updater.install_test_build_async()
        elif path == "/api/admin/config":
            import admin
            body = self.request_body()
            if not self.admin_authorised(body):
                return
            result = admin.apply_values(body.get("values") or {})
            self.send_json(result)
            return
        elif path == "/api/admin/restart":
            import admin
            if not self.admin_authorised(self.request_body()):
                return
            print("[Web] Service restart requested by the administrator.")
            admin.restart_async()
            self.send_json({"restarting": True})
            return
        elif path == "/api/admin/reload":
            import admin
            if not self.admin_authorised(self.request_body()):
                return
            self.send_json(admin.reload_config())
            return
        elif path == "/api/update/auto":
            import updater
            requested = parse_qs(urlparse(self.path).query).get("enabled", [""])[0].lower()
            if requested in ("1", "true", "on", "yes"):
                enabled = True
            elif requested in ("0", "false", "off", "no"):
                enabled = False
            else:
                enabled = not bool(updater.update_config().get("auto_update"))
            updater.set_auto_update(enabled)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{}')

def start_web_app():
    web_config = config.get_config().get("web", {})
    if not web_config.get("enabled", True):
        return None
    address = web_config.get("bind_address", "0.0.0.0")
    port = int(web_config.get("port", 8080))
    try:
        httpd = ThreadingHTTPServer((address, port), WebAppHandler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"[Web] Web app active on http://{socket.gethostname()}.local:{port}/")
        return httpd
    except OSError as e:
        print(f"[Error] Could not start web app: {e}")
        return None