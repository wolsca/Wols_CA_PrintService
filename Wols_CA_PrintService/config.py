import os
import json
import platform

# Determine OS
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

CONFIG_FILE = "WolsCAPrintService.json"
SYSTEM_CONFIG_DIR = "/etc/wolsca"
config_data = {}

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

# Print modes are named after what comes out of the printer. 'Duplex' and
# 'Simplex' were the values up to and including version 1.4, so they are still
# accepted and translated whenever a mode is read.
PRINT_MODES = ("Booklet", "DoubleSided", "SingleSided")
PRINT_MODE_ALIASES = {
    "booklet": "Booklet",
    "duplex": "DoubleSided",
    "double": "DoubleSided",
    "doublesided": "DoubleSided",
    "simplex": "SingleSided",
    "single": "SingleSided",
    "singlesided": "SingleSided",
}

# The intake queue id is an internal key: it names the cups-pdf instance
# (/etc/cups/cups-pdf-<id>.conf) and the drop directory. Up to and including
# version 1.4 these were 'duplex' and 'simplex', so both are translated.
INTAKE_ID_ALIASES = {
    "duplex": "doublesided",
    "simplex": "singlesided",
}

def normalize_print_mode(value):
    """Maps an old or differently cased mode name onto its current name."""
    if not value:
        return value
    text = str(value).strip()
    return PRINT_MODE_ALIASES.get(text.lower(), text)

def normalize_intake_id(value):
    """Maps an old or differently cased queue id onto its current id."""
    if not value:
        return value
    text = str(value).strip().lower()
    return INTAKE_ID_ALIASES.get(text, text)

def normalize_print_modes():
    """Rewrites the modes and queue ids of a configuration written by an older version.

    Returns True when something was renamed, so the caller can write the
    migrated configuration back - otherwise the old names keep showing up in the
    file even though the service works with the new ones.
    """
    changed = False
    settings = config_data.get("settings", {})
    if settings.get("print_mode"):
        migrated = normalize_print_mode(settings["print_mode"])
        changed = changed or migrated != settings["print_mode"]
        settings["print_mode"] = migrated
    for q_entry in config_data.get("intake", {}).get("queues", []):
        if q_entry.get("print_mode"):
            migrated = normalize_print_mode(q_entry["print_mode"])
            changed = changed or migrated != q_entry["print_mode"]
            q_entry["print_mode"] = migrated
        old_id = str(q_entry.get("id") or "").strip()
        new_id = normalize_intake_id(old_id)
        if new_id and new_id != old_id:
            q_entry["id"] = new_id
            changed = True
            # The drop directory is named after the id; only rewrite it when it
            # carries an old name of this queue ('duplex', 'Simplex', ...), never
            # a path the user chose himself.
            # The last segment is replaced textually, so a Linux path keeps its
            # forward slashes even when the migration runs on Windows.
            directory = str(q_entry.get("directory") or "").rstrip("/\\")
            tail = os.path.basename(directory.replace("\\", "/"))
            if tail and normalize_intake_id(tail) == new_id:
                q_entry["directory"] = directory[:len(directory) - len(tail)] + new_id
    return changed

def load_or_create_config():
    """Loads the JSON configuration or creates it with defaults if it does not exist."""
    global config_data

    default_config = {
        "mqtt": {
            "broker_ip": "192.168.101.240",
            "broker_port": 1883,
            "topic_prefix": "wols_ca/print_service",
            "discovery_prefix": "homeassistant",
            # Broker account, not a system user. Create it on the broker itself
            # (Mosquitto add-on in Home Assistant, or EMQX on the server).
            "user": "wolsca_mqtt",
            "password": "DefaultPassword",
            # Label of this instance. Empty is the classic single installation;
            # the Home Assistant add-on sets it to 'HA' at start-up, which keeps
            # its entities apart from a Debian installation on the same broker.
            "instance_id": ""
        },
        "paths": {
            "drop_directory": r"C:\ProgramData\WolsCA\PrintFileDrop" if IS_WINDOWS else "/var/spool/wolsca/PrintFileDrop",
            "temp_directory": r"C:\ProgramData\WolsCA\PrintTemp" if IS_WINDOWS else "/var/spool/wolsca/PrintTemp",
            "error_directory": r"C:\ProgramData\WolsCA\PrintError" if IS_WINDOWS else "/var/spool/wolsca/PrintError"
        },
        "hardware": {
            "printer_uri": "ipps://192.168.101.251:443/ipp/print",
            "cups_queue_name": "WolsCA_Output",
            # Who confirms the flip halfway through a job: 'auto' uses the
            # button on the printer when it offers manual duplex (AirPrint /
            # Mopria) and the Continue button of the service otherwise;
            # 'printer' and 'service' force one of the two.
            "flip_confirmation": "auto",
            # A single page in DoubleSided mode. 'off' prints it straight away;
            # 'printer' sends it to the manual feed slot, so the printer asks on
            # its own panel and prints nothing until OK; 'pause' asks in the web
            # app or Home Assistant first; 'blank' prints a blank front so the
            # printer asks and the page lands on the back of that sheet.
            "single_page_paper_change": "off",
            "single_page_media_source": "manual",
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
                    "id": "doublesided",
                    "cups_queue": "WolsCA_DoubleSided",
                    "description": "Double sided (two pages per sheet, front and back)",
                    "print_mode": "DoubleSided",
                    "directory": ""
                },
                {
                    "id": "singlesided",
                    "cups_queue": "WolsCA_SingleSided",
                    "description": "Single sided (one page per sheet)",
                    "print_mode": "SingleSided",
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
                    "dispatch": "cups",
                    "cups_queue": "WolsCA_Output",
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
            # On by default: without a push message on the phone the manual flip
            # halfway through a job is easy to miss. An empty topic makes the
            # service generate a unique one at first use (see notifier.py).
            "enabled": True,
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
        "update": {
            "enabled": True,
            "repository": "wolsca/Wols_CA_PrintService",
            "branch": "main",
            "channel": "release",
            "allow_test_builds": True,
            "check_interval_hours": 6,
            "auto_update": False,
            "source_directory": "/usr/local/src/wolsca-print-service",
            "update_command": ""
        },
        "settings": {
            # The MQTT credentials moved to the 'mqtt' section; they are only
            # read here for configurations written by an older version.
            "print_mode": "DoubleSided"
        }
    }

    if os.path.exists(CONFIG_PATH):
        print(f"[System] Loading configuration from {CONFIG_PATH}...")
        try:
            with open(CONFIG_PATH, 'r') as f:
                config_data = json.load(f)
                # Ensure all main sections exist
                for section in ("mqtt", "settings", "hardware", "virtual_printer", "printers", "web", "notify", "history", "intake", "update"):
                    if section not in config_data:
                        config_data[section] = default_config[section]

                # Fill in missing sub-keys to keep backward compatibility
                for section in ("mqtt", "hardware", "web", "notify", "history", "printers", "intake", "update"):
                    for key, value in default_config[section].items():
                        config_data[section].setdefault(key, value)

                if normalize_print_modes():
                    print("[System] Print modes and intake queue ids migrated to the current names.")
                    save_config()
        except Exception as e:
            print(f"[Error] Failed to read JSON config: {e}. Using defaults.")
            config_data = default_config
    else:
        print(f"[System] Config file not found. Creating default {CONFIG_PATH}...")
        config_data = default_config
        save_config()

def save_config():
    """Saves the current configuration state to the JSON file."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config_data, f, indent=4)
        print("[System] Configuration saved successfully.")
    except Exception as e:
        print(f"[Error] Could not save config file: {e}")

def get_config():
    return config_data

def get_mqtt_credentials():
    """Returns the broker account as (user, password).

    The credentials live in the 'mqtt' section. Configurations written by an
    older version kept them in 'settings', so that location is still honoured
    when the 'mqtt' section carries no value.
    """
    mqtt_section = config_data.get("mqtt", {})
    settings_section = config_data.get("settings", {})
    user = mqtt_section.get("user") or settings_section.get("user", "")
    password = mqtt_section.get("password") or settings_section.get("password", "")
    return user, password

# Execute on import to ensure directories exist
load_or_create_config()

DROP_DIR = config_data["paths"]["drop_directory"]
TEMP_DIR = config_data["paths"]["temp_directory"]
ERROR_DIR = config_data.get("paths", {}).get("error_directory", os.path.join(TEMP_DIR, "error"))

for directory in [DROP_DIR, TEMP_DIR, ERROR_DIR]:
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        print(f"[Error] Could not create directory {directory}: {e}")