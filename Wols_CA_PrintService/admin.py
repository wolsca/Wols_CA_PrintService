"""Administrator configuration editing for the Wols CA Print Service.

The configuration file stays the single source of truth, but the settings that
are changed in practice can be edited without a shell:

* in the web app, in the *Administrator* card, unlocked with the token from
  `web.admin_token` (when the token is empty the card stays locked, so the
  editor is off by default),
* in Home Assistant, through its own MQTT device *Wols CA Print Service Admin*
  with one entity per editable setting.

Editing is deliberately a two-step process: values are validated and written to
the configuration file immediately, and because most of them are only read at
start-up a *restart required* flag is raised. The web app then asks "restart the
service now, yes or no", Home Assistant shows the same question as a
`Restart required` sensor plus a `Restart service` button.

Only the whitelisted fields below can be edited; everything else in the
configuration needs the file and a restart, which keeps a typo in the web app
from breaking the paths or the intake queues.
"""

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime

import config

# Fields the administrator may edit, in the order they are shown.
#   key      dotted path into the configuration
#   type     text | password | number | bool | select
FIELDS = [
    {"key": "web.title", "label": "Web app title", "type": "text"},
    {"key": "web.language", "label": "Language", "type": "select", "options": ["en", "nl"]},
    {"key": "web.port", "label": "Web app port", "type": "number", "min": 1, "max": 65535},
    {"key": "web.public_url", "label": "Public URL", "type": "text"},
    {"key": "settings.print_mode", "label": "Default print mode", "type": "select",
     "options": ["Booklet", "Duplex", "Simplex"]},
    {"key": "printers.default", "label": "Default printer id", "type": "text"},
    {"key": "printers.personal_choice_ttl_seconds", "label": "Personal choice TTL (s)",
     "type": "number", "min": 30, "max": 86400},
    {"key": "hardware.printer_uri", "label": "Printer URI", "type": "text"},
    {"key": "hardware.cups_queue_name", "label": "Output CUPS queue", "type": "text"},
    {"key": "hardware.flip_instruction", "label": "Flip instruction", "type": "text"},
    {"key": "hardware.flip_timeout_seconds", "label": "Flip timeout (s)", "type": "number",
     "min": 60, "max": 86400},
    {"key": "mqtt.broker_ip", "label": "MQTT broker", "type": "text"},
    {"key": "mqtt.broker_port", "label": "MQTT port", "type": "number", "min": 1, "max": 65535},
    {"key": "notify.enabled", "label": "Notifications", "type": "bool"},
    {"key": "notify.url", "label": "Notification server", "type": "text"},
    {"key": "notify.topic", "label": "Notification topic", "type": "text"},
    {"key": "notify.notify_on_error", "label": "Notify on error", "type": "bool"},
    {"key": "update.enabled", "label": "Update checking", "type": "bool"},
    {"key": "update.channel", "label": "Update channel", "type": "select",
     "options": ["release", "branch"]},
    {"key": "update.auto_update", "label": "Automatic updates", "type": "bool"},
    {"key": "update.allow_test_builds", "label": "Allow test builds", "type": "bool"},
    {"key": "update.check_interval_hours", "label": "Check interval (h)", "type": "number",
     "min": 1, "max": 168},
    {"key": "update.branch", "label": "Update branch", "type": "text"},
    {"key": "history.enabled", "label": "Job history", "type": "bool"},
    {"key": "history.max_entries", "label": "History entries", "type": "number",
     "min": 1, "max": 500},
]

FIELDS_BY_KEY = {field["key"]: field for field in FIELDS}

SERVICE_NAME = "wolsca-print-service"

state = {
    "restart_required": False,
    "last_saved": None,
    "last_result": "",
    "changed": []
}


# --- helpers ------------------------------------------------------------

def entity_id(key):
    """'web.title' -> 'web_title', usable in a topic and a unique_id."""
    return key.replace(".", "_")


def get_value(key):
    section, _, name = key.partition(".")
    return config.get_config().get(section, {}).get(name)


def coerce(field, value):
    """Validates and converts one value; raises ValueError when impossible."""
    kind = field["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "on", "yes", "ja")
    if kind == "number":
        number = int(float(str(value).strip()))
        if "min" in field and number < field["min"]:
            raise ValueError(f"{field['label']}: minimum is {field['min']}")
        if "max" in field and number > field["max"]:
            raise ValueError(f"{field['label']}: maximum is {field['max']}")
        return number
    if kind == "select":
        text = str(value).strip()
        if text not in field["options"]:
            raise ValueError(f"{field['label']}: must be one of {', '.join(field['options'])}")
        return text
    return str(value)


def fields_payload():
    """The editable fields plus their current values, for the web app and HA."""
    entries = []
    for field in FIELDS:
        entry = dict(field)
        entry["value"] = get_value(field["key"])
        entries.append(entry)
    return {
        "fields": entries,
        "restart_required": bool(state["restart_required"]),
        "last_saved": state["last_saved"],
        "last_result": state["last_result"],
        "config_path": config.CONFIG_PATH,
        "service": SERVICE_NAME
    }


# --- access control -----------------------------------------------------

def admin_token():
    return str(config.get_config().get("web", {}).get("admin_token") or "")


def token_valid(token):
    """The editor is locked while no token is configured."""
    expected = admin_token()
    if not expected:
        return False
    return str(token or "") == expected


# --- saving -------------------------------------------------------------

def backup_config():
    """Keeps one .bak next to the configuration file."""
    try:
        if os.path.exists(config.CONFIG_PATH):
            shutil.copy2(config.CONFIG_PATH, config.CONFIG_PATH + ".bak")
    except Exception as e:
        print(f"[Admin] Could not write the configuration backup: {e}")


def apply_values(values, publish=True):
    """Validates and stores the given {key: value} pairs.

    Returns a result dict; nothing is written when a value is invalid.
    """
    prepared = {}
    errors = []
    for key, raw in (values or {}).items():
        field = FIELDS_BY_KEY.get(key)
        if not field:
            errors.append(f"{key}: not editable")
            continue
        try:
            prepared[key] = coerce(field, raw)
        except (ValueError, TypeError) as e:
            errors.append(str(e))

    if errors:
        state["last_result"] = "; ".join(errors)
        print(f"[Admin] Rejected the configuration change: {state['last_result']}")
        if publish:
            publish_state()
        return {"saved": False, "errors": errors, "changed": [],
                "restart_required": state["restart_required"]}

    changed = []
    c = config.get_config()
    for key, value in prepared.items():
        section, _, name = key.partition(".")
        target = c.setdefault(section, {})
        if target.get(name) != value:
            target[name] = value
            changed.append(key)

    if changed:
        backup_config()
        config.save_config()
        state["restart_required"] = True
        state["changed"] = sorted(set(state["changed"]) | set(changed))
        state["last_saved"] = datetime.now().isoformat(timespec="seconds")
        state["last_result"] = f"Saved {len(changed)} setting(s): {', '.join(changed)}."
        print(f"[Admin] {state['last_result']}")
    else:
        state["last_result"] = "No changes."

    if publish:
        publish_state()
    return {"saved": bool(changed), "errors": [], "changed": changed,
            "restart_required": state["restart_required"]}


def reload_config(publish=True):
    """Throws away unsaved edits by reading the file again."""
    config.load_or_create_config()
    state["restart_required"] = False
    state["changed"] = []
    state["last_result"] = "Configuration reloaded from disk."
    print(f"[Admin] {state['last_result']}")
    if publish:
        publish_state()
    return fields_payload()


# --- restarting ---------------------------------------------------------

def restart_service(publish=True):
    """Restarts the systemd unit; the answer to the 'restart now?' question."""
    command = ["systemctl", "restart", SERVICE_NAME]
    state["last_result"] = f"Restarting {SERVICE_NAME}..."
    print(f"[Admin] {state['last_result']}")
    if publish:
        publish_state()
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=60)
        output = completed.stdout.decode("utf-8", "replace").strip()
        if completed.returncode == 0:
            state["restart_required"] = False
            state["changed"] = []
            state["last_result"] = "Service restart requested."
        else:
            state["last_result"] = f"Restart failed (exit code {completed.returncode}): {output}"
    except Exception as e:
        state["last_result"] = f"Restart failed: {e}"
    print(f"[Admin] {state['last_result']}")
    if publish:
        publish_state()
    return {"restarted": not state["restart_required"], "detail": state["last_result"]}


def restart_async():
    threading.Thread(target=restart_service, daemon=True).start()


# --- MQTT ---------------------------------------------------------------

def topic(suffix):
    import mqtt_service
    return f"{mqtt_service.PREFIX}/admin/{suffix}"


def device_info():
    """A device of its own, so the admin entities can be hidden from users."""
    import mqtt_service
    return {
        "identifiers": ["wolsca_print_service_admin"],
        "name": "Wols CA Print Service Admin",
        "manufacturer": "Wols CA",
        "model": "Configuration",
        "sw_version": mqtt_service.SERVICE_VERSION,
        "via_device": "wolsca_print_service_01"
    }


def publish_state():
    """Publishes every field value plus the restart flag."""
    import mqtt_service
    try:
        for field in FIELDS:
            value = get_value(field["key"])
            if field["type"] == "bool":
                text = "ON" if value else "OFF"
            else:
                text = "" if value is None else str(value)
            mqtt_service.mqtt_client.publish(topic(f"value/{entity_id(field['key'])}"),
                                             text, retain=True)
        mqtt_service.mqtt_client.publish(topic("state"), json.dumps({
            "restart_required": bool(state["restart_required"]),
            "changed": state["changed"],
            "last_saved": state["last_saved"],
            "last_result": state["last_result"],
            "config_path": config.CONFIG_PATH
        }), retain=True)
        mqtt_service.mqtt_client.publish(topic("restart_required"),
                                         "ON" if state["restart_required"] else "OFF",
                                         retain=True)
    except Exception as e:
        print(f"[Error] Could not publish the admin state: {e}")


def handle_command(command_topic, payload):
    """Handles '<prefix>/admin/set/<entity>' and the admin buttons."""
    prefix = topic("set/")
    if command_topic.startswith(prefix):
        entity = command_topic[len(prefix):]
        for field in FIELDS:
            if entity_id(field["key"]) == entity:
                apply_values({field["key"]: payload})
                return True
        print(f"[Admin] Unknown setting '{entity}' on the admin topic.")
        return True
    return False


def publish_ha_discovery():
    """One Home Assistant entity per editable setting, plus the restart pair."""
    import mqtt_service

    device = device_info()
    for field in FIELDS:
        eid = entity_id(field["key"])
        common = {
            "name": field["label"],
            "state_topic": topic(f"value/{eid}"),
            "command_topic": topic(f"set/{eid}"),
            "entity_category": "config",
            "unique_id": f"wolsca_admin_{eid}",
            "device": device
        }
        if field["type"] == "bool":
            platform = "switch"
            common.update({"payload_on": "ON", "payload_off": "OFF",
                           "state_on": "ON", "state_off": "OFF", "icon": "mdi:toggle-switch"})
        elif field["type"] == "number":
            platform = "number"
            common.update({"min": field.get("min", 0), "max": field.get("max", 65535),
                           "step": 1, "mode": "box", "icon": "mdi:numeric"})
        elif field["type"] == "select":
            platform = "select"
            common.update({"options": field["options"], "icon": "mdi:format-list-bulleted"})
        else:
            platform = "text"
            common.update({"max": 255, "icon": "mdi:form-textbox"})

        mqtt_service.mqtt_client.publish(
            f"{mqtt_service.HA_PREFIX}/{platform}/wolsca_admin/{eid}/config",
            json.dumps(common), retain=True)
        mqtt_service.mqtt_client.subscribe(topic(f"set/{eid}"))

    restart_sensor = {
        "name": "Restart Required",
        "state_topic": topic("restart_required"),
        "payload_on": "ON",
        "payload_off": "OFF",
        "json_attributes_topic": topic("state"),
        "icon": "mdi:restart-alert",
        "entity_category": "diagnostic",
        "unique_id": "wolsca_admin_restart_required",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/binary_sensor/wolsca_admin/restart_required/config",
        json.dumps(restart_sensor), retain=True)

    restart_button = {
        "name": "Restart Print Service",
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "RESTART_SERVICE",
        "icon": "mdi:restart",
        "entity_category": "config",
        "unique_id": "wolsca_admin_restart",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/wolsca_admin/restart/config",
        json.dumps(restart_button), retain=True)

    discard_button = {
        "name": "Discard Configuration Changes",
        "command_topic": f"{mqtt_service.PREFIX}/command",
        "payload_press": "RELOAD_CONFIG",
        "icon": "mdi:undo-variant",
        "entity_category": "config",
        "unique_id": "wolsca_admin_reload",
        "device": device
    }
    mqtt_service.mqtt_client.publish(
        f"{mqtt_service.HA_PREFIX}/button/wolsca_admin/reload/config",
        json.dumps(discard_button), retain=True)

    publish_state()
