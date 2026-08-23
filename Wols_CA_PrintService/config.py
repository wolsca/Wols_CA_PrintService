import os
import json
import threading
import platform

# Determine OS
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

CONFIG_FILE = "WolsCAPrintService.json"
SYSTEM_CONFIG_DIR = "/etc/wolsca"
config_data = {}

# Version of the *configuration file*, not of the service. A file without a
# 'config_version' key was written before versioning existed and is therefore a
# 1.0 file. Every step below is applied exactly once, in order, so an old file
# is brought up to date field by field instead of being overwritten.
# A step may carry a letter ('1.1.a', '1.1.b'): sub-steps of one release, which
# keeps a step that is already published untouched while the next field is added.
CONFIG_VERSION = "1.1.b"
LEGACY_CONFIG_VERSION = "1.0"

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

# The three modes differ in their first letter, and that is all that has to be
# recognised: 'booklet', 'BOOKLET', 'Boekje', 'b' and 'Booklet ' all mean the
# same thing. A mode written differently used to fall through every comparison
# ('print_mode == "Booklet"'), so the job silently landed in the wrong branch.
PRINT_MODE_LETTERS = {
    "b": "Booklet",
    "d": "DoubleSided",
    "s": "SingleSided",
}

# The intake queue id is an internal key: it names the cups-pdf instance
# (/etc/cups/cups-pdf-<id>.conf) and the drop directory. Up to and including
# version 1.4 these were 'duplex' and 'simplex', so both are translated.
INTAKE_ID_ALIASES = {
    "duplex": "doublesided",
    "simplex": "singlesided",
}

def normalize_print_mode(value):
    """Maps an old, differently cased or abbreviated mode onto its current name.

    Validation is deliberately not case sensitive: first the known names and old
    names are tried, and otherwise the first letter decides ('b' = Booklet,
    'd' = DoubleSided, 's' = SingleSided). A value that starts with none of the
    three is returned unchanged, so it is still reported as unknown instead of
    being turned into an arbitrary mode.
    """
    if not value:
        return value
    text = str(value).strip()
    known = PRINT_MODE_ALIASES.get(text.lower())
    if known:
        return known
    return PRINT_MODE_LETTERS.get(text[:1].lower(), text)

def is_print_mode(value, mode):
    """True when `value` means `mode`, whatever its spelling."""
    return normalize_print_mode(value) == mode

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

def version_tuple(value):
    """'1.10' -> (1, 10, 0), so 1.10 is newer than 1.2 and not 'smaller'.

    A letter is a sub-step of that version: '1.1' < '1.1.a' < '1.1.b' < '1.2',
    so 'a' counts as 1, 'b' as 2 and a version without a letter as 0.
    """
    parts = []
    for part in str(value or LEGACY_CONFIG_VERSION).strip().lower().split("."):
        try:
            parts.append(int(part))
        except ValueError:
            letters = [ord(ch) - ord("a") + 1 for ch in part if "a" <= ch <= "z"]
            parts.append(letters[0] if letters else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)

def config_version():
    """The version of the loaded file; a missing key means the original 1.0."""
    return str(config_data.get("config_version") or LEGACY_CONFIG_VERSION).strip()

def _fill_defaults(section, defaults):
    """Adds the keys of a new version without touching an existing value."""
    added = []
    target = config_data.setdefault(section, {})
    for key, value in defaults.items():
        if key not in target:
            target[key] = value
            added.append(f"{section}.{key}")
    return added

def migrate_1_1_a(default_config):
    """1.1.a - Wake-on-LAN, waiting for the printer and its MAC address.

    A printer that is switched off or asleep may not cost a print job any more,
    so `hardware.wake_on_lan`, `printer_mac`, `wake_broadcast` and
    `wait_for_printer_seconds` are added. The MAC address cannot be guessed from
    an IP address by a sleeping printer, but as long as it is still awake the
    neighbour table of this host knows it - so it is looked up over the network
    and filled in, which saves reading it from the printer's own status page.
    """
    notes = _fill_defaults("hardware", {
        key: default_config["hardware"][key]
        for key in ("wake_on_lan", "printer_mac", "wake_broadcast",
                    "wait_for_printer_seconds")
    })
    if not str(config_data.get("hardware", {}).get("printer_mac") or "").strip():
        detected = detect_printer_mac()
        if detected:
            config_data["hardware"]["printer_mac"] = detected
            notes.append(f"hardware.printer_mac detected over the network: {detected}")
        else:
            notes.append("hardware.printer_mac stays empty - the printer did not answer, "
                         "so its MAC address could not be read from the network")
    return notes

def migrate_1_1_b(default_config):
    """1.1.b - one MAC address per interface, IP recovery and a printer per queue.

    One address was not enough. A printer has a MAC address per interface, so a
    printer moved from the cable to Wi-Fi answers with a different one:
    `printer_mac_wired` and `printer_mac_wifi` hold what is printed on the
    printer itself, `printer_mac` the address that answers right now. With those
    known a printer that lost its DHCP address can be found again by MAC address
    (`recover_printer_ip`), and an address that matches neither is reported
    instead of silently accepted.

    The same step gives every intake queue its own printer choice, so booklet,
    double sided and single sided can each go to a different machine.
    """
    notes = _fill_defaults("hardware", {
        key: default_config["hardware"][key]
        for key in ("printer_mac_wired", "printer_mac_wifi", "recover_printer_ip",
                    "block_on_mac_change")
    })
    hardware = config_data.setdefault("hardware", {})
    working = str(hardware.get("printer_mac") or "").strip()
    if working and not str(hardware.get("printer_mac_wired") or "").strip():
        # The interface it answered on is unknown; a print server is normally
        # cabled, so it is stored as the wired address. The Wi-Fi address has to
        # come from the printer itself - that is the whole point of having both.
        hardware["printer_mac_wired"] = working
        notes.append(f"hardware.printer_mac_wired set to {working}; fill in "
                     f"hardware.printer_mac_wifi from the printer itself to survive a "
                     f"switch to Wi-Fi")
    for q_entry in config_data.get("intake", {}).get("queues", []) or []:
        if "printer" not in q_entry:
            q_entry["printer"] = ""
            notes.append(f"intake queue '{q_entry.get('id')}' can now choose its own "
                         f"printer (empty = the default printer)")
    return notes

def detect_printer_mac():
    """The MAC address of the configured printer, read from the network.

    The printer is contacted first (that is what puts it in the ARP table) and
    only then the table is read. Returns None when the printer does not answer.
    """
    try:
        import printer_power
    except Exception:
        return None
    try:
        uri = str(config_data.get("hardware", {}).get("printer_uri", ""))
        targets = config_data.get("printers", {}).get("targets") or [{}]
        target = targets[0]
        # Short timeout: this runs once during the upgrade at start-up and a
        # printer that is off must not delay it.
        printer_power.reachable(target, uri, timeout=1.5)
        return printer_power.detect_mac(target=target, uri=uri)
    except Exception:
        return None

# One entry per configuration version, in order. The steps of a version that the
# file already has are skipped, so only the difference is applied.
MIGRATIONS = (
    ("1.1.a", migrate_1_1_a),
    ("1.1.b", migrate_1_1_b),
)

def upgrade_config(default_config):
    """Runs every migration newer than the version in the file.

    Returns True when the file has to be written back. Called on every start, so
    an installation that skips versions (1.0 straight to 1.3) still runs 1.1,
    1.2 and 1.3 one after another.
    """
    current = config_version()
    if version_tuple(current) >= version_tuple(CONFIG_VERSION):
        # Nothing to do; only stamp a file that never carried a version.
        if config_data.get("config_version") != CONFIG_VERSION:
            config_data["config_version"] = CONFIG_VERSION
            return True
        return False

    print(f"[Config] Upgrading configuration {current} -> {CONFIG_VERSION}...")
    for version, migration in MIGRATIONS:
        if version_tuple(version) <= version_tuple(current):
            continue                      # already in the file, skip this step
        try:
            notes = migration(default_config) or []
        except Exception as e:
            print(f"[Config] Upgrade to {version} failed: {e}")
            break
        for note in notes:
            print(f"[Config] {version}: {note}")
        config_data["config_version"] = version
        current = version
        print(f"[Config] Configuration is now version {version}.")
    return True

def load_or_create_config():
    """Loads the JSON configuration or creates it with defaults if it does not exist."""
    global config_data

    default_config = {
        "config_version": CONFIG_VERSION,
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
            "flip_timeout_seconds": 1800,
            # A printer that is asleep or switched off answers nothing. That is
            # not an error of the service: the job waits for it (0 disables the
            # waiting) and, when the MAC address is known and the printer has
            # Wake-on-LAN enabled, a magic packet is sent first.
            "wake_on_lan": True,
            # A printer has one MAC address per interface. The wired and the
            # Wi-Fi address are both written down (they are on the printer
            # itself), 'printer_mac' is the one that answers right now. Knowing
            # both means a printer that is moved from the cable to Wi-Fi - or
            # that got another DHCP address - is still found, and an address
            # that matches neither is reported instead of silently accepted.
            "printer_mac": "",
            "printer_mac_wired": "",
            "printer_mac_wifi": "",
            # Look the printer up by MAC address when it does not answer on the
            # configured address any more (a DHCP lease without a reservation).
            "recover_printer_ip": True,
            # Safety: an IP address does not say which machine answers on it. A
            # MAC address that is neither the wired nor the Wi-Fi address of the
            # printer means the document would be printed on an unknown device,
            # so the job is stopped. Switch off to print anyway.
            "block_on_mac_change": True,
            "wake_broadcast": "255.255.255.255",
            "wait_for_printer_seconds": 900
        },
        "intake": {
            "enabled": True,
            "queues": [
                {
                    "id": "booklet",
                    "cups_queue": "WolsCA_Booklet",
                    "description": "Booklet (A5, fold in the middle)",
                    "print_mode": "Booklet",
                    "directory": "",
                    # Which physical printer this queue prints on (a target id).
                    # Empty means: the default printer, or - when only one
                    # printer is known - simply that one.
                    "printer": ""
                },
                {
                    "id": "doublesided",
                    "cups_queue": "WolsCA_DoubleSided",
                    "description": "Double sided (two pages per sheet, front and back)",
                    "print_mode": "DoubleSided",
                    "directory": "",
                    "printer": ""
                },
                {
                    "id": "singlesided",
                    "cups_queue": "WolsCA_SingleSided",
                    "description": "Single sided (one page per sheet)",
                    "print_mode": "SingleSided",
                    "directory": "",
                    "printer": ""
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
                # 'paths' belongs in this list too: DROP_DIR and TEMP_DIR are
                # read straight from it below, so a file without the section
                # used to end the service with a KeyError before it started.
                for section in ("paths", "mqtt", "settings", "hardware", "virtual_printer", "printers", "web", "notify", "history", "intake", "update"):
                    if section not in config_data:
                        config_data[section] = default_config[section]

                changed = normalize_print_modes()
                if changed:
                    print("[System] Print modes and intake queue ids migrated to the current names.")

                # The version driven upgrade runs before the defaults are filled
                # in, so every step still sees the file as the older version
                # wrote it and can decide what to do with a missing field.
                if upgrade_config(default_config):
                    changed = True

                # Fill in missing sub-keys to keep backward compatibility - the
                # safety net for a section a migration does not cover.
                for section in ("paths", "mqtt", "hardware", "web", "notify", "history", "printers", "intake", "update"):
                    for key, value in default_config[section].items():
                        config_data[section].setdefault(key, value)

                if changed:
                    save_config()
        except Exception as e:
            print(f"[Error] Failed to read JSON config: {e}. Using defaults.")
            config_data = default_config
    else:
        print(f"[System] Config file not found. Creating default {CONFIG_PATH}...")
        config_data = default_config
        save_config()

_save_lock = threading.Lock()

def save_config():
    """Saves the current configuration state to the JSON file.

    Written to a temporary file next to it and then moved into place: opening
    the file for writing empties it first, so a save that fails halfway (or two
    saves at the same time - the web app, Home Assistant and an upgrade all
    save) leaves an empty configuration behind, and the service then no longer
    starts. The move is atomic, so the file is either the old or the new one.
    """
    with _save_lock:
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            temporary = f"{CONFIG_PATH}.new"
            with open(temporary, 'w') as f:
                json.dump(config_data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, CONFIG_PATH)
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