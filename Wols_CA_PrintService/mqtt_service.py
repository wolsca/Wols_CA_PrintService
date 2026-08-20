import json
import threading
from datetime import datetime
import paho.mqtt.client as mqtt
import config

SERVICE_VERSION = "1.4.2"

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
    "updated": datetime.now().isoformat()
}

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
PREFIX = c["mqtt"].get("topic_prefix", "wols_ca/printer_servic")
HA_PREFIX = c["mqtt"].get("discovery_prefix", "homeassistant")

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

DEVICE_INFO = {
    "identifiers": ["wolsca_print_service_01"],
    "name": "Wols CA Print Service",
    "manufacturer": "Wols CA",
    "model": "Double Sided Spooler v1.4"
}

def publish_ha_discovery():
    """Publishes Home Assistant auto-discovery payloads."""
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
        set_state("IDLE", "Service started and synchronized with HA.")
        publish_log("Service started and synchronized with Home Assistant.", "info")
    else:
        print(f"[MQTT] Connection failed, return code {reason_code}")

def on_message(client, userdata, msg):
    global waiting_for_user_action
    payload = msg.payload.decode('utf-8')
    topic = msg.topic

    if topic == f"{PREFIX}/command" and payload == "RESUME":
        if waiting_for_user_action:
            print("[System] 'RESUME' command received via MQTT. Continuing workflow...")
            publish_log("Resume command received via MQTT. Continuing workflow.", "info")
            waiting_for_user_action = False

    elif topic == f"{PREFIX}/command" and payload == "CANCEL":
        print("[System] 'CANCEL' command received via MQTT.")
        publish_log("Cancel command received via MQTT.", "warning")
        request_cancel()

    elif topic == f"{PREFIX}/command" and payload == "REPRINT":
        print("[System] 'REPRINT' command received via MQTT.")
        publish_log("Reprint command received via MQTT.", "info")
        request_reprint_front()

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt():
    """Initializes and connects the MQTT client."""
    c = config.get_config()
    mqtt_user = c["settings"]["user"]
    mqtt_pass = c["settings"]["password"]
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