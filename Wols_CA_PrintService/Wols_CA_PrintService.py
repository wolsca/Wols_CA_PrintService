import os
import sys
import time
import json
import platform
import subprocess
import tempfile
import shutil
import socket
import signal
import threading
import ssl
import secrets
import queue
import io
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import paho.mqtt.client as mqtt
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pypdf import PdfReader, PdfWriter, PageObject, Transformation

SERVICE_VERSION = "1.4.0"

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

if IS_WINDOWS:
    import ctypes
    import winreg

# systemd captures stdout/stderr, so keep the log lines unbuffered.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# ============================================================================
# 1. CONFIGURATION MANAGEMENT (JSON)
# ============================================================================
CONFIG_FILE = "WolsCAPrintService.json"
# System wide location used by the Debian/systemd deployment.
SYSTEM_CONFIG_DIR = "/etc/wolsca"
config = {}

def resolve_config_path():
    """Config lookup order: $WOLSCA_CONFIG, /etc/wolsca (Linux), next to the script."""
    env_path = os.environ.get("WOLSCA_CONFIG")
    if env_path:
        return os.path.abspath(env_path)

    if IS_LINUX:
        system_path = os.path.join(SYSTEM_CONFIG_DIR, CONFIG_FILE)
        if os.path.exists(system_path) or os.path.isdir(SYSTEM_CONFIG_DIR):
            return system_path

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)

CONFIG_PATH = resolve_config_path()

def load_or_create_config():
    global config
    
    default_config = {
        "mqtt": {
            "broker_ip": "192.168.101.240",
            "broker_port": 1883,
            "topic_prefix": "wolsca/printer",
            "discovery_prefix": "homeassistant"
        },
        "paths": {
            "drop_directory": r"C:\ProgramData\WolsCA\PrintFileDrop" if platform.system() == "Windows" else "/var/spool/wolsca/PrintFileDrop",
            "temp_directory": r"C:\ProgramData\WolsCA\PrintTemp" if platform.system() == "Windows" else "/var/spool/wolsca/PrintTemp",
            "error_directory": r"C:\ProgramData\WolsCA\PrintError" if platform.system() == "Windows" else "/var/spool/wolsca/PrintError"
        },
        "hardware": {
            "printer_uri": "ipps://192.168.101.251:443/ipp/print",
            "flip_instruction": "",
            "flip_timeout_seconds": 1800
        },
        "intake": {
            "enabled": True,
            "queues": [
                {
                    "id": "booklet",
                    "cups_queue": "WolsCA_Booklet",
                    "description": "Booklet (A5, fold in the middle)",
                    "print_mode": "Booklet",
                    "directory": ""
                },
                {
                    "id": "duplex",
                    "cups_queue": "WolsCA_DoubleSided",
                    "description": "Double sided (two pages per sheet, front and back)",
                    "print_mode": "Duplex",
                    "directory": ""
                },
                {
                    "id": "simplex",
                    "cups_queue": "WolsCA_SingleSided",
                    "description": "Single sided (one page per sheet)",
                    "print_mode": "Simplex",
                    "directory": ""
                }
            ]
        },
        "printers": {
            "default": "main",
            "personal_choice_ttl_seconds": 900,
            "targets": [
                {
                    "id": "main",
                    "name": "Main Printer",
                    "host": "192.168.101.251",
                    "port": 9100,
                    "duplex": False,
                    "dispatch": "raw",
                    "cups_queue": "",
                    "flip_instruction": ""
                }
            ]
        },
        "web": {
            "enabled": True,
            "bind_address": "0.0.0.0",
            "port": 8080,
            "title": "Wols CA Booklet Printer",
            "language": "en",
            "public_url": "",
            "admin_token": ""
        },
        "notify": {
            "enabled": False,
            "url": "https://ntfy.sh",
            "topic": "",
            "token": "",
            "priority": "high",
            "notify_on_error": True
        },
        "history": {
            "enabled": True,
            "max_entries": 10,
            "file": ""
        },
        "virtual_printer": {
            "name": "Wols CA Print Service",
            "installer_path": r"C:\ProgramData\WolsCA\Installers\PDFCreator-Setup.exe",
            "download_url": "https://download.pdfforge.org/download/pdfcreator/PDFCreator-stable",
            "cups_queue_name": "WolsCA_Booklet",
            "cups_share_on_network": True
        },
        "settings": {
            "user": "WolsCADoublePrint",
            "password": "DefaultPassword",
            "print_mode": "Booklet"
        }
    }

    config_path = CONFIG_PATH

    if os.path.exists(config_path):
        print(f"[System] Loading configuration from {config_path}...")
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                if "settings" not in config: config["settings"] = default_config["settings"]
                if "hardware" not in config: config["hardware"] = default_config["hardware"]
                if "virtual_printer" not in config: config["virtual_printer"] = default_config["virtual_printer"]
                if "printers" not in config: config["printers"] = default_config["printers"]
                if "web" not in config: config["web"] = default_config["web"]
                if "notify" not in config: config["notify"] = default_config["notify"]
                if "history" not in config: config["history"] = default_config["history"]
                if "intake" not in config: config["intake"] = default_config["intake"]
                # Fill in keys added by later versions so old files keep working.
                for section in ("hardware", "web", "notify", "history", "printers", "intake"):
                    for key, value in default_config[section].items():
                        config[section].setdefault(key, value)
        except Exception as e:
            print(f"[Error] Failed to read JSON config: {e}. Using defaults.")
            config = default_config
    else:
        print(f"[System] Config file not found. Creating default {config_path}...")
        config = default_config
        save_config()

def save_config():
    config_path = CONFIG_PATH
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=4)
        print("[System] Configuration saved successfully.")
    except Exception as e:
        print(f"[Error] Could not save config file: {e}")

load_or_create_config()

DROP_DIR = config["paths"]["drop_directory"]
TEMP_DIR = config["paths"]["temp_directory"]
ERROR_DIR = config.get("paths", {}).get("error_directory", os.path.join(TEMP_DIR, "error"))

MQTT_BROKER = config["mqtt"]["broker_ip"]
MQTT_PORT = config["mqtt"]["broker_port"]
PREFIX = config["mqtt"].get("topic_prefix", "wolsca/printer")
HA_PREFIX = config["mqtt"].get("discovery_prefix", "homeassistant")

MQTT_USER = config["settings"]["user"]
MQTT_PASS = config["settings"]["password"]

waiting_for_user_action = False
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
shutdown_event = threading.Event()

for directory in [DROP_DIR, TEMP_DIR, ERROR_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        print(f"[Error] Could not create directory {directory}: {e}")
        sys.exit(1)

# ============================================================================
# 1a. INTAKE QUEUES (ONE VISIBLE PRINTER PER PRINT MODE)
# ============================================================================
# Every intake queue is a shared CUPS queue with its own cups-pdf output
# directory. The directory a document arrives in decides the print mode, so the
# user simply picks 'Booklet', 'Double sided' or 'Single sided' in the normal
# print dialog of any device.
def intake_queues():
    """Normalized list of the configured intake queues (may be empty)."""
    section = config.get("intake", {}) or {}
    if not section.get("enabled", True):
        return []

    queues = []
    for entry in section.get("queues", []) or []:
        if not isinstance(entry, dict):
            continue
        queue_id = str(entry.get("id") or entry.get("cups_queue") or "").strip()
        if not queue_id:
            continue
        mode = str(entry.get("print_mode", "Booklet"))
        directory = str(entry.get("directory", "")).strip() or os.path.join(DROP_DIR, queue_id)
        queues.append({
            "id": queue_id,
            "cups_queue": entry.get("cups_queue") or f"WolsCA_{queue_id.capitalize()}",
            "description": entry.get("description") or mode,
            "print_mode": mode,
            "directory": directory
        })
    return queues

def prepare_intake_directories():
    """Creates the per-queue drop directories so the watchers can start."""
    for queue_entry in intake_queues():
        try:
            os.makedirs(queue_entry["directory"], exist_ok=True)
        except OSError as e:
            print(f"[Error] Could not create intake directory "
                  f"{queue_entry['directory']}: {e}")

prepare_intake_directories()

# ============================================================================
# 1b. PRINTER SELECTION (ADMIN DEFAULT + PERSONAL CHOICE FROM THE WEB APP)
# ============================================================================
state_lock = threading.Lock()

# Personal (per browser) printer choices made in the web app: token -> printer id.
personal_choices = {}
# The choice that will be applied to the next incoming job, with its expiry.
pending_choice = {"printer_id": None, "expires": 0.0, "token": None}

# Sticky per browser job options (copies / print mode), same idea as the printer choice.
personal_options = {}
pending_options = {"copies": None, "print_mode": None, "expires": 0.0, "token": None}

# Job control flags, set from the web app or MQTT while a job is running.
job_control = {"cancel": False, "reprint_front": False}

job_state = {
    "state": "STARTING",
    "detail": "",
    "filename": None,
    "pages": 0,
    "sheets": 0,
    "side": None,
    "copies": 1,
    "print_mode": None,
    "duplex": False,
    "sheets_done": 0,
    "printer_id": None,
    "printer_name": None,
    "intake_queue": None,
    "flip_instruction": "",
    "flip_deadline": 0.0,
    "waiting_for_flip": False,
    "updated": datetime.now().isoformat()
}

def legacy_target_from_uri():
    """Builds a target from the old single 'hardware.printer_uri' setting."""
    uri = config.get("hardware", {}).get("printer_uri", "")
    host = None
    try:
        host = urlparse(uri).hostname
    except Exception:
        host = None
    return {
        "id": "legacy",
        "name": f"Printer {host}" if host else "Configured printer",
        "host": host or "127.0.0.1",
        "port": 9100,
        "duplex": False,
        "dispatch": "raw",
        "cups_queue": "",
        "flip_instruction": ""
    }

def printer_targets():
    """All printers the admin made available, normalised."""
    targets = []
    for entry in config.get("printers", {}).get("targets", []) or []:
        host = entry.get("host")
        if not host and entry.get("uri"):
            try:
                host = urlparse(entry["uri"]).hostname
            except Exception:
                host = None
        if not host:
            continue
        targets.append({
            "id": str(entry.get("id") or host),
            "name": entry.get("name") or str(entry.get("id") or host),
            "host": host,
            "port": int(entry.get("port", 9100)),
            "location": entry.get("location", ""),
            # A real duplex printer prints both sides itself, so no flip is needed.
            "duplex": bool(entry.get("duplex", False)),
            # 'raw' = port 9100 socket, 'cups' = local CUPS queue with page progress.
            "dispatch": str(entry.get("dispatch", "raw")).lower(),
            "cups_queue": entry.get("cups_queue", ""),
            "flip_instruction": entry.get("flip_instruction", "")
        })
    if not targets:
        targets.append(legacy_target_from_uri())
    return targets

def find_printer(printer_id):
    for target in printer_targets():
        if target["id"] == printer_id:
            return target
    return None

def default_printer():
    """The printer chosen by the administrator in the configuration file."""
    configured = config.get("printers", {}).get("default")
    return find_printer(configured) or printer_targets()[0]

def personal_choice_ttl():
    try:
        return float(config.get("printers", {}).get("personal_choice_ttl_seconds", 900))
    except (TypeError, ValueError):
        return 900.0

def set_personal_printer(token, printer_id):
    """Stores a personal (per device) printer choice made in the web app."""
    target = find_printer(printer_id)
    if not target:
        return None
    with state_lock:
        personal_choices[token] = printer_id
        pending_choice["printer_id"] = printer_id
        pending_choice["expires"] = time.time() + personal_choice_ttl()
        pending_choice["token"] = token
    print(f"[Printers] Personal choice '{target['name']}' will be used for the next job.")
    mqtt_client.publish(f"{PREFIX}/settings/printer/state", target["name"], retain=True)
    return target

def set_default_printer(printer_id):
    """Administrator action: change the system wide default printer."""
    target = find_printer(printer_id)
    if not target:
        return None
    config.setdefault("printers", {})["default"] = target["id"]
    save_config()
    print(f"[Printers] Default printer changed to '{target['name']}'.")
    return target

def resolve_target_printer():
    """A valid personal choice wins over the administrator default."""
    with state_lock:
        printer_id = pending_choice["printer_id"]
        expires = pending_choice["expires"]
    if printer_id and time.time() < expires:
        target = find_printer(printer_id)
        if target:
            return target, "personal"
    return default_printer(), "default"

def consume_pending_choice():
    with state_lock:
        pending_choice["printer_id"] = None
        pending_choice["expires"] = 0.0
        pending_choice["token"] = None

PRINT_MODES = ["Bypass", "Standard", "Simplex", "Duplex", "Booklet"]

def set_personal_options(token, copies=None, print_mode=None):
    """Stores per device job options (copies, print mode) for the next job."""
    stored = personal_options.setdefault(token, {})
    if copies is not None:
        copies = max(1, min(20, int(copies)))
        stored["copies"] = copies
    if print_mode is not None:
        if print_mode not in PRINT_MODES:
            return None
        stored["print_mode"] = print_mode
    with state_lock:
        pending_options["copies"] = stored.get("copies")
        pending_options["print_mode"] = stored.get("print_mode")
        pending_options["expires"] = time.time() + personal_choice_ttl()
        pending_options["token"] = token
    print(f"[Options] Next job: copies={stored.get('copies')}, mode={stored.get('print_mode')}")
    return stored

def resolve_job_options():
    """Personal (web app) options win over the configured defaults."""
    with state_lock:
        copies = pending_options["copies"]
        print_mode = pending_options["print_mode"]
        expires = pending_options["expires"]
    if time.time() >= expires:
        copies, print_mode = None, None
    return {
        "copies": copies or 1,
        "print_mode": print_mode or config["settings"]["print_mode"]
    }

def consume_pending_options():
    with state_lock:
        pending_options["copies"] = None
        pending_options["print_mode"] = None
        pending_options["expires"] = 0.0
        pending_options["token"] = None

# ============================================================================
# 1c. PUSH NOTIFICATIONS (ntfy / GOTIFY COMPATIBLE HTTP POST)
# ============================================================================
def notify_config():
    return config.get("notify", {}) or {}

def public_base_url():
    """URL a phone can use to reach the web app, for the notification action."""
    configured = config.get("web", {}).get("public_url", "").strip()
    if configured:
        return configured.rstrip("/")
    port = int(config.get("web", {}).get("port", 8080))
    return f"http://{socket.gethostname()}.local:{port}"

def send_push_notification(title, message, tags="printer", actions=None):
    """Fire-and-forget POST to an ntfy compatible server; never breaks a job."""
    settings = notify_config()
    if not settings.get("enabled"):
        return
    topic = str(settings.get("topic", "")).strip()
    base = str(settings.get("url", "")).strip().rstrip("/")
    if not topic or not base:
        print("[Notify] Enabled but 'url' or 'topic' is missing; skipping.")
        return

    headers = {
        "Title": title,
        "Priority": str(settings.get("priority", "default")),
        "Tags": tags
    }
    if actions:
        headers["Actions"] = actions
    token = str(settings.get("token", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def worker():
        try:
            request = urllib.request.Request(f"{base}/{topic}",
                                             data=message.encode("utf-8"),
                                             headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=10):
                pass
            print(f"[Notify] Push sent: {title}")
        except Exception as e:
            print(f"[Notify] Could not send push notification: {e}")

    threading.Thread(target=worker, daemon=True).start()

def notify_flip_required(filename, sheets, instruction):
    """The one notification that really matters: go and flip the paper."""
    resume_url = f"{public_base_url()}/api/resume"
    send_push_notification(
        title="Turn the paper over",
        message=f"{filename}: front side done, {sheets} sheet(s) ready.\n{instruction}",
        tags="printer,arrows_counterclockwise",
        actions=f"http, Continue printing, {resume_url}, method=POST, clear=true"
    )

def notify_error(filename, message):
    if notify_config().get("notify_on_error", True):
        send_push_notification(title="Print job failed",
                               message=f"{filename or 'Print job'}: {message}",
                               tags="printer,warning")

# ============================================================================
# 1d. HUMAN READABLE ERRORS
# ============================================================================
FRIENDLY_ERRORS = [
    ("timed out", "The printer did not answer. Is it switched on and connected to the network?"),
    ("refused the connection", "The printer refused the print job. Check the printer address and port."),
    ("no route to host", "The printer cannot be reached on the network."),
    ("name or service not known", "The printer name could not be resolved. Check the configuration."),
    ("encrypted", "This PDF is password protected. Remove the password and print it again."),
    ("empty", "This PDF contains no pages."),
    ("no pages", "This PDF contains no pages."),
    ("corrupt", "This PDF could not be read. Try exporting or printing it again."),
    ("unreadable", "This PDF could not be read. Try exporting or printing it again."),
    ("cancel", "The print job was cancelled."),
    ("permission", "The service is not allowed to read or write the file.")
]

def friendly_error(message):
    """Turns an internal exception text into a sentence a user understands."""
    lowered = str(message).lower()
    for needle, sentence in FRIENDLY_ERRORS:
        if needle in lowered:
            return sentence
    return f"Something went wrong: {message}"

# ============================================================================
# 1e. JOB HISTORY (LAST N JOBS, KEPT IN MEMORY AND ON DISK)
# ============================================================================
history_lock = threading.Lock()
job_history = []

def history_file_path():
    configured = config.get("history", {}).get("file", "").strip()
    if configured:
        return configured
    return os.path.join(TEMP_DIR, "job_history.json")

def history_limit():
    try:
        return max(1, int(config.get("history", {}).get("max_entries", 10)))
    except (TypeError, ValueError):
        return 10

def load_history():
    if not config.get("history", {}).get("enabled", True):
        return
    path = history_file_path()
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                entries = json.load(f)
            if isinstance(entries, list):
                with history_lock:
                    job_history.extend(entries[-history_limit():])
                print(f"[History] Loaded {len(job_history)} previous job(s).")
    except Exception as e:
        print(f"[History] Could not read {path}: {e}")

def record_history(filename, pages, sheets, copies, mode, printer_name, result, detail=""):
    if not config.get("history", {}).get("enabled", True):
        return
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "filename": filename,
        "pages": pages,
        "sheets": sheets,
        "copies": copies,
        "mode": mode,
        "printer": printer_name,
        "result": result,
        "detail": detail
    }
    with history_lock:
        job_history.append(entry)
        del job_history[:-history_limit()]
        snapshot = list(job_history)
    try:
        with open(history_file_path(), 'w') as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        print(f"[History] Could not write history file: {e}")

def history_entries():
    with history_lock:
        return list(reversed(job_history))

# ============================================================================
# 2. MQTT STATE MACHINE & HA AUTO-DISCOVERY
# ============================================================================
DEVICE_INFO = {
    "identifiers": ["wolsca_print_service_01"],
    "name": "Wols CA Print Service",
    "manufacturer": "Wols CA",
    "model": "Double Sided Spooler v1.2"
}

def publish_ha_discovery():
    print("[MQTT] Publishing Home Assistant Auto-Discovery configuration...")
    
    config_status = {
        "name": "Print Service Status",
        "state_topic": f"{PREFIX}/status",
        "value_template": "{{ value_json.state }}",
        "json_attributes_topic": f"{PREFIX}/status",
        "icon": "mdi:printer-3d",
        "unique_id": "wolsca_print_status",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/sensor/wolsca_print/status/config", json.dumps(config_status), retain=True)

    config_error = {
        "name": "Print Service Last Error",
        "state_topic": f"{PREFIX}/status",
        "value_template": "{% if value_json.state == 'ERROR' %}{{ value_json.detail }}{% else %}No errors{% endif %}",
        "icon": "mdi:alert-circle-outline",
        "unique_id": "wolsca_print_error",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/sensor/wolsca_print/error/config", json.dumps(config_error), retain=True)

    config_button = {
        "name": "Resume Print (Flip)",
        "command_topic": f"{PREFIX}/command",
        "payload_press": "RESUME",
        "icon": "mdi:page-next",
        "unique_id": "wolsca_print_resume",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/button/wolsca_print/resume/config", json.dumps(config_button), retain=True)

    config_cancel = {
        "name": "Cancel Print Job",
        "command_topic": f"{PREFIX}/command",
        "payload_press": "CANCEL",
        "icon": "mdi:cancel",
        "unique_id": "wolsca_print_cancel",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/button/wolsca_print/cancel/config", json.dumps(config_cancel), retain=True)

    config_reprint = {
        "name": "Reprint Front Side",
        "command_topic": f"{PREFIX}/command",
        "payload_press": "REPRINT",
        "icon": "mdi:printer-refresh",
        "unique_id": "wolsca_print_reprint",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/button/wolsca_print/reprint/config", json.dumps(config_reprint), retain=True)

    config_mode = {
        "name": "Print Mode",
        "state_topic": f"{PREFIX}/settings/mode/state",
        "command_topic": f"{PREFIX}/settings/mode/set",
        "options": PRINT_MODES,
        "icon": "mdi:book-open-page-variant",
        "unique_id": "wolsca_print_mode",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/select/wolsca_print/mode/config", json.dumps(config_mode), retain=True)
    mqtt_client.publish(f"{PREFIX}/settings/mode/state", config["settings"]["print_mode"], retain=True)

    names = [target["name"] for target in printer_targets()]
    config_printer = {
        "name": "Target Printer",
        "state_topic": f"{PREFIX}/settings/printer/state",
        "command_topic": f"{PREFIX}/settings/printer/set",
        "options": names,
        "icon": "mdi:printer-settings",
        "unique_id": "wolsca_print_target",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/select/wolsca_print/printer/config", json.dumps(config_printer), retain=True)
    mqtt_client.publish(f"{PREFIX}/settings/printer/state", default_printer()["name"], retain=True)

def set_state(state, detail="", **fields):
    with state_lock:
        job_state["state"] = state
        job_state["detail"] = detail
        job_state["waiting_for_flip"] = (state == "WAITING_FOR_FLIP")
        job_state["updated"] = datetime.now().isoformat()
        job_state.update(fields)
        snapshot = dict(job_state)

    payload = json.dumps({
        "state": state,
        "detail": detail,
        "filename": snapshot["filename"],
        "pages": snapshot["pages"],
        "sheets": snapshot["sheets"],
        "sheets_done": snapshot["sheets_done"],
        "side": snapshot["side"],
        "copies": snapshot["copies"],
        "print_mode": snapshot["print_mode"],
        "printer": snapshot["printer_name"],
        "queued": pending_job_count(),
        "version": SERVICE_VERSION,
        "timestamp": snapshot["updated"]
    })
    mqtt_client.publish(f"{PREFIX}/status", payload, retain=True)
    print(f"[State] -> {state} {f'({detail})' if detail else ''}")

def publish_metrics(filename, page_count, mode, processing_time):
    metrics = {
        "filename": filename,
        "page_count": page_count,
        "mode": mode,
        "processing_time_ms": processing_time,
        "timestamp": datetime.now().isoformat()
    }
    mqtt_client.publish(f"{PREFIX}/metrics", json.dumps(metrics))

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print(f"[MQTT] Successfully connected to broker at {MQTT_BROKER}")
        publish_ha_discovery()
        client.subscribe(f"{PREFIX}/command")
        client.subscribe(f"{PREFIX}/settings/mode/set")
        client.subscribe(f"{PREFIX}/settings/printer/set")
        set_state("IDLE", "Service started and synchronized with HA.")
    else:
        print(f"[MQTT] Connection failed, return code {reason_code}")

def on_message(client, userdata, msg):
    global waiting_for_user_action
    payload = msg.payload.decode('utf-8')
    topic = msg.topic
    
    if topic == f"{PREFIX}/command" and payload == "RESUME":
        if waiting_for_user_action:
            print("[System] 'RESUME' command received via MQTT. Continuing workflow...")
            waiting_for_user_action = False

    elif topic == f"{PREFIX}/command" and payload == "CANCEL":
        print("[System] 'CANCEL' command received via MQTT.")
        request_cancel()

    elif topic == f"{PREFIX}/command" and payload == "REPRINT":
        print("[System] 'REPRINT' command received via MQTT.")
        request_reprint_front()
            
    elif topic == f"{PREFIX}/settings/mode/set":
        print(f"[Settings] Print Mode updated to: {payload}")
        config["settings"]["print_mode"] = payload
        save_config()
        mqtt_client.publish(f"{PREFIX}/settings/mode/state", payload, retain=True)

    elif topic == f"{PREFIX}/settings/printer/set":
        # Home Assistant sends the display name; treat it as an admin action.
        chosen = next((t for t in printer_targets() if t["name"] == payload), None)
        if chosen and set_default_printer(chosen["id"]):
            mqtt_client.publish(f"{PREFIX}/settings/printer/state", chosen["name"], retain=True)
        else:
            print(f"[Settings] Unknown printer requested over MQTT: {payload}")

mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

# ============================================================================
# 3. PDF MANIPULATION (IMPOSITION & FAULT TOLERANCE)
# ============================================================================
def validate_pdf(input_pdf_path):
    """Fails fast with a clear message on encrypted, empty or corrupt PDFs."""
    try:
        reader = PdfReader(input_pdf_path)
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {e}")

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("This PDF is encrypted and cannot be printed.")
        except ValueError:
            raise
        except Exception:
            raise ValueError("This PDF is encrypted and cannot be printed.")

    try:
        page_count = len(reader.pages)
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {e}")

    if page_count == 0:
        raise ValueError("The PDF has no pages.")
    return page_count

def generate_booklet_pdfs(input_pdf_path):
    try:
        reader = PdfReader(input_pdf_path)
        page_count = len(reader.pages)
        
        a4_width, a4_height = 842.0, 595.0
        a5_width, a5_height = a4_width / 2.0, a4_height

        total_booklet_pages = ((page_count + 3) // 4) * 4
        total_sheets = total_booklet_pages // 4

        front_writer = PdfWriter()
        back_writer = PdfWriter()

        for sheet in range(total_sheets):
            left_page_front_idx = total_booklet_pages - 1 - (2 * sheet)
            right_page_front_idx = 2 * sheet
            left_page_back_idx = (2 * sheet) + 1
            right_page_back_idx = total_booklet_pages - 2 - (2 * sheet)

            # Create Front Sheet
            front_sheet = PageObject.create_blank_page(width=a4_width, height=a4_height)
            if left_page_front_idx < page_count:
                lp_f = reader.pages[left_page_front_idx]
                scale = min(a5_width / float(lp_f.mediabox.width), a5_height / float(lp_f.mediabox.height))
                lp_f.add_transformation(Transformation().scale(scale, scale))
                front_sheet.merge_page(lp_f)
                
            if right_page_front_idx < page_count:
                rp_f = reader.pages[right_page_front_idx]
                scale = min(a5_width / float(rp_f.mediabox.width), a5_height / float(rp_f.mediabox.height))
                rp_f.add_transformation(Transformation().scale(scale, scale).translate(tx=a5_width, ty=0))
                front_sheet.merge_page(rp_f)
            front_writer.add_page(front_sheet)

            # Create Back Sheet
            back_sheet = PageObject.create_blank_page(width=a4_width, height=a4_height)
            if left_page_back_idx < page_count:
                lp_b = reader.pages[left_page_back_idx]
                scale = min(a5_width / float(lp_b.mediabox.width), a5_height / float(lp_b.mediabox.height))
                lp_b.add_transformation(Transformation().scale(scale, scale))
                back_sheet.merge_page(lp_b)
                
            if right_page_back_idx < page_count:
                rp_b = reader.pages[right_page_back_idx]
                scale = min(a5_width / float(rp_b.mediabox.width), a5_height / float(rp_b.mediabox.height))
                rp_b.add_transformation(Transformation().scale(scale, scale).translate(tx=a5_width, ty=0))
                back_sheet.merge_page(rp_b)
            back_writer.add_page(back_sheet)

        base_name = os.path.basename(input_pdf_path)
        front_pdf_path = os.path.join(TEMP_DIR, f"front_{base_name}")
        back_pdf_path = os.path.join(TEMP_DIR, f"back_{base_name}")

        with open(front_pdf_path, "wb") as f_out:
            front_writer.write(f_out)
        with open(back_pdf_path, "wb") as b_out:
            back_writer.write(b_out)

        return front_pdf_path, back_pdf_path, page_count

    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Corrupt or unreadable PDF file: {str(e)}")

def generate_duplex_booklet_pdf(front_pdf_path, back_pdf_path, base_name):
    """Interleaves the imposed sides so a duplex printer needs no manual flip."""
    front_reader = PdfReader(front_pdf_path)
    back_reader = PdfReader(back_pdf_path)
    writer = PdfWriter()
    for index in range(len(front_reader.pages)):
        writer.add_page(front_reader.pages[index])
        if index < len(back_reader.pages):
            writer.add_page(back_reader.pages[index])

    duplex_path = os.path.join(TEMP_DIR, f"duplex_{base_name}")
    with open(duplex_path, "wb") as out:
        writer.write(out)
    return duplex_path

def generate_two_sided_pdfs(input_pdf_path):
    """
    Splits a document into odd and even pages for the 'Duplex' mode on a printer
    without a duplex unit: the odd pages are printed first, the user puts the
    stack back and the even pages are printed on the other side.
    """
    reader = PdfReader(input_pdf_path)
    page_count = len(reader.pages)
    base_name = os.path.basename(input_pdf_path)

    front_writer = PdfWriter()
    back_writer = PdfWriter()
    for index in range(page_count):
        (front_writer if index % 2 == 0 else back_writer).add_page(reader.pages[index])

    front_path = os.path.join(TEMP_DIR, f"front_{base_name}")
    back_path = os.path.join(TEMP_DIR, f"back_{base_name}")
    with open(front_path, "wb") as out:
        front_writer.write(out)
    with open(back_path, "wb") as out:
        back_writer.write(out)
    return front_path, back_path, page_count

# ============================================================================
# 4. JOB CONTROL (CANCEL / REPRINT) AND HARDWARE DISPATCH
# ============================================================================
class JobCancelled(Exception):
    """Raised when the running job is cancelled by the user or a timeout."""

DEFAULT_FLIP_INSTRUCTION = ("Take the whole stack out of the output tray, do NOT rotate it, "
                            "and put it back in the paper tray printed side down, top edge first.")

def flip_instruction_for(target):
    """Per printer wording wins over the global one, which wins over the default."""
    return (target.get("flip_instruction")
            or config.get("hardware", {}).get("flip_instruction")
            or DEFAULT_FLIP_INSTRUCTION)

def flip_timeout_seconds():
    try:
        return max(0, int(config.get("hardware", {}).get("flip_timeout_seconds", 1800)))
    except (TypeError, ValueError):
        return 1800

def request_cancel():
    """Cancels the running job; also releases a pending flip wait."""
    global waiting_for_user_action
    with state_lock:
        job_control["cancel"] = True
    waiting_for_user_action = False

def request_reprint_front():
    """Prints the front side again, e.g. after a paper jam."""
    global waiting_for_user_action
    with state_lock:
        job_control["reprint_front"] = True
    waiting_for_user_action = False

def cancel_requested():
    with state_lock:
        return job_control["cancel"]

def take_reprint_request():
    with state_lock:
        requested = job_control["reprint_front"]
        job_control["reprint_front"] = False
    return requested

def reset_job_control():
    with state_lock:
        job_control["cancel"] = False
        job_control["reprint_front"] = False

def dispatch_via_socket(pdf_path, target, copies):
    """Raw JetDirect transfer on port 9100 (no feedback from the printer)."""
    printer_ip = target["host"]
    port = int(target.get("port", 9100))
    print(f"[Hardware] Direct TCP Socket transfer initiating to {printer_ip}:{port}")

    with open(pdf_path, 'rb') as f:
        data = f.read()

    for copy_index in range(copies):
        if cancel_requested():
            raise JobCancelled("Cancelled before the remaining copies were sent.")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(15.0)
                s.connect((printer_ip, port))
                s.sendall(data)
        except socket.timeout:
            raise ValueError(f"Connection to printer ({printer_ip}) timed out.")
        except ConnectionRefusedError:
            raise ValueError(f"Printer ({printer_ip}) refused the connection on port {port}.")
        except Exception as e:
            raise ValueError(f"Hardware dispatch failed: {str(e)}")
        if copies > 1:
            print(f"[Hardware] Copy {copy_index + 1}/{copies} sent.")
        time.sleep(2)

IPP_JOB_REQUEST = """{
    OPERATION Get-Job-Attributes
    GROUP operation-attributes-tag
    ATTR charset attributes-charset utf-8
    ATTR naturalLanguage attributes-natural-language en
    ATTR uri printer-uri $uri
    ATTR integer job-id $job-id
    ATTR keyword requested-attributes job-state,job-impressions-completed
}
"""

def ipp_request_file():
    path = os.path.join(TEMP_DIR, "get-job-attributes.test")
    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(IPP_JOB_REQUEST)
    return path

def cups_job_impressions(queue_name, job_id):
    """Real page level progress: asks CUPS how many sides it already printed."""
    if not shutil.which("ipptool"):
        return None
    try:
        result = subprocess.run(
            ["ipptool", "-d", f"job-id={job_id}", "-t",
             f"ipp://localhost/printers/{queue_name}", ipp_request_file()],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None

    for line in result.stdout.splitlines():
        if "job-impressions-completed" in line:
            digits = "".join(ch for ch in line.split("=")[-1] if ch.isdigit())
            if digits:
                return int(digits)
    return None

def dispatch_via_cups(pdf_path, target, copies, duplex, total_sheets, side, sides=None):
    """Sends the job through a local CUPS queue, so page progress is available."""
    queue_name = target.get("cups_queue") or target["id"]
    command = ["lp", "-d", queue_name, "-n", str(copies)]
    # An explicit 'sides' value (forced single/double sided) wins over the
    # booklet duplex default of short edge binding.
    if not sides and duplex:
        sides = "two-sided-short-edge"
    if sides:
        command += ["-o", f"sides={sides}"]
    command += ["-o", "media=A4", pdf_path]

    print(f"[Hardware] Submitting to CUPS queue '{queue_name}' (sides={sides or 'default'}).")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise ValueError("CUPS command 'lp' is not available on this host.")
    except subprocess.CalledProcessError as e:
        raise ValueError(f"CUPS refused the job: {(e.stderr or '').strip() or e}")

    job_id = None
    for word in (result.stdout or "").split():
        if "-" in word and word.rsplit("-", 1)[-1].isdigit():
            job_id = word.rsplit("-", 1)[-1]
            break

    if not job_id:
        print("[Hardware] Job submitted, but CUPS returned no job id; no progress available.")
        return

    expected = max(1, total_sheets * copies)
    while not shutdown_event.is_set():
        if cancel_requested():
            subprocess.run(["cancel", f"{queue_name}-{job_id}"], capture_output=True)
            raise JobCancelled("Cancelled while the printer was working.")

        done = cups_job_impressions(queue_name, job_id)
        if done is not None:
            set_state("PRINTING",
                      f"Sheet {min(done, expected)} of {expected}",
                      sheets_done=min(done, expected), side=side)

        active = subprocess.run(["lpstat", "-o", queue_name], capture_output=True, text=True)
        if f"{queue_name}-{job_id}" not in (active.stdout or ""):
            break
        time.sleep(2)

    print(f"[Hardware] CUPS job {queue_name}-{job_id} finished.")

def dispatch_to_printer_ipp(pdf_path, print_mode_name, target=None, side=None,
                            copies=1, duplex=False, total_sheets=0, sides=None):
    """
    Submits the document to the physical printer, either through a local CUPS
    queue (with page progress) or as a raw socket transfer on port 9100.
    The target printer is the personal choice from the web app, or the admin default.
    """
    target = target or default_printer()
    if cancel_requested():
        raise JobCancelled("Cancelled before printing started.")

    set_state("PRINTING", f"Dispatching {os.path.basename(pdf_path)} to {target['name']}",
              side=side, sheets_done=0, printer_id=target["id"], printer_name=target["name"])

    if target.get("dispatch") == "cups" and shutil.which("lp"):
        dispatch_via_cups(pdf_path, target, copies, duplex, total_sheets, side, sides)
    else:
        dispatch_via_socket(pdf_path, target, copies)

    print(f"[Hardware] Transfer complete for {print_mode_name}.")

# ============================================================================
# 5. CORE WORKFLOW
# ============================================================================
def wait_for_flip(filename, sheets, instruction):
    """
    Waits until the user confirms the paper is back in the tray. Returns
    'resume' or 'reprint'; raises JobCancelled on cancel, timeout or shutdown.
    """
    global waiting_for_user_action

    timeout = flip_timeout_seconds()
    deadline = time.time() + timeout if timeout else 0.0
    waiting_for_user_action = True
    set_state("WAITING_FOR_FLIP",
              f"Front side done. Put the {sheets} sheet(s) back in the tray and press Continue.",
              side="front", flip_instruction=instruction, flip_deadline=deadline)
    notify_flip_required(filename, sheets, instruction)

    while waiting_for_user_action and not shutdown_event.is_set():
        if deadline and time.time() > deadline:
            waiting_for_user_action = False
            raise JobCancelled(f"Nobody confirmed the flip within {timeout // 60} minutes.")
        time.sleep(0.5)

    if shutdown_event.is_set():
        raise JobCancelled("The service was stopped while waiting for the flip.")
    if cancel_requested():
        raise JobCancelled("Cancelled while waiting for the paper to be flipped.")
    if take_reprint_request():
        print("[System] Reprint of the front side requested.")
        return "reprint"
    return "resume"

def remove_quietly(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as e:
            print(f"[Warning] Could not remove {path}: {e}")

def process_print_job(filepath, intake=None):
    global waiting_for_user_action

    if not os.path.exists(filepath):
        return

    start_time = time.time()
    filename = os.path.basename(filepath)
    print(f"\n--- NEW PRINT JOB DETECTED ---")
    print(f"[File] {filename}")

    target, origin = resolve_target_printer()
    options = resolve_job_options()
    consume_pending_choice()
    consume_pending_options()
    reset_job_control()

    copies = options["copies"]
    # The queue the document arrived on is the choice the user made in the
    # print dialog, so it wins over the personal web app choice and the default.
    if intake and intake.get("print_mode") in PRINT_MODES:
        print_mode = intake["print_mode"]
        print(f"[Intake] Queue '{intake['cups_queue']}' forces mode {print_mode}.")
    else:
        print_mode = options["print_mode"]
    instruction = flip_instruction_for(target)
    # A real duplex printer prints both sides itself, but only through CUPS,
    # because the raw socket transfer cannot carry the 'sides' option.
    duplex = bool(target.get("duplex")) and target.get("dispatch") == "cups" and bool(shutil.which("lp"))
    if target.get("duplex") and not duplex:
        print("[Logic] Printer is marked duplex but is not reachable through CUPS; "
              "falling back to the manual flip workflow.")

    print(f"[Printers] Target: {target['name']} ({target['host']}:{target['port']}) [{origin}]")
    print(f"[Options] Mode: {print_mode}, copies: {copies}, duplex: {duplex}")

    set_state("PROCESSING", f"Analyzing {filename}", filename=filename, pages=0, sheets=0,
              sheets_done=0, side=None, copies=copies, print_mode=print_mode, duplex=duplex,
              printer_id=target["id"], printer_name=target["name"],
              intake_queue=intake["cups_queue"] if intake else None,
              flip_instruction=instruction, flip_deadline=0.0)

    front_pdf = back_pdf = duplex_pdf = None
    pages = sheets = 0

    try:
        pages = validate_pdf(filepath)

        if print_mode == "Booklet":
            print("[Logic] Booklet Mode. Generating imposed PDFs...")
            front_pdf, back_pdf, pages = generate_booklet_pdfs(filepath)
            sheets = ((pages + 3) // 4)
            set_state("PROCESSING", f"{pages} pages become {sheets} sheet(s)",
                      pages=pages, sheets=sheets)

            if duplex:
                duplex_pdf = generate_duplex_booklet_pdf(front_pdf, back_pdf, filename)
                print("[Job] Duplex printer: sending both sides in one job, no flip needed.")
                dispatch_to_printer_ipp(duplex_pdf, "Booklet-Duplex", target, side="both",
                                        copies=copies, duplex=True, total_sheets=sheets * 2)
            else:
                while True:
                    print("[Job 1] Dispatching Front Pages...")
                    dispatch_to_printer_ipp(front_pdf, "Booklet-Front", target, side="front",
                                            copies=copies, total_sheets=sheets)
                    if wait_for_flip(filename, sheets, instruction) == "resume":
                        break

                print("[Job 2] Dispatching Back Pages...")
                dispatch_to_printer_ipp(back_pdf, "Booklet-Back", target, side="back",
                                        copies=copies, total_sheets=sheets)

            proc_time_ms = int((time.time() - start_time) * 1000)
            publish_metrics(filename, pages, "Booklet", proc_time_ms)
            set_state("COMPLETED", f"Successfully processed {filename}", side=None)

        elif print_mode == "Duplex":
            # Forced double sided: no imposition, one page per side of a sheet.
            sheets = (pages + 1) // 2
            print("[Logic] Duplex Mode (forced double sided, no imposition).")
            set_state("PROCESSING", f"{pages} pages become {sheets} sheet(s)",
                      pages=pages, sheets=sheets)

            if duplex or pages < 2:
                dispatch_to_printer_ipp(filepath, "Duplex", target, side="both",
                                        copies=copies, duplex=duplex, total_sheets=pages,
                                        sides="two-sided-long-edge" if duplex else "one-sided")
            else:
                front_pdf, back_pdf, pages = generate_two_sided_pdfs(filepath)
                while True:
                    print("[Job 1] Dispatching the odd pages...")
                    dispatch_to_printer_ipp(front_pdf, "Duplex-Front", target, side="front",
                                            copies=copies, total_sheets=sheets,
                                            sides="one-sided")
                    if wait_for_flip(filename, sheets, instruction) == "resume":
                        break

                print("[Job 2] Dispatching the even pages...")
                dispatch_to_printer_ipp(back_pdf, "Duplex-Back", target, side="back",
                                        copies=copies, total_sheets=sheets,
                                        sides="one-sided")

            proc_time_ms = int((time.time() - start_time) * 1000)
            publish_metrics(filename, pages, "Duplex", proc_time_ms)
            set_state("COMPLETED", f"Successfully processed {filename}", side=None)

        elif print_mode == "Simplex":
            # Forced single sided: one page per sheet, never a flip.
            print("[Logic] Simplex Mode (forced single sided).")
            sheets = pages
            dispatch_to_printer_ipp(filepath, "Simplex", target, copies=copies,
                                    total_sheets=pages, sides="one-sided")
            set_state("COMPLETED", f"Processed and printed {filename}")

        else:
            print(f"[Logic] {print_mode} Mode")
            sheets = pages
            dispatch_to_printer_ipp(filepath, print_mode, target, copies=copies,
                                    total_sheets=pages)
            set_state("COMPLETED", f"Processed and printed {filename}")

        record_history(filename, pages, sheets, copies, print_mode, target["name"], "COMPLETED")
        if os.path.exists(filepath):
            os.remove(filepath)
            print(f"[System] Removed original file from drop directory.")

    except JobCancelled as jc:
        print(f"[System] Job cancelled: {jc}")
        set_state("CANCELLED", str(jc), side=None)
        record_history(filename, pages, sheets, copies, print_mode, target["name"],
                       "CANCELLED", str(jc))
        remove_quietly(filepath)

    except ValueError as ve:
        message = friendly_error(ve)
        print(f"[Error] Validation failed: {ve}")
        set_state("ERROR", message, side=None)
        record_history(filename, pages, sheets, copies, print_mode, target["name"],
                       "ERROR", message)
        notify_error(filename, message)
        try:
            error_path = os.path.join(ERROR_DIR, filename)
            shutil.move(filepath, error_path)
            print(f"[System] Moved faulty file to quarantine: {error_path}")
        except Exception as move_error:
            print(f"[Warning] Could not quarantine {filename}: {move_error}")

    except Exception as e:
        message = friendly_error(e)
        print(f"[Error] Fatal workflow exception: {e}")
        set_state("ERROR", message, side=None)
        record_history(filename, pages, sheets, copies, print_mode, target["name"],
                       "ERROR", message)
        notify_error(filename, message)

    finally:
        remove_quietly(front_pdf, back_pdf, duplex_pdf)
        waiting_for_user_action = False
        reset_job_control()
        set_state("IDLE", "Waiting for the next print job",
                  filename=None, pages=0, sheets=0, sheets_done=0, side=None,
                  intake_queue=None, flip_deadline=0.0)

# ============================================================================
# 6. DIRECTORY WATCHER
# ============================================================================
def wait_until_file_is_complete(filepath, timeout=120.0):
    """Waits until the file size stops growing (cups-pdf/PDFCreator still writing)."""
    deadline = time.time() + timeout
    last_size = -1
    while time.time() < deadline and not shutdown_event.is_set():
        try:
            size = os.path.getsize(filepath)
        except OSError:
            return False
        if size > 0 and size == last_size:
            return True
        last_size = size
        time.sleep(1.0)
    return os.path.exists(filepath)

class PrintFolderWatcher(FileSystemEventHandler):
    """Watches one drop directory; 'intake' fixes the print mode of that queue."""
    def __init__(self, intake=None):
        super().__init__()
        self.intake = intake

    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            if wait_until_file_is_complete(event.src_path):
                enqueue_print_job(event.src_path, self.intake)

def scan_directory(directory, intake=None):
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        print(f"[Warning] Could not scan {directory}: {e}")
        return
    for filename in names:
        path = os.path.join(directory, filename)
        if filename.lower().endswith(".pdf") and os.path.isfile(path):
            enqueue_print_job(path, intake)

def scan_existing_files():
    print("[System] Scanning for existing files in the drop directories...")
    scan_directory(DROP_DIR)
    for queue_entry in intake_queues():
        scan_directory(queue_entry["directory"], queue_entry)

# ============================================================================
# 6a. JOB QUEUE (ONE JOB AT A TIME, WITH A VISIBLE WAITING COUNT)
# ============================================================================
job_queue = queue.Queue()
queue_lock = threading.Lock()
queued_files = []
active_file = {"name": None}

def enqueue_print_job(filepath, intake=None):
    """Queues the file so a second document waits instead of interleaving."""
    name = os.path.basename(filepath)
    with queue_lock:
        queued_files.append(name)
    job_queue.put((filepath, intake))
    origin = f" via {intake['cups_queue']}" if intake else ""
    print(f"[Queue] Added '{name}'{origin} ({len(queued_files)} waiting).")

def pending_job_count():
    with queue_lock:
        return len(queued_files)

def queue_snapshot():
    with queue_lock:
        return {"active": active_file["name"], "waiting": list(queued_files)}

def job_worker():
    """Single worker so only one document is printed at a time."""
    while not shutdown_event.is_set():
        try:
            filepath, intake = job_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        name = os.path.basename(filepath)
        with queue_lock:
            if name in queued_files:
                queued_files.remove(name)
            active_file["name"] = name
        try:
            process_print_job(filepath, intake)
        except Exception as e:
            print(f"[Error] Unhandled error while printing {name}: {e}")
        finally:
            with queue_lock:
                active_file["name"] = None
            job_queue.task_done()

# ============================================================================
# 6b. MOBILE WEB APP (STATUS, FLIP CONFIRMATION, PRINTER SELECTION)
# ============================================================================
WEB_PAGE = """<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__TITLE__">
<meta name="theme-color" content="#1f2933">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon.svg">
<title>__TITLE__</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px env(safe-area-inset-right) 32px env(safe-area-inset-left);
       font: 17px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       background: #f4f5f7; color: #1f2933; }
@media (prefers-color-scheme: dark) { body { background: #14181c; color: #e6e9ec; }
  .card { background: #1f262c !important; } select { background: #14181c; color: #e6e9ec; } }
h1 { font-size: 20px; margin: 4px 0 16px; }
.card { background: #fff; border-radius: 14px; padding: 16px; margin-bottom: 14px;
        box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.state { font-size: 22px; font-weight: 600; }
.dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%;
       margin-right: 8px; background: #7b8794; }
.idle .dot { background: #7b8794; } .busy .dot { background: #2f80ed; }
.wait .dot { background: #f2994a; } .ok .dot { background: #27ae60; }
.err .dot { background: #eb5757; }
.muted { color: #7b8794; font-size: 14px; }
button { width: 100%; min-height: 56px; font-size: 19px; font-weight: 600;
         border: 0; border-radius: 12px; background: #2f80ed; color: #fff; }
button.flip { background: #f2994a; min-height: 72px; font-size: 21px; }
button:disabled { background: #c7ccd1; color: #fff; }
button.ghost { background: transparent; color: #2f80ed; border: 1px solid #c7ccd1;
               min-height: 48px; font-size: 17px; margin-top: 8px; }
button.danger { background: transparent; color: #eb5757; border: 1px solid #eb5757;
                min-height: 48px; font-size: 17px; margin-top: 8px; }
select { width: 100%; min-height: 48px; font-size: 17px; border-radius: 10px;
         padding: 8px; border: 1px solid #c7ccd1; margin-bottom: 10px; }
.row { display: flex; justify-content: space-between; padding: 4px 0; }
.bar { height: 8px; border-radius: 4px; background: #e6e9ec; margin: 12px 0 4px;
       overflow: hidden; }
.bar > div { height: 100%; width: 0; background: #2f80ed; transition: width .4s; }
.flipimg { width: 100%; height: 84px; margin: 6px 0 10px; }
ul.hist { list-style: none; margin: 0; padding: 0; font-size: 15px; }
ul.hist li { display: flex; justify-content: space-between; gap: 8px;
             padding: 6px 0; border-top: 1px solid rgba(123,135,148,.25); }
ul.hist li:first-child { border-top: 0; }
.ok-t { color: #27ae60; } .err-t { color: #eb5757; } .cancel-t { color: #7b8794; }
footer { text-align: center; font-size: 13px; color: #7b8794; margin-top: 8px; }
footer a { color: #7b8794; }
</style>
</head>
<body>
<h1>__TITLE__</h1>

<div class="card" id="statusCard">
  <div class="state"><span class="dot"></span><span id="state">...</span></div>
  <div class="bar"><div id="barFill"></div></div>
  <p id="detail" class="muted"></p>
  <div class="row"><span class="muted" id="lblFile"></span><span id="file">-</span></div>
  <div class="row"><span class="muted" id="lblPages"></span><span id="pages">-</span></div>
  <div class="row"><span class="muted" id="lblPrinter"></span><span id="printer">-</span></div>
  <div class="row"><span class="muted" id="lblQueue"></span><span id="queue">0</span></div>
  <p class="muted" id="updated"></p>
</div>

<div class="card" id="flipCard">
  <div id="flipHelp" class="muted"></div>
  <svg class="flipimg" viewBox="0 0 240 84" aria-hidden="true">
    <rect x="12" y="16" width="62" height="52" rx="4" fill="#fff" stroke="#7b8794"/>
    <line x1="22" y1="28" x2="64" y2="28" stroke="#2f80ed" stroke-width="3"/>
    <line x1="22" y1="38" x2="64" y2="38" stroke="#2f80ed" stroke-width="3"/>
    <line x1="22" y1="48" x2="52" y2="48" stroke="#2f80ed" stroke-width="3"/>
    <path d="M92 42 h44" stroke="#f2994a" stroke-width="4"/>
    <path d="M132 34 l10 8 l-10 8 z" fill="#f2994a"/>
    <rect x="158" y="16" width="62" height="52" rx="4" fill="#e6e9ec" stroke="#7b8794"/>
    <path d="M158 16 l16 0 l-16 16 z" fill="#7b8794"/>
    <text x="18" y="80" font-size="11" fill="#7b8794">printed</text>
    <text x="158" y="80" font-size="11" fill="#7b8794">face down, top first</text>
  </svg>
  <button class="flip" id="resume" disabled></button>
  <button class="ghost" id="reprint"></button>
  <button class="danger" id="cancel"></button>
</div>

<div class="card">
  <div class="muted" id="lblOptions"></div>
  <select id="choice"></select>
  <select id="mode"></select>
  <select id="copies"></select>
  <button id="apply"></button>
  <p class="muted" id="choiceInfo"></p>
</div>

<div class="card">
  <div class="muted" id="lblQueues"></div>
  <ul class="hist" id="queues"></ul>
  <p class="muted" id="queuesHint"></p>
</div>

<div class="card">
  <div class="muted" id="lblHistory"></div>
  <ul class="hist" id="history"></ul>
</div>

<footer>__TITLE__ <span id="version"></span> &middot; <a href="/qr">QR</a></footer>

<script>
var T = __STRINGS__;
var cls = {IDLE:"idle", STARTING:"idle", PROCESSING:"busy", PRINTING:"busy",
           WAITING_FOR_FLIP:"wait", COMPLETED:"ok", CANCELLED:"idle",
           ERROR:"err", OFFLINE:"idle"};
var byId = function (id) { return document.getElementById(id); };
var touched = false;
["choice", "mode", "copies"].forEach(function (id) {
  byId(id).addEventListener("change", function () { touched = true; });
});

byId("lblFile").textContent = T.document;
byId("lblPages").textContent = T.pagesSheets;
byId("lblPrinter").textContent = T.printingOn;
byId("lblQueue").textContent = T.queued;
byId("lblOptions").textContent = T.optionsTitle;
byId("lblQueues").textContent = T.queuesTitle;
byId("queuesHint").textContent = T.queuesHint;
byId("lblHistory").textContent = T.historyTitle;
byId("resume").textContent = T.continueButton;
byId("reprint").textContent = T.reprintButton;
byId("cancel").textContent = T.cancelButton;
byId("apply").textContent = T.applyButton;

var copiesSelect = byId("copies");
for (var n = 1; n <= 10; n++) {
  var co = document.createElement("option");
  co.value = String(n);
  co.textContent = n === 1 ? T.oneCopy : n + T.copiesSuffix;
  copiesSelect.appendChild(co);
}
["Booklet", "Duplex", "Simplex", "Standard", "Bypass"].forEach(function (m) {
  var mo = document.createElement("option");
  mo.value = m;
  mo.textContent = T.modes[m] || m;
  byId("mode").appendChild(mo);
});

function renderQueues(queues) {
  var list = byId("queues");
  list.innerHTML = "";
  if (!queues || !queues.length) {
    byId("lblQueues").parentNode.hidden = true;
    return;
  }
  queues.forEach(function (q) {
    var li = document.createElement("li");
    var left = document.createElement("span");
    left.textContent = q.cups_queue;
    var right = document.createElement("span");
    right.className = "muted";
    right.textContent = T.modes[q.print_mode] || q.print_mode;
    li.appendChild(left);
    li.appendChild(right);
    list.appendChild(li);
  });
}

function renderHistory(entries) {
  var list = byId("history");
  list.innerHTML = "";
  if (!entries || !entries.length) {
    var empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = T.noHistory;
    list.appendChild(empty);
    return;
  }
  entries.forEach(function (e) {
    var li = document.createElement("li");
    var left = document.createElement("span");
    left.textContent = e.filename + " (" + e.pages + "p)";
    var right = document.createElement("span");
    right.className = e.result === "COMPLETED" ? "ok-t"
        : (e.result === "ERROR" ? "err-t" : "cancel-t");
    right.textContent = (T.results[e.result] || e.result) + " " +
        (e.timestamp || "").replace("T", " ").slice(5, 16);
    li.appendChild(left);
    li.appendChild(right);
    list.appendChild(li);
  });
}

function render(s) {
  byId("state").textContent = T.states[s.state] || s.state;
  byId("statusCard").className = "card " + (cls[s.state] || "idle");
  byId("detail").textContent = s.detail || "";
  byId("file").textContent = s.filename || "-";
  byId("pages").textContent = s.pages ? s.pages + T.pagesUnit + s.sheets + T.sheetsUnit : "-";
  byId("printer").textContent = s.effective_printer_name || "-";
  byId("queue").textContent = s.queued;
  byId("updated").textContent = T.updated + " " + new Date().toLocaleTimeString();
  byId("version").textContent = "v" + s.version;

  var total = (s.sheets || 0) * (s.copies || 1) * (s.duplex ? 2 : 1);
  var ratio = total ? Math.min(100, Math.round((s.sheets_done / total) * 100)) : 0;
  if (s.state === "COMPLETED") { ratio = 100; }
  byId("barFill").style.width = ratio + "%";

  byId("resume").disabled = !s.waiting_for_flip;
  byId("flipCard").hidden = !(s.waiting_for_flip || s.busy);
  byId("reprint").hidden = !s.waiting_for_flip;
  byId("cancel").hidden = !s.busy;
  byId("flipHelp").textContent = s.waiting_for_flip ? (s.flip_instruction || "") : "";
  var svg = document.querySelector(".flipimg");
  if (svg) { svg.style.display = s.waiting_for_flip ? "block" : "none"; }

  var sel = byId("choice");
  if (sel.options.length !== s.printers.length) {
    sel.innerHTML = "";
    s.printers.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p.id;
      o.textContent = p.name + (p.id === s.default_printer_id ? T.defaultSuffix : "")
          + (p.duplex ? T.duplexSuffix : "");
      sel.appendChild(o);
    });
  }
  if (!touched) {
    sel.value = s.my_printer_id || s.default_printer_id;
    byId("mode").value = s.my_print_mode || s.print_mode_default;
    byId("copies").value = String(s.my_copies || 1);
  }
  byId("choiceInfo").textContent = s.personal_active
      ? T.choiceActive.replace("%s", s.personal_expires_in)
      : T.choiceInactive;
  renderQueues(s.intake);
  renderHistory(s.history);
}

function poll() {
  fetch("/api/status", {cache: "no-store"}).then(function (r) { return r.json(); })
    .then(render).catch(function () {});
}

function post(url, body) {
  return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify(body || {})}).then(poll);
}

byId("resume").onclick = function () { post("/api/resume"); };
byId("reprint").onclick = function () { post("/api/reprint"); };
byId("cancel").onclick = function () {
  if (confirm(T.confirmCancel)) { post("/api/cancel"); }
};
byId("apply").onclick = function () {
  touched = false;
  post("/api/options", {printer: byId("choice").value,
                        print_mode: byId("mode").value,
                        copies: parseInt(byId("copies").value, 10)});
};

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""

APP_ICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
            "<rect width='64' height='64' rx='12' fill='#2f80ed'/>"
            "<rect x='16' y='12' width='32' height='16' rx='2' fill='#fff'/>"
            "<rect x='12' y='28' width='40' height='18' rx='3' fill='#e6e9ec'/>"
            "<rect x='18' y='40' width='28' height='14' rx='2' fill='#fff'/></svg>")

WEB_STRINGS = {
    "en": {
        "document": "Document",
        "pagesSheets": "Pages / sheets",
        "printingOn": "Printing on",
        "queued": "Waiting jobs",
        "optionsTitle": "My settings for the next print job",
        "queuesTitle": "Printers you can choose in the print dialog",
        "queuesHint": "Pick one of these printers on your device; the printer you "
                      "choose decides how the document is printed.",
        "historyTitle": "Last print jobs",
        "noHistory": "No print jobs yet.",
        "continueButton": "Paper re-inserted - CONTINUE",
        "reprintButton": "Print the front side again",
        "cancelButton": "Cancel this print job",
        "applyButton": "Use these settings",
        "confirmCancel": "Cancel the current print job?",
        "oneCopy": "1 copy",
        "copiesSuffix": " copies",
        "pagesUnit": " pages / ",
        "sheetsUnit": " sheets",
        "updated": "updated",
        "defaultSuffix": " (default)",
        "duplexSuffix": " - duplex, no flip",
        "choiceActive": "Your settings are used for the next job (%s s left).",
        "choiceInactive": "Without a choice the administrator settings are used.",
        "modes": {"Booklet": "Booklet (A5, fold in the middle)",
                  "Duplex": "Double sided (front and back)",
                  "Simplex": "Single sided (one page per sheet)",
                  "Standard": "Standard (one side per sheet)",
                  "Bypass": "Bypass (send as it is)"},
        "states": {"IDLE": "Ready", "STARTING": "Starting",
                   "PROCESSING": "Preparing booklet", "PRINTING": "Printing",
                   "WAITING_FOR_FLIP": "Waiting for you to flip the paper",
                   "COMPLETED": "Done", "CANCELLED": "Cancelled",
                   "ERROR": "Something went wrong", "OFFLINE": "Service stopped"},
        "results": {"COMPLETED": "done", "ERROR": "failed", "CANCELLED": "cancelled"}
    },
    "nl": {
        "document": "Document",
        "pagesSheets": "Pagina's / bladen",
        "printingOn": "Printen op",
        "queued": "Wachtende opdrachten",
        "optionsTitle": "Mijn instellingen voor de volgende printopdracht",
        "queuesTitle": "Printers die je in het printvenster kunt kiezen",
        "queuesHint": "Kies een van deze printers op je apparaat; de printer die je "
                      "kiest bepaalt hoe het document wordt geprint.",
        "historyTitle": "Laatste printopdrachten",
        "noHistory": "Nog geen printopdrachten.",
        "continueButton": "Papier teruggelegd - DOORGAAN",
        "reprintButton": "Voorkant opnieuw printen",
        "cancelButton": "Deze opdracht annuleren",
        "applyButton": "Deze instellingen gebruiken",
        "confirmCancel": "De huidige printopdracht annuleren?",
        "oneCopy": "1 exemplaar",
        "copiesSuffix": " exemplaren",
        "pagesUnit": " pagina's / ",
        "sheetsUnit": " bladen",
        "updated": "bijgewerkt",
        "defaultSuffix": " (standaard)",
        "duplexSuffix": " - dubbelzijdig, niet omdraaien",
        "choiceActive": "Jouw instellingen gelden voor de volgende opdracht (nog %s s).",
        "choiceInactive": "Zonder keuze worden de instellingen van de beheerder gebruikt.",
        "modes": {"Booklet": "Boekje (A5, in het midden vouwen)",
                  "Duplex": "Dubbelzijdig (voor- en achterkant)",
                  "Simplex": "Enkelzijdig (\u00e9\u00e9n pagina per blad)",
                  "Standard": "Standaard (\u00e9\u00e9n pagina per blad)",
                  "Bypass": "Ongewijzigd doorsturen"},
        "states": {"IDLE": "Gereed", "STARTING": "Opstarten",
                   "PROCESSING": "Boekje voorbereiden", "PRINTING": "Aan het printen",
                   "WAITING_FOR_FLIP": "Wacht tot je het papier omdraait",
                   "COMPLETED": "Klaar", "CANCELLED": "Geannuleerd",
                   "ERROR": "Er ging iets mis", "OFFLINE": "Service gestopt"},
        "results": {"COMPLETED": "klaar", "ERROR": "mislukt", "CANCELLED": "geannuleerd"}
    }
}

def web_language():
    language = str(config.get("web", {}).get("language", "en")).lower()[:2]
    return language if language in WEB_STRINGS else "en"

def render_web_page():
    language = web_language()
    return (WEB_PAGE.replace("__TITLE__", web_title())
                    .replace("__LANG__", language)
                    .replace("__STRINGS__", json.dumps(WEB_STRINGS[language])))

QR_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>body { font: 17px -apple-system, "Segoe UI", Roboto, sans-serif; text-align: center;
  padding: 24px; } code { font-size: 20px; } img, svg { margin: 20px auto; display: block; }
</style></head><body>
<h1>__TITLE__</h1>
<p>Scan this code or open the address on your phone:</p>
__QR__
<p><code>__URL__</code></p>
<p><a href="/">Back to the print status</a></p>
</body></html>
"""

def render_qr_page():
    """QR code for the web app URL; falls back to the plain address."""
    url = public_base_url() + "/"
    markup = "<p>(install the optional 'segno' package to get a QR code here)</p>"
    try:
        import segno
        buffer = io.StringIO()
        segno.make(url, error="m").save(buffer, kind="svg", scale=6, border=2)
        markup = buffer.getvalue()
    except Exception:
        pass
    return (QR_PAGE.replace("__TITLE__", web_title())
                   .replace("__QR__", markup)
                   .replace("__URL__", url))

def web_title():
    return config.get("web", {}).get("title", "Wols CA Booklet Printer")

def status_snapshot(token):
    """State shown in the web app, including the resolved target printer."""
    with state_lock:
        snapshot = dict(job_state)
        my_printer_id = personal_choices.get(token)
        pending_id = pending_choice["printer_id"]
        pending_expires = pending_choice["expires"]

    effective, origin = resolve_target_printer()
    personal_active = bool(pending_id) and time.time() < pending_expires

    my_options = personal_options.get(token, {})
    queue_info = queue_snapshot()
    busy = snapshot["state"] in ("PROCESSING", "PRINTING", "WAITING_FOR_FLIP")

    return {
        "version": SERVICE_VERSION,
        "state": snapshot["state"],
        "detail": snapshot["detail"],
        "filename": snapshot["filename"],
        "pages": snapshot["pages"],
        "sheets": snapshot["sheets"],
        "sheets_done": snapshot["sheets_done"],
        "side": snapshot["side"],
        "copies": snapshot["copies"],
        "duplex": snapshot["duplex"],
        "busy": busy,
        "waiting_for_flip": snapshot["waiting_for_flip"],
        "flip_instruction": snapshot["flip_instruction"],
        "flip_seconds_left": int(max(0, snapshot["flip_deadline"] - time.time()))
                             if snapshot["flip_deadline"] else 0,
        "updated": snapshot["updated"],
        "printers": printer_targets(),
        "default_printer_id": default_printer()["id"],
        "my_printer_id": my_printer_id,
        "my_copies": my_options.get("copies"),
        "my_print_mode": my_options.get("print_mode"),
        "effective_printer_id": effective["id"],
        "effective_printer_name": snapshot["printer_name"] or effective["name"],
        "effective_source": origin,
        "personal_active": personal_active,
        "personal_expires_in": int(max(0, pending_expires - time.time())) if personal_active else 0,
        "print_mode": snapshot["print_mode"] or config["settings"]["print_mode"],
        "print_mode_default": config["settings"]["print_mode"],
        "queued": len(queue_info["waiting"]),
        "queue": queue_info,
        "intake": intake_queues(),
        "intake_queue": snapshot["intake_queue"],
        "history": history_entries()
    }

class WebAppHandler(BaseHTTPRequestHandler):
    server_version = "WolsCAPrintService"

    def log_message(self, fmt, *args):
        pass  # keep the journal readable

    # -- helpers ------------------------------------------------------------
    def client_token(self):
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "wolsca_client" and value:
                return value, False
        return secrets.token_hex(8), True

    def send_payload(self, body, content_type, set_cookie=None, status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie",
                             f"wolsca_client={set_cookie}; Path=/; Max-Age=31536000; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload, set_cookie=None, status=200):
        self.send_payload(json.dumps(payload), "application/json; charset=utf-8",
                          set_cookie, status)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return {}
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {key: values[0] for key, values in parse_qs(raw).items()}

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        token, is_new = self.client_token()
        cookie = token if is_new else None
        path = urlparse(self.path).path

        if path == "/":
            self.send_payload(render_web_page(), "text/html; charset=utf-8", cookie)
        elif path == "/qr":
            self.send_payload(render_qr_page(), "text/html; charset=utf-8", cookie)
        elif path == "/api/status":
            self.send_json(status_snapshot(token), cookie)
        elif path == "/api/history":
            self.send_json({"history": history_entries()}, cookie)
        elif path == "/api/printers":
            self.send_json({"printers": printer_targets(),
                            "default": default_printer()["id"]}, cookie)
        elif path == "/api/queues":
            self.send_json({"queues": intake_queues()}, cookie)
        elif path == "/manifest.webmanifest":
            manifest = {
                "name": web_title(),
                "short_name": "Booklet",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#f4f5f7",
                "theme_color": "#1f2933",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]
            }
            self.send_payload(json.dumps(manifest), "application/manifest+json", cookie)
        elif path == "/icon.svg":
            self.send_payload(APP_ICON, "image/svg+xml", cookie)
        elif path == "/healthz":
            self.send_payload("ok", "text/plain; charset=utf-8")
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        global waiting_for_user_action
        token, is_new = self.client_token()
        cookie = token if is_new else None
        path = urlparse(self.path).path
        body = self.read_body()

        if path == "/api/resume":
            if waiting_for_user_action:
                print("[Web] 'CONTINUE' pressed in the web app. Resuming workflow...")
                waiting_for_user_action = False
            self.send_json(status_snapshot(token), cookie)

        elif path == "/api/cancel":
            print("[Web] 'CANCEL' pressed in the web app.")
            request_cancel()
            self.send_json(status_snapshot(token), cookie)

        elif path == "/api/reprint":
            print("[Web] 'REPRINT FRONT' pressed in the web app.")
            request_reprint_front()
            self.send_json(status_snapshot(token), cookie)

        elif path == "/api/printer":
            target = set_personal_printer(token, str(body.get("printer", "")))
            if not target:
                self.send_json({"error": "unknown printer"}, cookie, status=400)
                return
            self.send_json(status_snapshot(token), cookie)

        elif path == "/api/options":
            if "printer" in body:
                if not set_personal_printer(token, str(body.get("printer", ""))):
                    self.send_json({"error": "unknown printer"}, cookie, status=400)
                    return
            copies = body.get("copies")
            print_mode = body.get("print_mode")
            try:
                copies = int(copies) if copies is not None else None
            except (TypeError, ValueError):
                self.send_json({"error": "copies must be a number"}, cookie, status=400)
                return
            if print_mode is not None and print_mode not in PRINT_MODES:
                self.send_json({"error": "unknown print mode"}, cookie, status=400)
                return
            set_personal_options(token, copies=copies, print_mode=print_mode)
            self.send_json(status_snapshot(token), cookie)

        elif path == "/api/default":
            admin_token = config.get("web", {}).get("admin_token", "")
            if not admin_token or body.get("token") != admin_token:
                self.send_json({"error": "forbidden"}, cookie, status=403)
                return
            target = set_default_printer(str(body.get("printer", "")))
            if not target:
                self.send_json({"error": "unknown printer"}, cookie, status=400)
                return
            self.send_json(status_snapshot(token), cookie)

        else:
            self.send_json({"error": "not found"}, status=404)

def start_web_app():
    """Starts the phone friendly status/resume/printer-choice web app."""
    web_config = config.get("web", {})
    if not web_config.get("enabled", True):
        print("[Web] Web app disabled by configuration.")
        return None

    address = web_config.get("bind_address", "0.0.0.0")
    port = int(web_config.get("port", 8080))

    try:
        httpd = ThreadingHTTPServer((address, port), WebAppHandler)
    except OSError as e:
        print(f"[Error] Could not start the web app on {address}:{port}: {e}")
        return None

    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[Web] Status page available on http://{socket.gethostname()}.local:{port}/ "
          f"(listening on {address}:{port})")
    return httpd

# ============================================================================
# 7. ZERO-TOUCH DEPLOYMENT (AUTO-DOWNLOAD & INSTALL)
# ============================================================================
def check_virtual_printer():
    """Verifies that the virtual (job intake) printer exists on this host."""
    if IS_LINUX:
        check_cups_queue()
        return
    if not IS_WINDOWS:
        return

    printer_name = config["virtual_printer"]["name"]
    result = subprocess.run(["powershell", "-Command", f"Get-Printer -Name '{printer_name}' -ErrorAction SilentlyContinue"], capture_output=True, text=True)
    
    if not result.stdout.strip():
        print(f"[Zero-Touch] Virtual printer '{printer_name}' not found.")
        print("[Zero-Touch] Requesting Administrator privileges for fully automated deployment...")
        
        script_path = os.path.abspath(__file__)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script_path}" --install-printer', None, 1)

def check_cups_queue():
    """Linux: verifies that the cups-pdf backed queue is present."""
    queue = config["virtual_printer"].get("cups_queue_name", "WolsCA_Booklet")

    if not shutil.which("lpstat"):
        print("[Zero-Touch] CUPS is not installed. Run the service once with "
              "'--install-printer' as root to deploy cups-pdf.")
        return

    expected = [entry["cups_queue"] for entry in intake_queues()] or [queue]
    for queue_name in expected:
        result = subprocess.run(["lpstat", "-p", queue_name], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[Zero-Touch] CUPS queue '{queue_name}' not found.")
            print("[Zero-Touch] Run 'sudo <python> Wols_CA_PrintService.py "
                  "--install-printer' to create it.")
        else:
            print(f"[Zero-Touch] CUPS queue '{queue_name}' is available.")

def run_root_command(args, description):
    """Runs a privileged setup command and reports the outcome."""
    print(f"[Admin] {description}")
    try:
        subprocess.run(args, check=True)
        return True
    except FileNotFoundError:
        print(f"[Error] Command not found: {args[0]}")
    except subprocess.CalledProcessError as e:
        print(f"[Error] {description} failed (exit code {e.returncode}).")
    return False

def configure_cups_pdf(drop_dir, conf_path="/etc/cups/cups-pdf.conf", template=None):
    """Points a cups-pdf instance at its drop directory instead of the home folder."""
    if not os.path.exists(conf_path):
        source = template or "/etc/cups/cups-pdf.conf"
        if os.path.exists(source):
            shutil.copyfile(source, conf_path)
        else:
            print(f"[Warning] {conf_path} not found, skipping cups-pdf configuration.")
            return

    overrides = {
        "Out": drop_dir,
        "AnonDirName": drop_dir,
        "Grp": "lp",
        "UserUMask": "0000",
        "Truncate": "64",
        "PostProcessing": ""
    }

    try:
        with open(conf_path, 'r') as f:
            lines = f.readlines()

        cleaned = []
        for line in lines:
            key = line.strip().lstrip("#").split(" ")[0] if line.strip() else ""
            if key in overrides and not line.strip().startswith("#"):
                continue
            cleaned.append(line)

        cleaned.append("\n### Wols CA Print Service ###\n")
        for key, value in overrides.items():
            cleaned.append(f"{key} {value}\n" if value else f"{key}\n")

        with open(conf_path, 'w') as f:
            f.writelines(cleaned)
        print(f"[Admin] cups-pdf now writes into {drop_dir}.")
    except Exception as e:
        print(f"[Warning] Could not update {conf_path}: {e}")

def find_cups_pdf_ppd():
    """The cups-pdf PPD location differs between Debian and Ubuntu releases."""
    candidates = [
        "/usr/share/ppd/cups-pdf/CUPS-PDF_opt.ppd",
        "/usr/share/ppd/cups-pdf/CUPS-PDF_noopt.ppd",
        "/usr/share/cups/model/CUPS-PDF_opt.ppd",
        "/usr/share/cups/model/CUPS-PDF_noopt.ppd",
        "/usr/share/ppd/cups-pdf/CUPS-PDF.ppd",
        "/usr/share/cups/model/CUPS-PDF.ppd"
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    print("[Warning] No cups-pdf PPD found; falling back to the driverless 'everywhere' model.")
    return None

def cups_backend_dir():
    """The CUPS backend directory differs between distributions."""
    for path in ("/usr/lib/cups/backend", "/usr/libexec/cups/backend"):
        if os.path.isdir(path):
            return path
    return None

def ensure_cups_pdf_instance(queue_entry):
    """
    Creates a private cups-pdf backend instance for one intake queue.
    cups-pdf reads /etc/cups/cups-pdf-<suffix>.conf when it is called as
    'cups-pdf-<suffix>', which is what gives every queue its own directory.
    Returns the device URI to use for the queue.
    """
    backend_dir = cups_backend_dir()
    base_backend = os.path.join(backend_dir, "cups-pdf") if backend_dir else None
    directory = queue_entry["directory"]

    os.makedirs(directory, exist_ok=True)
    # setgid + group writable: cups-pdf (root/lp) and the service user share it.
    os.chmod(directory, 0o2775)

    if not base_backend or not os.path.exists(base_backend):
        print("[Warning] cups-pdf backend not found; using the shared output directory.")
        return "cups-pdf:/"

    suffix = queue_entry["id"]
    instance = os.path.join(backend_dir, f"cups-pdf-{suffix}")
    try:
        if not os.path.exists(instance):
            os.symlink("cups-pdf", instance)
        configure_cups_pdf(directory, f"/etc/cups/cups-pdf-{suffix}.conf")
        print(f"[Admin] cups-pdf instance '{suffix}' writes into {directory}.")
        return f"cups-pdf-{suffix}:/"
    except Exception as e:
        print(f"[Warning] Could not create the cups-pdf instance '{suffix}': {e}")
        return "cups-pdf:/"

def create_intake_queue(queue_entry, share):
    """Publishes one visible, driverless queue for a single print mode."""
    queue_name = queue_entry["cups_queue"]
    device_uri = ensure_cups_pdf_instance(queue_entry)
    ppd = find_cups_pdf_ppd()

    lpadmin_args = ["lpadmin", "-p", queue_name, "-v", device_uri, "-E"]
    lpadmin_args += ["-P", ppd] if ppd else ["-m", "everywhere"]
    lpadmin_args += [
        "-o", "printer-is-shared=" + ("true" if share else "false"),
        # Advertise the queue as driverless/AirPrint capable for phones and tablets.
        "-o", "document-format-default=application/pdf",
        "-D", queue_entry["description"],
        "-L", "Wols CA Print Service"
    ]
    run_root_command(lpadmin_args, f"Creating CUPS queue '{queue_name}'")
    run_root_command(["cupsenable", queue_name], f"Enabling queue '{queue_name}'")
    run_root_command(["cupsaccept", queue_name], f"Accepting jobs on '{queue_name}'")

def advertise_web_app_over_mdns():
    """Publishes the web app as _http._tcp so phones find http://<host>.local:<port>/."""
    if not shutil.which("avahi-daemon"):
        print("[Warning] avahi-daemon not installed; skipping mDNS advertising.")
        return

    port = int(config.get("web", {}).get("port", 8080))
    service_file = "/etc/avahi/services/wolsca-print-web.service"
    content = f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Wols CA Booklet Printer on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>{port}</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
"""
    try:
        os.makedirs(os.path.dirname(service_file), exist_ok=True)
        with open(service_file, "w") as f:
            f.write(content)
        run_root_command(["systemctl", "enable", "--now", "avahi-daemon"],
                         "Enabling the Avahi (Bonjour/mDNS) daemon")
        run_root_command(["systemctl", "reload-or-restart", "avahi-daemon"],
                         "Publishing the web app over mDNS")
        print(f"[Admin] Web app advertised as http://{socket.gethostname()}.local:{port}/")
    except Exception as e:
        print(f"[Warning] Could not write {service_file}: {e}")

def perform_cups_printer_install():
    """Linux: installs CUPS + cups-pdf and publishes the intake queue."""
    print("\n===================================================")
    print("  Wols CA CUPS Intake Printer Deployment Started    ")
    print("===================================================\n")

    if os.geteuid() != 0:
        print("[Error] Root privileges are required. Re-run with sudo.")
        sys.exit(1)

    queue = config["virtual_printer"].get("cups_queue_name", "WolsCA_Booklet")
    share = config["virtual_printer"].get("cups_share_on_network", True)
    drop_dir = config["paths"]["drop_directory"]

    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    print("[Admin] 1/5: Installing CUPS, cups-pdf and Avahi (Debian/Ubuntu)...")
    try:
        subprocess.run(["apt-get", "update"], check=True, env=env)
        subprocess.run(["apt-get", "install", "-y", "cups", "cups-pdf",
                        "cups-ipp-utils", "avahi-daemon", "avahi-utils"],
                       check=True, env=env)
    except Exception as e:
        print(f"[Error] Package installation failed: {e}")
        sys.exit(1)

    print("[Admin] 2/5: Configuring cups-pdf output directory...")
    os.makedirs(drop_dir, exist_ok=True)
    # setgid + group writable: cups-pdf (root/lp) and the service user share the folder.
    os.chmod(drop_dir, 0o2775)
    configure_cups_pdf(drop_dir)

    queues = intake_queues()
    if queues:
        print("[Admin] 3/5: Creating one visible queue per print mode...")
        for queue_entry in queues:
            create_intake_queue(queue_entry, share)
    else:
        print("[Admin] 3/5: Creating the single intake queue...")
        create_intake_queue({"id": "booklet", "cups_queue": queue,
                             "description": "Wols CA Booklet Intake",
                             "print_mode": "Booklet", "directory": drop_dir}, share)

    if share:
        print("[Admin] 4/5: Publishing the queue on the local network...")
        run_root_command(["cupsctl", "--share-printers", "--remote-any"],
                         "Enabling network sharing")
        run_root_command(["systemctl", "enable", "--now", "avahi-daemon"],
                         "Enabling Bonjour/AirPrint announcements")
        run_root_command(["systemctl", "restart", "cups"], "Restarting CUPS")
    else:
        print("[Admin] 4/5: Network sharing disabled by configuration.")

    print("[Admin] 5/5: Advertising the web app over mDNS...")
    advertise_web_app_over_mdns()

    host = socket.gethostname()
    web_port = int(config.get("web", {}).get("port", 8080))
    print(f"\n[Admin] Deployment complete.")
    for queue_entry in (queues or [{"cups_queue": queue, "print_mode": "Booklet"}]):
        print(f"  Print to : ipp://{host}.local:631/printers/{queue_entry['cups_queue']}"
              f"  ({queue_entry['print_mode']})")
    print(f"  Web app  : http://{host}.local:{web_port}/")
    sys.exit(0)

def download_installer(url, dest_path):
    """Automates the download of the required installer package."""
    print(f"[Network] Downloading dependencies from: {url}")
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        # Create unverified context to prevent issues on rigid corporate firewalls/old root certs
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(url, context=ctx) as response, open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
        print("[Network] Download completed successfully.")
        return True
    except Exception as e:
        print(f"[Error] Auto-download failed: {e}")
        return False

def perform_admin_printer_install():
    """Runs with Administrator privileges to download, install and configure."""
    print("\n===================================================")
    print("  Wols CA Zero-Touch Printer Deployment Started    ")
    print("===================================================\n")
    
    installer_path = config["virtual_printer"]["installer_path"]
    download_url = config["virtual_printer"].get("download_url", "")
    drop_dir = config["paths"]["drop_directory"]

    if not os.path.exists(installer_path):
        print("[System] Installer package missing. Initiating automatic retrieval...")
        if not download_url:
            print("[Error] No download URL configured in JSON. Aborting.")
            time.sleep(5)
            sys.exit(1)
            
        success = download_installer(download_url, installer_path)
        if not success:
            print("[Error] Could not fetch the package. Deployment aborted.")
            time.sleep(5)
            sys.exit(1)

    print(f"[Admin] 1/2: Installing Virtual Printer silently...")
    try:
        subprocess.run([installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", '/COMPONENTS="program"'], check=True)
        print("[Admin] Spooler engine installed successfully.")
    except Exception as e:
        print(f"[Error] Failed to install spooler: {e}")

    print("[Admin] 2/2: Injecting Registry Keys for silent Auto-Save...")
    try:
        key_path = r"Software\pdfforge\PDFCreator\Settings\ConversionProfiles\0"
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        
        winreg.SetValueEx(key, "Enabled", 0, winreg.REG_SZ, "True")
        winreg.SetValueEx(key, "AutoSaveEnabled", 0, winreg.REG_SZ, "True")
        winreg.SetValueEx(key, "AutoSaveDirectory", 0, winreg.REG_SZ, drop_dir)
        winreg.SetValueEx(key, "AutoSaveFilename", 0, winreg.REG_SZ, "<DateTime>_<JobId>_WolsPrintJob")
        winreg.SetValueEx(key, "ShowProgress", 0, winreg.REG_SZ, "False")
        winreg.SetValueEx(key, "OpenViewer", 0, winreg.REG_SZ, "False")
        winreg.CloseKey(key)
        print(f"[Admin] Registry injected. Files will drop to: {drop_dir}")
    except Exception as e:
        print(f"[Warning] Registry injection failed: {e}")

    print("\n[Admin] Deployment complete. You can close this window.")
    time.sleep(5)
    sys.exit(0)

# ============================================================================
# 8. MAIN ENTRY POINT
# ============================================================================
def handle_termination(signum, frame):
    print(f"\n[System] Received signal {signum}. Shutting down service...")
    shutdown_event.set()

def start_service():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_termination)
        except (ValueError, OSError):
            pass

    threading.Thread(target=check_virtual_printer, daemon=True).start()

    load_history()
    worker = threading.Thread(target=job_worker, daemon=True)
    worker.start()

    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"[Warning] Could not connect to MQTT Broker. ({e})")

    httpd = start_web_app()

    scan_existing_files()

    observer = Observer()
    observer.schedule(PrintFolderWatcher(), DROP_DIR, recursive=False)
    # One watcher per intake queue: the directory decides the print mode.
    for queue_entry in intake_queues():
        observer.schedule(PrintFolderWatcher(queue_entry),
                          queue_entry["directory"], recursive=False)
    observer.start()

    print("\n===================================================")
    print(f"  Wols CA Print Service {SERVICE_VERSION} started!")
    print("  Booklet imposition, web app, push notifications    ")
    print(f"  OS Detected: {platform.system()}")
    print(f"  Default printer: {default_printer()['name']}")
    for queue_entry in intake_queues():
        print(f"  Intake queue: {queue_entry['cups_queue']} -> "
              f"{queue_entry['print_mode']} ({queue_entry['directory']})")
    print(f"  Push notifications: "
          f"{'on' if notify_config().get('enabled') else 'off'}")
    if httpd:
        print(f"  Web app: {public_base_url()}/")
    print("===================================================\n")

    set_state("IDLE", "Service started.")

    shutdown_event.wait()

    set_state("OFFLINE", "Service intentionally stopped.")
    if httpd:
        httpd.shutdown()
    observer.stop()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    observer.join()

if __name__ == "__main__":
    if "--install-printer" in sys.argv:
        if IS_LINUX:
            perform_cups_printer_install()
        elif IS_WINDOWS:
            perform_admin_printer_install()
        else:
            print(f"[Error] Printer deployment is not supported on {platform.system()}.")
            sys.exit(1)
    else:
        start_service()