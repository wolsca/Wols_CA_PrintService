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


def phase_printer(d):
    """The physical printer over IPP."""
    c = config.get_config()
    uri = str(c.get("hardware", {}).get("printer_uri", "")).strip()
    host = None
    target = next((t for t in c.get("printers", {}).get("targets", [])
                   if t.get("id") == c.get("printers", {}).get("default")), None)
    if target:
        host = target.get("host")
    if host:
        d.command(["ping", "-c", "2", "-W", "2", host], f"Ping {host}")
    if not uri:
        d.check("hardware.printer_uri configured", False)
        return
    d.command(["ipptool", "-t", uri, "get-printer-attributes.test"],
              f"IPP get-printer-attributes on {uri}", timeout=45, expect="PASS")
    d.command(["lpstat", "-v"], "Device URIs of all queues")
    d.command(["lpstat", "-l", "-p", c.get("hardware", {}).get("cups_queue_name") or "WolsCA_Output"],
              "Details of the output queue", optional=True)


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
    d.check("MQTT client connected", mqtt_service.mqtt_client.is_connected(),
            f"topic prefix {mqtt_service.PREFIX}")

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

    before = set()
    for root, _dirs, files in os.walk(directory):
        before.update(os.path.join(root, f) for f in files)

    step = d.command(["lp", "-d", queue_name, "-t", "WolsCA-SelfTest",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_strings.json")],
                     f"Submit a self-test job to '{queue_name}'")
    if step["status"] != "PASS":
        return

    deadline = time.time() + 40
    appeared = []
    while time.time() < deadline and not appeared:
        time.sleep(2)
        for root, _dirs, files in os.walk(directory):
            for name in files:
                path = os.path.join(root, name)
                if path not in before and name.lower().endswith(".pdf"):
                    appeared.append(path)
    d.check("cups-pdf produced a PDF in the drop folder", bool(appeared),
            ", ".join(os.path.basename(p) for p in appeared) or f"nothing new in {directory} within 40s")
    d.command(["lpstat", "-W", "completed", "-o"], "Completed CUPS jobs", optional=True)
    d.command(["journalctl", "-u", "wolsca-print-service", "-n", "40", "--no-pager"],
              "Last service log lines", optional=True)


PHASES = {
    "system": phase_system,
    "config": phase_config,
    "cups": phase_cups,
    "printer": phase_printer,
    "network": phase_network,
    "chain": phase_chain
}

# 'chain' actually submits a print job, so it is not part of the default run.
DEFAULT_PHASES = ["system", "config", "cups", "printer", "network"]


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
        "name": "Print Service Self-Test",
        "state_topic": topic("report"),
        "value_template": "{{ value_json.result }}",
        "json_attributes_topic": topic("report"),
        "icon": "mdi:clipboard-check-outline",
        "unique_id": "wolsca_print_selftest",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/wolsca_print/selftest/config",
        json.dumps(sensor), retain=True)

    summary = {
        "name": "Print Service Self-Test Summary",
        "state_topic": topic("report"),
        "value_template": "{{ value_json.summary }}",
        "icon": "mdi:format-list-checks",
        "unique_id": "wolsca_print_selftest_summary",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/wolsca_print/selftest_summary/config",
        json.dumps(summary), retain=True)

    failures = {
        "name": "Print Service Self-Test Failures",
        "state_topic": topic("report"),
        "value_template": "{{ value_json.failed }}",
        "json_attributes_topic": topic("report"),
        "json_attributes_template": "{{ {'failed_steps': value_json.failed_steps} | tojson }}",
        "icon": "mdi:alert-decagram-outline",
        "unique_id": "wolsca_print_selftest_failures",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/sensor/wolsca_print/selftest_failures/config",
        json.dumps(failures), retain=True)

    button = {
        "name": "Run Print Service Self-Test",
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "SELFTEST",
        "icon": "mdi:play-box-outline",
        "unique_id": "wolsca_print_selftest_run",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/wolsca_print/selftest/config",
        json.dumps(button), retain=True)

    chain_button = {
        "name": "Run Print Service Chain Test",
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "SELFTEST_CHAIN",
        "icon": "mdi:printer-check",
        "unique_id": "wolsca_print_selftest_chain",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/wolsca_print/selftest_chain/config",
        json.dumps(chain_button), retain=True)
