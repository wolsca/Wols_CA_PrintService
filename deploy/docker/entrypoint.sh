#!/usr/bin/env bash
#
# Wols CA Print Service - entrypoint of the Debian test container.
#
# Starts the same moving parts as the Debian installation (D-Bus, Avahi, CUPS,
# the cups-pdf intake queues and the service itself) so the code under test
# behaves exactly as it does on the server. The configuration is untouched: the
# service reads /etc/wolsca/WolsCAPrintService.json.
#
# Commands:
#   service            start CUPS and the print service (default)
#   self-test [phases] run the diagnostics phases and exit with their result
#   shell              start a shell with CUPS running
#
# With WOLSCA_VIRTUAL_OUTPUT=1 the physical output queue is replaced by a
# cups-pdf queue that writes into WOLSCA_VIRTUAL_OUTPUT_DIR (default
# /var/spool/wolsca/PrintOut). Nothing is sent to the real printer, so the whole
# chain - including the flip workflow - can be tested on a desktop.
#
set -euo pipefail

INSTALL_DIR=/opt/wolsca-print-service
CONFIG_PATH=${WOLSCA_CONFIG:-/etc/wolsca/WolsCAPrintService.json}
PYTHON="${INSTALL_DIR}/venv/bin/python"
# Home Assistant add-on: the Supervisor writes the options here and /data is the
# only persistent directory, so the configuration lives there instead of in
# /etc/wolsca. Its content and structure are identical to the Debian install.
ADDON_OPTIONS=/data/options.json

log() { echo "[Container] $*"; }

if [ -f "${ADDON_OPTIONS}" ]; then
    CONFIG_PATH=/data/WolsCAPrintService.json
    export WOLSCA_CONFIG="${CONFIG_PATH}"
fi

apply_addon_options() {
    [ -f "${ADDON_OPTIONS}" ] || return 0
    log "Home Assistant add-on detected; applying the add-on options."
    # When the Mosquitto add-on is used, the Supervisor hands the broker details
    # over through the mqtt service, so they do not have to be typed twice. The
    # Supervisor API is queried directly, so no bashio runtime is required.
    local service=""
    if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
        service=$(curl -fsS -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
                       http://supervisor/services/mqtt 2>/dev/null || true)
        if [ -n "${service}" ]; then
            log "MQTT broker details available from the Home Assistant mqtt service."
        fi
    fi
    # The service JSON is handed over as an argument, never through the shell
    # environment: a password may contain any character.
    "${PYTHON}" - "${CONFIG_PATH}" "${ADDON_OPTIONS}" "${service}" <<'PY'
import json, os, sys

config_path, options_path = sys.argv[1], sys.argv[2]
with open(config_path) as handle:
    data = json.load(handle)
with open(options_path) as handle:
    options = json.load(handle)
try:
    mqtt_service = (json.loads(sys.argv[3]).get("data") or {}) if len(sys.argv) > 3 and sys.argv[3] else {}
except ValueError:
    mqtt_service = {}

mqtt = data.setdefault("mqtt", {})
settings = data.setdefault("settings", {})
hardware = data.setdefault("hardware", {})
web = data.setdefault("web", {})
update = data.setdefault("update", {})

# An empty string means "keep what is in the configuration file", so the add-on
# options never wipe a working setting.
mapping = [
    ("mqtt_broker", mqtt, "broker_ip"),
    ("mqtt_port", mqtt, "broker_port"),
    ("mqtt_user", settings, "user"),
    ("mqtt_password", settings, "password"),
    ("mqtt_topic_prefix", mqtt, "topic_prefix"),
    ("mqtt_discovery_prefix", mqtt, "discovery_prefix"),
    ("printer_uri", hardware, "printer_uri"),
    ("flip_timeout_seconds", hardware, "flip_timeout_seconds"),
    ("print_mode", settings, "print_mode"),
    ("web_port", web, "port"),
    ("web_title", web, "title"),
    ("web_language", web, "language"),
    ("admin_token", web, "admin_token"),
]
applied = []
for option, section, key in mapping:
    value = options.get(option)
    if value is None or (isinstance(value, str) and not value.strip()):
        continue
    section[key] = value
    applied.append(key)

for option, key in (("auto_update", "auto_update"),
                    ("allow_test_builds", "allow_test_builds")):
    if option in options:
        update[key] = bool(options[option])
        applied.append(key)

# The Mosquitto add-on, only where the options left the field empty.
for service_key, section, key, option in (("host", mqtt, "broker_ip", "mqtt_broker"),
                                          ("username", settings, "user", "mqtt_user"),
                                          ("password", settings, "password", "mqtt_password")):
    value = mqtt_service.get(service_key)
    option_value = options.get(option)
    if value in (None, ""):
        continue
    if isinstance(option_value, str) and option_value.strip():
        continue
    section[key] = value
    applied.append(f"{key} (mqtt service)")

# Running inside Home Assistant is what makes this the 'HA' instance: the topic
# prefix gets the HA_ marker and the instance label makes every entity id, the
# discovery node and the device unique. That is what lets this add-on and a
# Debian installation share one broker without colliding.
instance = str(options.get("instance_id")
               or os.environ.get("WOLSCA_INSTANCE_ID")
               or "HA").strip()
marker = f"{instance}_"
prefix = str(mqtt.get("topic_prefix") or "wolsca/printer")
if not prefix.startswith(marker):
    prefix = f"{marker}{prefix}"
mqtt["topic_prefix"] = prefix
mqtt["instance_id"] = instance
applied.append(f"instance_id={instance}")
applied.append(f"topic_prefix={prefix}")

with open(config_path, "w") as handle:
    json.dump(data, handle, indent=4)
print(f"[Container] Add-on options applied: {', '.join(applied) or 'nothing'}.")
PY
}

prepare_config() {
    if [ ! -f "${CONFIG_PATH}" ]; then
        log "No configuration yet, installing the shipped default at ${CONFIG_PATH}."
        mkdir -p "$(dirname "${CONFIG_PATH}")"
        cp "${INSTALL_DIR}/WolsCAPrintService.default.json" "${CONFIG_PATH}"
    fi
    # Only the connection details are taken from the environment, so no
    # credentials have to be committed; everything else stays as configured.
    if [ -n "${WOLSCA_MQTT_BROKER:-}${WOLSCA_MQTT_USER:-}${WOLSCA_MQTT_PASSWORD:-}${WOLSCA_MQTT_PREFIX:-}${WOLSCA_ADMIN_TOKEN:-}${WOLSCA_VIRTUAL_OUTPUT:-}" ]; then
        "${PYTHON}" - "$CONFIG_PATH" <<'PY'
import json, os, sys

path = sys.argv[1]
with open(path) as handle:
    data = json.load(handle)

mqtt = data.setdefault("mqtt", {})
settings = data.setdefault("settings", {})
web = data.setdefault("web", {})
hardware = data.setdefault("hardware", {})
if os.environ.get("WOLSCA_VIRTUAL_OUTPUT") == "1":
    # The virtual printer: the installer creates a raw queue with the
    # 'wolscafile' backend, which copies the PDF into this directory instead of
    # sending it to the real printer. Nothing else in the configuration changes.
    target = os.environ.get("WOLSCA_VIRTUAL_OUTPUT_DIR", "/var/spool/wolsca/PrintOut")
    hardware["printer_uri"] = f"wolscafile:{target}"
    print(f"[Container] Virtual output: hardware.printer_uri -> wolscafile:{target}")

mapping = [
    ("WOLSCA_MQTT_BROKER", mqtt, "broker_ip"),
    ("WOLSCA_MQTT_PREFIX", mqtt, "topic_prefix"),
    ("WOLSCA_MQTT_USER", settings, "user"),
    ("WOLSCA_MQTT_PASSWORD", settings, "password"),
    ("WOLSCA_ADMIN_TOKEN", web, "admin_token"),
]
applied = []
for variable, section, key in mapping:
    value = os.environ.get(variable)
    if value:
        section[key] = value
        applied.append(key)

with open(path, "w") as handle:
    json.dump(data, handle, indent=4)
print(f"[Container] Applied from the environment: {', '.join(applied) or 'nothing'}.")
PY
    fi
    # The add-on options win over the file, so what is configured in Home
    # Assistant is what the service runs with.
    apply_addon_options
    if [ -x "${INSTALL_DIR}/fix-permissions.sh" ]; then
        WOLSCA_CONFIG="${CONFIG_PATH}" "${INSTALL_DIR}/fix-permissions.sh" || \
            log "fix-permissions.sh reported a problem (harmless in a container)."
    fi
}

start_dbus_avahi() {
    mkdir -p /run/dbus
    rm -f /run/dbus/pid
    dbus-daemon --system --fork 2>/dev/null || log "D-Bus not started."
    avahi-daemon --daemonize --no-drop-root 2>/dev/null || log "Avahi not started (no mDNS in this container)."
}

wait_for_cups() {
    for _ in $(seq 1 40); do
        if lpstat -r >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

start_cups() {
    mkdir -p /run/cups /var/log/cups
    log "Starting cupsd..."
    # cupsd exits by design whenever its configuration changes (cupsctl,
    # lpadmin sharing changes); on the server systemd starts it again, here this
    # supervisor loop does. Without it the scheduler would be gone right after
    # the installer enabled sharing.
    (
        while true; do
            /usr/sbin/cupsd -f || log "cupsd exited ($?), starting it again."
            sleep 1
        done
    ) &
    if wait_for_cups; then
        log "cupsd is up."
    else
        log "cupsd did not answer in time; continuing anyway."
    fi
}

check_virtual_output() {
    # prepare_config already pointed hardware.printer_uri at wolscafile:, so the
    # installer created the output queue with that backend. Only verify it here:
    # printing on the real printer during an automated test is unacceptable.
    local dir="${WOLSCA_VIRTUAL_OUTPUT_DIR:-/var/spool/wolsca/PrintOut}"
    mkdir -p "${dir}"
    chmod 0777 "${dir}" 2>/dev/null || true

    local devices
    devices=$(lpstat -v 2>/dev/null | grep -i "wolsca_output" || true)
    if echo "${devices}" | grep -q "wolscafile:"; then
        log "Virtual output active: ${devices}"
        return 0
    fi
    log "[Error] The output queue does not use the virtual printer (${devices:-no queue found})."
    return 1
}

install_queues() {
    log "Creating the intake queues (same installer as on Debian)..."
    WOLSCA_CONFIG="${CONFIG_PATH}" "${PYTHON}" -c \
        "import installer; installer.perform_cups_printer_install()" || \
        log "The printer installation reported a problem; see the output above."
    # cupsctl and lpadmin make cupsd restart itself; wait until it answers again.
    wait_for_cups || log "cupsd is not answering after the installation."
    if [ "${WOLSCA_VIRTUAL_OUTPUT:-0}" = "1" ]; then
        # Without the virtual queue the test would print on the real printer, so
        # the container refuses to continue.
        check_virtual_output || exit 1
    fi
}

case "${1:-service}" in
    service)
        prepare_config
        start_dbus_avahi
        start_cups
        install_queues
        log "Starting the print service..."
        cd "${INSTALL_DIR}"
        exec "${PYTHON}" main.py
        ;;
    self-test)
        shift || true
        prepare_config
        start_dbus_avahi
        start_cups
        install_queues
        cd "${INSTALL_DIR}"
        # Run the service alongside the tests, so the web app and the queue
        # watcher are live for the network and chain phases.
        "${PYTHON}" main.py &
        sleep 6
        if [ "$#" -gt 0 ]; then
            exec "${PYTHON}" main.py --self-test "$@"
        fi
        exec "${PYTHON}" main.py --self-test --all
        ;;
    shell)
        prepare_config
        start_dbus_avahi
        start_cups
        exec /bin/bash
        ;;
    *)
        exec "$@"
        ;;
esac
