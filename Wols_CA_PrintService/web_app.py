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
        "print_mode": print_mode or config.get_config()["settings"]["print_mode"]
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
<script>
var T = __STRINGS__;
var byId = function(id) { return document.getElementById(id); };
byId("lblFile").textContent = T.document || "Document";
byId("lblPages").textContent = T.pagesSheets || "Pages";
byId("lblPrinter").textContent = T.printingOn || "Printer";
byId("resume").textContent = T.continueButton || "CONTINUE";
byId("reprint").textContent = T.reprintButton || "Reprint";
byId("cancel").textContent = T.cancelButton || "Cancel";

function render(s) {
  var st = s.state;
  byId("state").textContent = (T.states && T.states[st]) ? T.states[st] : st;
  byId("statusCard").className = "card " + (["PROCESSING","PRINTING"].includes(st) ? "busy" : (st === "WAITING_FOR_FLIP" ? "wait" : "idle"));
  byId("detail").textContent = s.detail || "";
  byId("file").textContent = s.filename || "-";
  byId("pages").textContent = s.pages ? s.pages + " p / " + s.sheets + " s" : "-";
  byId("printer").textContent = s.effective_printer_name || "-";
  byId("resume").disabled = !s.waiting_for_flip;
  byId("flipCard").hidden = !(s.waiting_for_flip || s.busy);
  byId("reprint").hidden = !s.waiting_for_flip;
  byId("cancel").hidden = !s.busy;
  byId("flipHelp").textContent = s.waiting_for_flip ? (s.flip_instruction || "") : "";
}

function poll() {
  fetch("/api/status", {cache: "no-store"}).then(function(r) { return r.json(); }).then(render).catch(function(){});
}

function post(url) {
  fetch(url, {method: "POST"}).then(poll);
}

byId("resume").onclick = function() { post("/api/resume"); };
byId("reprint").onclick = function() { post("/api/reprint"); };
byId("cancel").onclick = function() { if(confirm(T.confirmCancel || "Cancel?")) post("/api/cancel"); };

poll();
setInterval(poll, 2000);
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
                "busy": snapshot["state"] in ("PROCESSING", "PRINTING", "WAITING_FOR_FLIP"),
                "effective_printer_name": snapshot["printer_name"] or eff_target["name"],
                "flip_instruction": snapshot["flip_instruction"]
            }
            data = json.dumps(payload).encode("utf-8")
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
            if mqtt_service.waiting_for_user_action:
                print("[Web] 'CONTINUE' pressed in the web app.")
                mqtt_service.waiting_for_user_action = False
        elif path == "/api/cancel":
            mqtt_service.request_cancel()
        elif path == "/api/reprint":
            mqtt_service.request_reprint_front()

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