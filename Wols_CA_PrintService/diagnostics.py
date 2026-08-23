"""Self-test / diagnostics module.

Runs a series of test phases, each consisting of shell commands and checks.
Every single command and its result is published to MQTT, and the whole run is
aggregated into one retained report that Home Assistant renders as a sensor
with a markdown attribute.

Usage:
    python main.py --self-test                  # all phases
    python main.py --self-test cups,printer     # selected phases
    MQTT: publish "SELFTEST" or "SELFTEST:cups,printer" to <prefix>/command (HA button)
    HTTP: POST /api/diagnostics/run , GET /api/diagnostics
"""

import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

import config
import mqtt_service

MAX_OUTPUT_CHARS = 1500          # per step, in the MQTT payload
MAX_REPORT_CHARS = 12000         # HA attributes are limited in size
DEFAULT_TIMEOUT = 30

run_lock = threading.Lock()
last_report = {}


def topic(suffix):
    return f"{mqtt_service.PREFIX}/diagnostics/{suffix}"


def truncate(text, limit=MAX_OUTPUT_CHARS):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


class Diagnostics:
    """Collects and publishes the results of one diagnostics run."""

    def __init__(self, publish=True):
        self.publish = publish
        self.steps = []
        self.phase = "general"
        self.started = time.time()

    # --- primitives -----------------------------------------------------
    def _record(self, title, status, command="", output="", detail="", duration_ms=0):
        step = {
            "index": len(self.steps) + 1,
            "phase": self.phase,
            "title": title,
            "command": command,
            "status": status,
            "detail": detail,
            "output": truncate(output),
            "duration_ms": duration_ms,
            "timestamp": datetime.now().isoformat(timespec="seconds")
        }
        self.steps.append(step)

        marker = {"PASS": "OK  ", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP", "INFO": "INFO"}.get(status, status)
        print(f"[Test] [{marker}] {self.phase}: {title}" + (f" ({detail})" if detail else ""))
        if command:
            print(f"       $ {command}")
        if step["output"]:
            for line in step["output"].splitlines()[:20]:
                print(f"       | {line}")

        if self.publish:
            try:
                mqtt_service.mqtt_client.publish(topic("step"), json.dumps(step))
            except Exception as e:
                print(f"[Error] Could not publish the diagnostics step: {e}")
        return step

    def command(self, argv, title=None, timeout=DEFAULT_TIMEOUT, ok_codes=(0,),
                expect=None, optional=False):
        """Runs a command and logs the command line together with its output."""
        printable = argv if isinstance(argv, str) else " ".join(argv)
        title = title or printable
        started = time.time()

        binary = (argv.split()[0] if isinstance(argv, str) else argv[0])
        if not isinstance(argv, str) and not shutil.which(binary):
            return self._record(title, "SKIP", printable, "", f"'{binary}' is not installed")

        try:
            completed = subprocess.run(argv, shell=isinstance(argv, str), timeout=timeout,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            output = completed.stdout.decode("utf-8", "replace")
            code = completed.returncode
        except subprocess.TimeoutExpired:
            return self._record(title, "FAIL", printable, "", f"timed out after {timeout}s",
                                int((time.time() - started) * 1000))
        except Exception as e:
            return self._record(title, "FAIL", printable, "", str(e),
                                int((time.time() - started) * 1000))

        duration = int((time.time() - started) * 1000)
        status = "PASS" if code in ok_codes else ("WARN" if optional else "FAIL")
        detail = f"exit code {code}"
        if status == "PASS" and expect and expect not in output:
            status = "WARN" if optional else "FAIL"
            detail = f"expected '{expect}' in the output"
        return self._record(title, status, printable, output, detail, duration)

    def check(self, title, ok, detail="", output="", optional=False):
        status = "PASS" if ok else ("WARN" if optional else "FAIL")
        return self._record(title, status, "", output, detail)

    def info(self, title, output="", detail=""):
        return self._record(title, "INFO", "", output, detail)

    # --- aggregation ----------------------------------------------------
    def counts(self):
        result = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0, "INFO": 0}
        for step in self.steps:
            result[step["status"]] = result.get(step["status"], 0) + 1
        return result

    def markdown(self):
        buffer = io.StringIO()
        counts = self.counts()
        buffer.write(f"## Wols CA Print Service self-test\n")
        buffer.write(f"{datetime.now().isoformat(timespec='seconds')} on {socket.gethostname()}\n\n")
        buffer.write(f"**{counts['PASS']} passed, {counts['FAIL']} failed, "
                     f"{counts['WARN']} warnings, {counts['SKIP']} skipped**\n")
        current = None
        for step in self.steps:
            if step["phase"] != current:
                current = step["phase"]
                buffer.write(f"\n### {current}\n")
            icon = {"PASS": "OK", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP", "INFO": "i"}[step["status"]]
            buffer.write(f"- **{icon}** {step['title']}")
            if step["detail"]:
                buffer.write(f" - {step['detail']}")
            buffer.write("\n")
            if step["command"]:
                buffer.write(f"  `{step['command']}`\n")
            if step["status"] in ("FAIL", "WARN") and step["output"]:
                snippet = "\n".join(step["output"].splitlines()[:8])
                buffer.write(f"  ```\n{snippet}\n  ```\n")
        return truncate(buffer.getvalue(), MAX_REPORT_CHARS)

    def report(self, phases):
        counts = self.counts()
        failed = [f"{s['phase']}: {s['title']}" for s in self.steps if s["status"] == "FAIL"]
        return {
            "result": "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS"),
            "host": socket.gethostname(),
            "version": mqtt_service.SERVICE_VERSION,
            "phases": phases,
            "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "duration_s": round(time.time() - self.started, 1),
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "warnings": counts["WARN"],
            "skipped": counts["SKIP"],
            "failed_steps": failed,
            "summary": f"{counts['PASS']} passed / {counts['FAIL']} failed / {counts['WARN']} warnings",
            "markdown": self.markdown(),
            "steps": self.steps
        }


# ----------------------------------------------------------------------
# Test phases - one function per phase of the print chain
# ----------------------------------------------------------------------

def phase_system(d):
    """Host, OS and service unit state."""
    import version
    d.info("Service version", json.dumps(version.version_info(), indent=2),
           version.FULL_VERSION)
    d.command(["uname", "-a"], "Kernel and architecture")
    d.command(["cat", "/etc/os-release"], "Debian release")
    d.command(["systemctl", "is-active", "wolsca-print-service"],
              "Print service unit active", expect="active", optional=True)
    d.command(["systemctl", "is-active", "cups"], "CUPS unit active", expect="active")
    d.command(["systemctl", "is-active", "avahi-daemon"], "Avahi unit active",
              expect="active", optional=True)
    d.command(["df", "-h", "/var/spool"], "Free space on /var/spool")
    d.command(["id", "-a"], "Effective user of this process")


def phase_config(d):
    """Configuration file and the derived paths."""
    c = config.get_config()
    d.check("Configuration file present", os.path.exists(config.CONFIG_PATH),
            config.CONFIG_PATH)
    for directory in (config.DROP_DIR, config.TEMP_DIR, config.ERROR_DIR):
        d.check(f"Directory {directory}", os.path.isdir(directory),
                "writable" if os.access(directory, os.W_OK) else "NOT writable")
    d.command(["ls", "-laR", config.DROP_DIR], "Contents of the drop directory")

    target_id = c.get("printers", {}).get("default")
    targets = c.get("printers", {}).get("targets", [])
    target = next((t for t in targets if t.get("id") == target_id), targets[0] if targets else {})
    d.check("Default printer target resolved", bool(target), target.get("id", "none"),
            json.dumps(target, indent=2))
    d.check("Target dispatches through CUPS", target.get("dispatch") == "cups",
            f"dispatch={target.get('dispatch')}, cups_queue={target.get('cups_queue')}",
            optional=True)
    d.info("Intake queues", json.dumps(c.get("intake", {}).get("queues", []), indent=2))

    mqtt_user, mqtt_password = config.get_mqtt_credentials()
    d.check("MQTT broker account configured", bool(mqtt_user and mqtt_password),
            f"mqtt.user={mqtt_user or 'empty'} (create this account on the broker)")
    mode = c.get("settings", {}).get("print_mode")
    d.check("Default print mode known", mode in config.PRINT_MODES,
            f"settings.print_mode={mode}, expected one of {', '.join(config.PRINT_MODES)}")


def phase_permissions(d):
    """Ownership and access rights of every location the service touches."""
    c = config.get_config()

    def describe(path):
        try:
            info = os.stat(path)
        except OSError as e:
            return str(e)
        try:
            import grp
            import pwd
            owner = pwd.getpwuid(info.st_uid).pw_name
            group = grp.getgrgid(info.st_gid).gr_name
        except Exception:
            owner, group = str(info.st_uid), str(info.st_gid)
        return f"{oct(info.st_mode & 0o7777)} {owner}:{group}"

    d.command(["id", "-a"], "Effective user of this process")

    config_dir = os.path.dirname(config.CONFIG_PATH)
    d.check(f"Configuration directory {config_dir} traversable",
            os.path.isdir(config_dir) and os.access(config_dir, os.X_OK | os.R_OK),
            describe(config_dir))
    d.check(f"Configuration file {config.CONFIG_PATH} readable",
            os.access(config.CONFIG_PATH, os.R_OK), describe(config.CONFIG_PATH))
    d.check("Configuration file writable (the installer rewrites it)",
            os.access(config.CONFIG_PATH, os.W_OK), describe(config.CONFIG_PATH),
            optional=True)

    directories = [config.DROP_DIR, config.TEMP_DIR, config.ERROR_DIR]
    directories += [q.get("directory") for q in c.get("intake", {}).get("queues", [])
                    if q.get("directory")]
    for directory in directories:
        ok = os.path.isdir(directory) and os.access(directory, os.R_OK | os.W_OK | os.X_OK)
        d.check(f"Spool directory {directory} readable and writable", ok, describe(directory))

    history = c.get("history", {}).get("file")
    if history:
        parent = os.path.dirname(history) or "."
        d.check(f"History file location {parent} writable",
                os.access(history if os.path.exists(history) else parent, os.W_OK),
                describe(history if os.path.exists(history) else parent))

    d.command(["ls", "-ld"] + [p for p in directories if p], "Modes of the spool directories",
              optional=True)
    d.info("Remedy", "sudo /opt/wolsca-print-service/fix-permissions.sh",
           "run this if any check above failed")


def phase_update(d):
    """Version files and the availability of a newer release."""
    import updater
    import version

    d.info("Installed version", json.dumps(version.version_info(), indent=2),
           version.FULL_VERSION)
    section = updater.update_config()
    d.info("Update configuration", json.dumps(section, indent=2),
           f"{section.get('repository')} ({section.get('channel')})")

    d.check("Only releases trigger an update",
            str(section.get("channel", "release")).lower() == "release",
            "commit builds must be requested with the test build button", optional=True)

    result = updater.check(publish=False)
    d.check("Update check succeeded", bool(result.get("check_ok")), result["last_result"])
    d.check("Running the latest release", not result["update_available"],
            f"installed {result['installed_version']}, release {result['latest_version']}"
            + (f" ({result['release_tag']})" if result.get("release_tag") else ""),
            optional=True)

    source = section.get("source_directory")
    d.check(f"Source checkout {source} present",
            os.path.isdir(os.path.join(source or "", ".git")),
            "needed for the update button", optional=True)


def phase_admin(d):
    """The administrator configuration editor and its restart state."""
    import admin

    d.check("Administrator token configured", bool(admin.admin_token()),
            "web.admin_token unlocks the configuration editor in the web app",
            optional=True)
    d.info("Editable settings", "\n".join(f"{f['key']} = {admin.get_value(f['key'])}"
                                          for f in admin.FIELDS),
           f"{len(admin.FIELDS)} settings")
    d.check("No restart pending", not admin.state["restart_required"],
            admin.state["last_result"] or "no configuration change waiting", optional=True)
    d.check(f"Configuration file {config.CONFIG_PATH} writable",
            os.access(config.CONFIG_PATH, os.W_OK),
            "needed to save changes from the web app or Home Assistant")


def phase_cups(d):
    """CUPS queues, sharing and cups-pdf configuration."""
    d.command(["lpstat", "-t"], "Full CUPS status")
    d.command(["lpstat", "-d"], "CUPS default destination")
    d.command(["lpstat", "-o"], "Jobs still queued")
    d.command("grep -E '^(Listen|Browsing|BrowseLocalProtocols|DefaultShared)' /etc/cups/cupsd.conf",
              "Sharing directives in cupsd.conf")
    d.command("ls -1 /etc/cups/cups-pdf*.conf", "cups-pdf instance configurations")
    d.command("grep -hE '^(Out|AnonDirName|AnonUser|Grp|UserUMask)' /etc/cups/cups-pdf*.conf",
              "cups-pdf output directories")
    d.command("tail -n 30 /var/log/cups/cups-pdf_log", "Last cups-pdf log lines", optional=True)
    d.command("tail -n 40 /var/log/cups/error_log", "Last CUPS error_log lines", optional=True)

    existing = ""
    try:
        existing = subprocess.run(["lpstat", "-p"], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=15).stdout.decode("utf-8", "replace")
    except Exception:
        pass
    c = config.get_config()
    for q_entry in c.get("intake", {}).get("queues", []):
        name = q_entry.get("cups_queue")
        if name:
            d.check(f"Intake queue '{name}' exists", name in existing, q_entry.get("print_mode", ""))
    output_queue = c.get("hardware", {}).get("cups_queue_name") or "WolsCA_Output"
    d.check(f"Output queue '{output_queue}' exists", output_queue in existing)
    d.check("Stray 'PDF' queue removed", "printer PDF " not in existing,
            "the stock cups-pdf queue writes into an unwatched folder", optional=True)


def plain_ipp_uri(uri):
    """The unencrypted address of the same printer, used as a retry.

    Many printers present a self signed certificate or only an old TLS version,
    so 'ipps://host:443/ipp/print' fails while 'ipp://host:631/ipp/print'
    answers. Returns None when there is nothing to fall back to.
    """
    if not str(uri).startswith("ipps://"):
        return None
    authority, _, path = uri[len("ipps://"):].partition("/")
    host = authority.split("@")[-1]
    if host.startswith("["):                      # IPv6 literal
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return f"ipp://{host}:631/{path}"


def run_ipptool(uri, request, timeout=45):
    """Runs one Get-Printer-Attributes request. Returns (exit code, output)."""
    argv = ["ipptool", "-T", "10", "-t", uri, request]
    try:
        completed = subprocess.run(argv, timeout=timeout,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return completed.returncode, completed.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except Exception as e:
        return None, str(e)


def check_ipp(d, uri):
    """Asks the printer for its attributes, retrying without TLS on failure."""
    import printer_capabilities
    title = f"IPP get-printer-attributes on {uri}"
    if not shutil.which("ipptool"):
        d.check(title, False, "'ipptool' is not installed (package cups-ipp-utils)",
                optional=True)
        return
    request = printer_capabilities.request_file()
    code, output = run_ipptool(uri, request)
    detail = f"exit code {code}" if code is not None else output
    if code == 0 and "PASS" in output:
        d.check(title, True, detail, f"$ ipptool -t {uri} {request}\n{output}")
        return

    fallback = plain_ipp_uri(uri)
    if fallback:
        f_code, f_output = run_ipptool(fallback, request)
        if f_code == 0 and "PASS" in f_output:
            d.check(title, False,
                    f"{detail} over TLS, but {fallback} answers - only the "
                    f"encrypted connection fails",
                    f"$ ipptool -t {uri} {request}\n{output}\n"
                    f"$ ipptool -t {fallback} {request}\n{f_output}",
                    optional=True)
            return
        output = (f"$ ipptool -t {uri} {request}\n{output}\n"
                  f"$ ipptool -t {fallback} {request}\n{f_output}")
    else:
        output = f"$ ipptool -t {uri} {request}\n{output}"
    d.check(title, False, detail, output)


def phase_printer(d):
    """The physical printer over IPP."""
    c = config.get_config()
    uri = str(c.get("hardware", {}).get("printer_uri", "")).strip()
    host = None
    target = next((t for t in c.get("printers", {}).get("targets", [])
                   if t.get("id") == c.get("printers", {}).get("default")), None)
    if target:
        host = target.get("host")

    # The configured host only matters when the job really leaves over the
    # network. With the virtual printer of the test container (a file backend)
    # the physical printer is not part of the path, so pinging it says nothing.
    network_uri = uri.startswith(("ipp://", "ipps://", "socket://", "http://", "https://"))
    raw_dispatch = bool(target) and target.get("dispatch") == "raw"
    host_in_path = bool(host) and (network_uri or raw_dispatch)
    if host and not host_in_path:
        d.info(f"Ping {host} skipped",
               detail=f"the output goes to {uri or 'the CUPS output queue'}, "
                      f"not to {host}")
    elif host:
        # ICMP is often blocked (firewall, container bridge) while IPP works, so
        # an unanswered ping is a warning, never a failure.
        d.command(["ping", "-c", "2", "-W", "2", host], f"Ping {host}", optional=True)
    if not uri:
        d.check("hardware.printer_uri configured", False)
        return
    if uri.startswith(("ipp://", "ipps://")):
        check_ipp(d, uri)
    else:
        # A socket or file backend (for example the virtual printer of the test
        # container) does not speak IPP, so there is nothing to query.
        d.info("IPP check skipped", detail=f"{uri} is not an IPP target")
    d.command(["lpstat", "-v"], "Device URIs of all queues")
    d.command(["lpstat", "-l", "-p", c.get("hardware", {}).get("cups_queue_name") or "WolsCA_Output"],
              "Details of the output queue", optional=True)

    # Who asks for the flip: the button on the printer or the Continue button of
    # the service. Only one of the two is ever offered.
    import printer_capabilities
    owner = printer_capabilities.flip_owner(target)
    d.info(f"Flip confirmed by the {owner}", detail=printer_capabilities.describe(target))
    if owner == "printer":
        d.check("'ipptool' available for the printer confirmed flip",
                bool(shutil.which("ipptool")),
                "the job is sent straight to the printer, not through the output queue")


def phase_network(d):
    """MQTT broker, mDNS and the web app."""
    c = config.get_config()
    broker = c["mqtt"]["broker_ip"]
    port = int(c["mqtt"]["broker_port"])
    try:
        with socket.create_connection((broker, port), timeout=5):
            d.check(f"TCP connection to the MQTT broker {broker}:{port}", True)
    except OSError as e:
        d.check(f"TCP connection to the MQTT broker {broker}:{port}", False, str(e))
    mqtt_user, _ = config.get_mqtt_credentials()
    # In the test container the broker is not part of what is being released, so
    # a rejected login must not block the build pipeline; on a real installation
    # it stays a failure, because Home Assistant would get nothing.
    is_test_container = str(os.environ.get("WOLSCA_VIRTUAL_OUTPUT", "")).lower() in ("1", "true", "yes")
    d.check("MQTT client connected", mqtt_service.mqtt_client.is_connected(),
            f"topic prefix {mqtt_service.PREFIX}, account '{mqtt_user}' "
            f"(the broker must know this account)", optional=is_test_container)

    web = c.get("web", {})
    if web.get("enabled", True):
        web_port = int(web.get("port", 8080))
        try:
            with socket.create_connection(("127.0.0.1", web_port), timeout=5):
                d.check(f"Web app listening on port {web_port}", True)
        except OSError as e:
            d.check(f"Web app listening on port {web_port}", False, str(e))

    d.command(["avahi-browse", "-rt", "_ipp._tcp"], "IPP services announced over mDNS",
              timeout=20, optional=True)
    d.command(["ss", "-ltnp"], "Listening TCP sockets", optional=True)


def phase_notify(d):
    """Push notifications: configuration and a real test message."""
    import notifier

    info = notifier.describe()
    if not info["enabled"]:
        d.info("Notifications are switched off", detail="notify.enabled is false")
        return
    d.check("Notification server configured", bool(info["url"]), info["url"])
    topic = notifier.ensure_topic()
    d.check("Notification topic present", bool(topic),
            f"subscribe on the phone: {notifier.subscribe_url(topic)}")
    ok, detail = notifier.self_test()
    d.check("Test notification delivered", ok, detail)


def phase_chain(d):
    """End to end: submit a page to an intake queue and watch the drop folder."""
    c = config.get_config()
    queues = c.get("intake", {}).get("queues", [])
    if not queues:
        d.check("Intake queue available for the chain test", False)
        return
    q_entry = queues[0]
    queue_name = q_entry.get("cups_queue")
    directory = q_entry.get("directory") or config.DROP_DIR

    # Watch the whole drop tree, not only the directory of this queue: when
    # cups-pdf ignores its per-instance configuration the PDF lands in the
    # parent folder, and then the print mode of the queue is silently lost.
    watched = config.DROP_DIR

    def snapshot():
        found = set()
        for root, _dirs, files in os.walk(watched):
            found.update(os.path.join(root, f) for f in files
                         if f.lower().endswith(".pdf"))
        return found

    before = snapshot()

    step = d.command(["lp", "-d", queue_name, "-t", "WolsCA-SelfTest",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_strings.json")],
                     f"Submit a self-test job to '{queue_name}'")
    if step["status"] != "PASS":
        return

    deadline = time.time() + 40
    appeared = []
    while time.time() < deadline and not appeared:
        time.sleep(2)
        appeared = sorted(snapshot() - before)

    d.check("cups-pdf produced a PDF in the drop tree", bool(appeared),
            ", ".join(appeared) or f"nothing new below {watched} within 40s")
    if appeared:
        in_queue_dir = [p for p in appeared
                        if os.path.abspath(p).startswith(os.path.abspath(directory) + os.sep)]
        d.check(f"PDF landed in the directory of '{queue_name}'", bool(in_queue_dir),
                f"expected below {directory}, found {', '.join(appeared)}; "
                "if it is in the parent folder cups-pdf ignored "
                f"/etc/cups/cups-pdf-{q_entry.get('id')}.conf and the print mode "
                "of the queue is lost")
    d.command("ls -laR " + watched, "Content of the drop tree", optional=True)
    d.command(["lpstat", "-W", "completed", "-o"], "Completed CUPS jobs", optional=True)
    d.command(["journalctl", "-u", "wolsca-print-service", "-n", "40", "--no-pager"],
              "Last service log lines", optional=True)


PHASES = {
    "system": phase_system,
    "config": phase_config,
    "admin": phase_admin,
    "permissions": phase_permissions,
    "update": phase_update,
    "cups": phase_cups,
    "printer": phase_printer,
    "network": phase_network,
    "notify": phase_notify,
    "chain": phase_chain
}

# 'chain' actually submits a print job, so it is not part of the default run.
DEFAULT_PHASES = ["system", "config", "admin", "permissions", "update",
                  "cups", "printer", "network", "notify"]


def run(phases=None, publish=True):
    """Runs the requested phases and publishes the aggregated report."""
    requested = [p for p in (phases or DEFAULT_PHASES) if p in PHASES]
    if not requested:
        requested = list(DEFAULT_PHASES)

    if not run_lock.acquire(blocking=False):
        print("[Test] A diagnostics run is already in progress.")
        return last_report or {"result": "BUSY"}

    try:
        d = Diagnostics(publish=publish)
        if publish:
            mqtt_service.publish_log(f"Self-test started ({', '.join(requested)}).", "info")
            mqtt_service.mqtt_client.publish(topic("state"), "running", retain=True)

        print("\n===================================================")
        print("  Wols CA Print Service self-test")
        print("===================================================")

        for name in requested:
            d.phase = name
            print(f"\n--- Phase: {name} ---")
            try:
                PHASES[name](d)
            except Exception as e:
                d._record(f"Phase '{name}' crashed", "FAIL", "", "", str(e))

        report = d.report(requested)
        globals()["last_report"] = report

        if publish:
            mqtt_service.mqtt_client.publish(topic("report"), json.dumps(report), retain=True)
            mqtt_service.mqtt_client.publish(topic("state"), report["result"], retain=True)
            mqtt_service.publish_log(f"Self-test finished: {report['summary']}.",
                                     "error" if report["failed"] else "info")

        print(f"\n[Test] Result: {report['result']} - {report['summary']} "
              f"in {report['duration_s']}s")
        for failure in report["failed_steps"]:
            print(f"[Test] Failed: {failure}")
        return report
    finally:
        run_lock.release()


def run_async(phases=None):
    threading.Thread(target=run, kwargs={"phases": phases}, daemon=True).start()


def publish_ha_discovery():
    """Home Assistant entities for the diagnostics report."""
    device = mqtt_service.DEVICE_INFO

    sensor = {
        "name": mqtt_service.entity_name("Print Service Self-Test"),
        "state_topic": topic("report"),
        "value_template": "{{ value_json.result }}",
        "json_attributes_topic": topic("report"),
        "icon": "mdi:clipboard-check-outline",
        "unique_id": mqtt_service.uid("wolsca_print_selftest"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/selftest/config",
        json.dumps(sensor), retain=True)

    summary = {
        "name": mqtt_service.entity_name("Print Service Self-Test Summary"),
        "state_topic": topic("report"),
        "value_template": "{{ value_json.summary }}",
        "icon": "mdi:format-list-checks",
        "unique_id": mqtt_service.uid("wolsca_print_selftest_summary"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/selftest_summary/config",
        json.dumps(summary), retain=True)

    failures = {
        "name": mqtt_service.entity_name("Print Service Self-Test Failures"),
        "state_topic": topic("report"),
        "value_template": "{{ value_json.failed }}",
        "json_attributes_topic": topic("report"),
        "json_attributes_template": "{{ {'failed_steps': value_json.failed_steps} | tojson }}",
        "icon": "mdi:alert-decagram-outline",
        "unique_id": mqtt_service.uid("wolsca_print_selftest_failures"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/{mqtt_service.NODE_ID}/selftest_failures/config",
        json.dumps(failures), retain=True)

    button = {
        "name": mqtt_service.entity_name("Run Print Service Self-Test"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "SELFTEST",
        "icon": "mdi:play-box-outline",
        "unique_id": mqtt_service.uid("wolsca_print_selftest_run"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/selftest/config",
        json.dumps(button), retain=True)

    chain_button = {
        "name": mqtt_service.entity_name("Run Print Service Chain Test"),
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "SELFTEST_CHAIN",
        "icon": "mdi:printer-check",
        "unique_id": mqtt_service.uid("wolsca_print_selftest_chain"),
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/{mqtt_service.NODE_ID}/selftest_chain/config",
        json.dumps(chain_button), retain=True)
