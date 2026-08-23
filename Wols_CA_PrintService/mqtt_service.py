import json
import re
import threading
from datetime import datetime
import paho.mqtt.client as mqtt
import config
import version

# Release from the VERSION file plus the commit number from BUILD_NUMBER.
SERVICE_VERSION = version.FULL_VERSION

# --- State Management ---
state_lock = threading.Lock()
waiting_for_user_action = False
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
    # Who confirms the flip: 'service' (Continue button in the web app and in
    # Home Assistant) or 'printer' (the button on the printer itself). Exactly
    # one of the two is offered, never both.
    "flip_owner": "service",
    "updated": datetime.now().isoformat()
}

def set_flip_owner(owner):
    """Hands the flip confirmation to the printer panel or back to the service.

    The Continue button in Home Assistant is switched off through its
    availability topic, so the two can never be pressed against each other.
    """
    with state_lock:
        if job_state["flip_owner"] == owner:
            return
        job_state["flip_owner"] = owner
    publish_resume_availability(owner)
    print(f"[System] Flip confirmation is handled by the {owner}.")


def publish_resume_availability(owner=None):
    """Marks the Home Assistant Continue button (un)available."""
    if owner is None:
        with state_lock:
            owner = job_state["flip_owner"]
    mqtt_client.publish(f"{PREFIX}/availability/resume",
                        "offline" if owner == "printer" else "online", retain=True)


def request_cancel():
    """Flags the current job for cancellation."""
    global waiting_for_user_action
    with state_lock:
        job_control["cancel"] = True
    waiting_for_user_action = False

def request_reprint_front():
    """Flags the front side to be reprinted."""
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

# --- MQTT Setup ---
c = config.get_config()
PREFIX = c["mqtt"].get("topic_prefix", "wols_ca/print_service")
HA_PREFIX = c["mqtt"].get("discovery_prefix", "homeassistant")

# Instance identity. Empty means the classic single installation, so its entity
# ids stay exactly as they were. The Home Assistant add-on sets it to 'HA'
# (dynamically, in the container entrypoint), which gives every entity its own
# unique_id, its own discovery node and its own device - that is what makes an
# add-on and a Debian installation able to run side by side on one broker.
INSTANCE_ID = str(c["mqtt"].get("instance_id", "") or "").strip()
INSTANCE_SLUG = re.sub(r"[^a-z0-9]+", "_", INSTANCE_ID.lower()).strip("_")
SUFFIX = f"_{INSTANCE_SLUG}" if INSTANCE_SLUG else ""
NODE_ID = f"wolsca_print{SUFFIX}"
ADMIN_NODE_ID = f"wolsca_admin{SUFFIX}"
DEVICE_ID = f"wolsca_print_service_01{SUFFIX}"
NAME_SUFFIX = f" ({INSTANCE_ID})" if INSTANCE_ID else ""


def uid(name):
    """A unique_id that is unique per instance as well."""
    return f"{name}{SUFFIX}"


def entity_name(name):
    """Entity name carrying the instance label, so both are recognisable in HA."""
    return f"{name}{NAME_SUFFIX}"


mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

DEVICE_INFO = {
    "identifiers": [DEVICE_ID],
    "name": f"Wols CA Print Service{NAME_SUFFIX}",
    "manufacturer": "Wols CA",
    "model": "Double Sided Spooler",
    "sw_version": SERVICE_VERSION
}

def publish_ha_discovery():
    """Publishes Home Assistant auto-discovery payloads."""
    print("[MQTT] Publishing Home Assistant Auto-Discovery configuration...")

    config_status = {
        "name": entity_name("Print Service Status"),
        "state_topic": f"{PREFIX}/status",
        "value_template": "{{ value_json.state }}",
        "json_attributes_topic": f"{PREFIX}/status",
        "icon": "mdi:printer-3d",
        "unique_id": uid("wolsca_print_status"),
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/sensor/{NODE_ID}/status/config", json.dumps(config_status), retain=True)

    config_error = {
        "name": entity_name("Print Service Last Error"),
        "state_topic": f"{PREFIX}/status",
        "value_template": "{% if value_json.state == 'ERROR' %}{{ value_json.detail }}{% else %}No errors{% endif %}",
        "icon": "mdi:alert-circle-outline",
        "unique_id": uid("wolsca_print_error"),
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/sensor/{NODE_ID}/error/config", json.dumps(config_error), retain=True)

    config_button = {
        "name": entity_name("Resume Print (Flip)"),
        "command_topic": f"{PREFIX}/command",
        "payload_press": "RESUME",
        "icon": "mdi:page-next",
        "unique_id": uid("wolsca_print_resume"),
        # Unavailable while the printer itself asks for the flip on its panel.
        "availability_topic": f"{PREFIX}/availability/resume",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/button/{NODE_ID}/resume/config", json.dumps(config_button), retain=True)
    publish_resume_availability()

    config_cancel = {
        "name": entity_name("Cancel Print Job"),
        "command_topic": f"{PREFIX}/command",
        "payload_press": "CANCEL",
        "icon": "mdi:cancel",
        "unique_id": uid("wolsca_print_cancel"),
        "device": DEVICE_INFO
    }
    mqtt_client.publish(f"{HA_PREFIX}/button/{NODE_ID}/cancel/config", json.dumps(config_cancel), retain=True)

    # Imported late: these modules import this one.
    try:
        import job_log
        job_log.publish_ha_discovery()
    except Exception as e:
        print(f"[Warning] Could not publish the job log discovery: {e}")

    try:
        import diagnostics
        diagnostics.publish_ha_discovery()
    except Exception as e:
        print(f"[Warning] Could not publish the diagnostics discovery: {e}")

    try:
        import updater
        updater.publish_ha_discovery()
    except Exception as e:
        print(f"[Warning] Could not publish the update discovery: {e}")

    try:
        import admin
        admin.publish_ha_discovery()
    except Exception as e:
        print(f"[Warning] Could not publish the admin discovery: {e}")

def set_state(state, detail="", pending_count=0, **fields):
    """Updates the global job state and publishes it to MQTT."""
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
        "flip_owner": snapshot["flip_owner"],
        "queued": pending_count,
        "version": SERVICE_VERSION,
        "timestamp": snapshot["updated"]
    })
    mqtt_client.publish(f"{PREFIX}/status", payload, retain=True)
    print(f"[State] -> {state} {f'({detail})' if detail else ''}")

def publish_metrics(filename, page_count, mode, processing_time):
    """Publishes performance metrics."""
    metrics = {
        "filename": filename,
        "page_count": page_count,
        "mode": mode,
        "processing_time_ms": processing_time,
        "timestamp": datetime.now().isoformat()
    }
    mqtt_client.publish(f"{PREFIX}/metrics", json.dumps(metrics))

def publish_log(message, level="info"):
    """Publishes a structured log event to the MQTT broker for external monitoring."""
    try:
        payload = json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "level": level,
            "message": message
        })
        mqtt_client.publish(f"{PREFIX}/log", payload)
    except Exception as e:
        print(f"[Error] Failed to publish MQTT log: {e}")

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        broker_ip = config.get_config()["mqtt"]["broker_ip"]
        print(f"[MQTT] Successfully connected to broker at {broker_ip}")
        publish_ha_discovery()
        client.subscribe(f"{PREFIX}/command")
        client.subscribe(f"{PREFIX}/admin/set/#")
        set_state("IDLE", "Service started and synchronized with HA.")
        publish_log("Service started and synchronized with Home Assistant.", "info")
    else:
        mqtt_user, _ = config.get_mqtt_credentials()
        print(f"[MQTT] Connection failed, return code {reason_code} "
              f"(account '{mqtt_user}'; create it on the broker or correct "
              f"mqtt.user/mqtt.password). Printing itself keeps working.")

def on_message(client, userdata, msg):
    global waiting_for_user_action
    payload = msg.payload.decode('utf-8')
    topic = msg.topic

    if topic.startswith(f"{PREFIX}/admin/set/"):
        # An administrator changed a setting from Home Assistant.
        import admin
        admin.handle_command(topic, payload)
        return

    import job_log

    if topic == f"{PREFIX}/command" and payload == "RESUME":
        with state_lock:
            owner = job_state["flip_owner"]
        if owner == "printer":
            print("[System] 'RESUME' ignored: the flip is confirmed on the printer itself.")
            job_log.warn("flip", "Continue from Home Assistant ignored - this printer asks "
                                 "for the flip on its own panel")
        elif waiting_for_user_action:
            print("[System] 'RESUME' command received via MQTT. Continuing workflow...")
            job_log.step("flip", "Continue pressed in Home Assistant")
            waiting_for_user_action = False
        else:
            print("[System] 'RESUME' received while no job was waiting.")
            job_log.warn("flip", "Continue pressed in Home Assistant while no job was waiting")

    elif topic == f"{PREFIX}/command" and payload == "CANCEL":
        print("[System] 'CANCEL' command received via MQTT.")
        job_log.warn("cancel", "Cancel pressed in Home Assistant")
        request_cancel()

    elif topic == f"{PREFIX}/command" and payload == "REPRINT":
        print("[System] 'REPRINT' command received via MQTT.")
        job_log.step("flip", "Reprint of the front side requested in Home Assistant")
        request_reprint_front()

    elif topic == f"{PREFIX}/command" and payload.startswith("SELFTEST"):
        # SELFTEST, SELFTEST_CHAIN or SELFTEST:cups,printer
        import diagnostics
        if payload == "SELFTEST_CHAIN":
            phases = diagnostics.DEFAULT_PHASES + ["chain"]
        elif ":" in payload:
            phases = [p.strip() for p in payload.split(":", 1)[1].split(",") if p.strip()]
        else:
            phases = None
        print(f"[System] 'SELFTEST' command received via MQTT ({phases or 'default phases'}).")
        diagnostics.run_async(phases)

    elif topic == f"{PREFIX}/command" and payload in ("CHECK_UPDATE", "INSTALL_UPDATE",
                                                      "CHECK_TEST_BUILD", "INSTALL_TEST_BUILD",
                                                      "AUTOUPDATE_ON", "AUTOUPDATE_OFF"):
        import updater
        print(f"[System] '{payload}' command received via MQTT.")
        if payload == "CHECK_UPDATE":
            updater.check_async()
        elif payload == "INSTALL_UPDATE":
            updater.install_async()
        elif payload == "CHECK_TEST_BUILD":
            updater.check_test_build_async()
        elif payload == "INSTALL_TEST_BUILD":
            updater.install_test_build_async()
        else:
            updater.set_auto_update(payload == "AUTOUPDATE_ON")

    elif topic == f"{PREFIX}/command" and payload in ("RESTART_SERVICE", "RELOAD_CONFIG"):
        import admin
        print(f"[System] '{payload}' command received via MQTT.")
        if payload == "RESTART_SERVICE":
            admin.restart_async()
        else:
            admin.reload_config()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt():
    """Initializes and connects the MQTT client."""
    c = config.get_config()
    mqtt_user, mqtt_pass = config.get_mqtt_credentials()
    broker_ip = c["mqtt"]["broker_ip"]
    broker_port = c["mqtt"]["broker_port"]

    mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
    try:
        mqtt_client.connect(broker_ip, broker_port, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"[Warning] Could not connect to MQTT Broker. ({e})")

def stop_mqtt():
    """Stops the MQTT client loop."""
    mqtt_client.loop_stop()
    mqtt_client.disconnect()