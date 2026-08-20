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

log() { echo "[Container] $*"; }

prepare_config() {
    if [ ! -f "${CONFIG_PATH}" ]; then
        log "No configuration yet, installing the shipped default at ${CONFIG_PATH}."
        mkdir -p "$(dirname "${CONFIG_PATH}")"
        cp "${INSTALL_DIR}/WolsCAPrintService.default.json" "${CONFIG_PATH}"
    fi
    # Only the connection details are taken from the environment, so no
    # credentials have to be committed; everything else stays as configured.
    if [ -n "${WOLSCA_MQTT_BROKER:-}${WOLSCA_MQTT_USER:-}${WOLSCA_MQTT_PASSWORD:-}${WOLSCA_MQTT_PREFIX:-}${WOLSCA_ADMIN_TOKEN:-}" ]; then
        "${PYTHON}" - "$CONFIG_PATH" <<'PY'
import json, os, sys

path = sys.argv[1]
with open(path) as handle:
    data = json.load(handle)

mqtt = data.setdefault("mqtt", {})
settings = data.setdefault("settings", {})
web = data.setdefault("web", {})
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

virtual_output_queue() {
    local dir="${WOLSCA_VIRTUAL_OUTPUT_DIR:-/var/spool/wolsca/PrintOut}"
    local queue
    queue=$(cd "${INSTALL_DIR}" && WOLSCA_CONFIG="${CONFIG_PATH}" "${PYTHON}" -c \
        "import config; print(config.get_config().get('hardware', {}).get('cups_queue_name') or 'WolsCA_Output')" 2>/dev/null)
    queue=${queue:-WolsCA_Output}
    log "Virtual output: '${queue}' writes PDF files into ${dir} instead of the printer."

    local device
    device=$(cd "${INSTALL_DIR}" && WOLSCA_CONFIG="${CONFIG_PATH}" "${PYTHON}" -c \
        "import installer; print(installer.ensure_cups_pdf_instance({'id': 'output', 'directory': '${dir}'}))")
    local ppd
    ppd=$(cd "${INSTALL_DIR}" && "${PYTHON}" -c \
        "import installer; print(installer.find_cups_pdf_ppd() or '')")

    if [ -n "${ppd}" ]; then
        lpadmin -p "${queue}" -v "${device}" -P "${ppd}" -E \
            -D "Wols CA virtual output printer (PDF)" -L "Wols CA Print Service"
    else
        lpadmin -p "${queue}" -v "${device}" -m everywhere -E \
            -D "Wols CA virtual output printer (PDF)" -L "Wols CA Print Service"
    fi
    cupsenable "${queue}" || true
    cupsaccept "${queue}" || true
    wait_for_cups || log "cupsd is not answering after creating the virtual output queue."
}

install_queues() {
    log "Creating the intake queues (same installer as on Debian)..."
    WOLSCA_CONFIG="${CONFIG_PATH}" "${PYTHON}" -c \
        "import installer; installer.perform_cups_printer_install()" || \
        log "The printer installation reported a problem; see the output above."
    # cupsctl and lpadmin make cupsd restart itself; wait until it answers again.
    wait_for_cups || log "cupsd is not answering after the installation."
    if [ "${WOLSCA_VIRTUAL_OUTPUT:-0}" = "1" ]; then
        virtual_output_queue
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
