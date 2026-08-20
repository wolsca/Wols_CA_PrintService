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

def load_or_create_config():
    """Loads the JSON configuration or creates it with defaults if it does not exist."""
    global config_data

    default_config = {
        "mqtt": {
            "broker_ip": "192.168.101.240",
            "broker_port": 1883,
            "topic_prefix": "wolsca/printer",
            "discovery_prefix": "homeassistant"
        },
        "paths": {
            "drop_directory": r"C:\ProgramData\WolsCA\PrintFileDrop" if IS_WINDOWS else "/var/spool/wolsca/PrintFileDrop",
            "temp_directory": r"C:\ProgramData\WolsCA\PrintTemp" if IS_WINDOWS else "/var/spool/wolsca/PrintTemp",
            "error_directory": r"C:\ProgramData\WolsCA\PrintError" if IS_WINDOWS else "/var/spool/wolsca/PrintError"
        },
        "hardware": {
            "printer_uri": "ipps://192.168.101.251:443/ipp/print",
            "cups_queue_name": "WolsCA_Output",
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
            "user": "WolsCADoublePrint",
            "password": "DefaultPassword",
            "print_mode": "Booklet"
        }
    }

    if os.path.exists(CONFIG_PATH):
        print(f"[System] Loading configuration from {CONFIG_PATH}...")
        try:
            with open(CONFIG_PATH, 'r') as f:
                config_data = json.load(f)
                # Ensure all main sections exist
                for section in ("settings", "hardware", "virtual_printer", "printers", "web", "notify", "history", "intake", "update"):
                    if section not in config_data:
                        config_data[section] = default_config[section]

                # Fill in missing sub-keys to keep backward compatibility
                for section in ("hardware", "web", "notify", "history", "printers", "intake", "update"):
                    for key, value in default_config[section].items():
                        config_data[section].setdefault(key, value)
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