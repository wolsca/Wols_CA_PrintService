#!/usr/bin/env bash
#
# Removes the Wols CA Print Service from a Debian/Ubuntu host.
# Configuration (/etc/wolsca) and spooled files (/var/spool/wolsca) are kept
# unless --purge is given.
#
# Usage: sudo ./deploy/debian/uninstall.sh [--purge]
#
set -euo pipefail

SERVICE_NAME="wolsca-print-service"
INSTALL_DIR="/opt/${SERVICE_NAME}"
CONFIG_DIR="/etc/wolsca"
SPOOL_DIR="/var/spool/wolsca"
SERVICE_USER="wolsca"
PURGE="no"

for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE="yes" ;;
        *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

echo "==> Stopping and disabling the service"
systemctl disable --now "${SERVICE_NAME}.service" || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

echo "==> Removing ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR}"

if [[ -f /etc/avahi/services/wolsca-print-web.service ]]; then
    echo "==> Removing the mDNS advertisement of the web app"
    rm -f /etc/avahi/services/wolsca-print-web.service
    systemctl reload-or-restart avahi-daemon || true
fi

if [[ "${PURGE}" == "yes" ]]; then
    echo "==> Purging configuration, spool directories and the CUPS queues"
    QUEUES="$(python3 - "$CONFIG_DIR/WolsCAPrintService.json" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
    names = [q.get("cups_queue", "") for q in data.get("intake", {}).get("queues", [])]
    names.append(data.get("virtual_printer", {}).get("cups_queue_name", ""))
    names.append(data.get("hardware", {}).get("cups_queue_name", "") or "WolsCA_Output")
    print("\n".join(sorted({n for n in names if n})))
except Exception:
    print("")
PY
)"
    for queue in ${QUEUES}; do
        lpadmin -x "${queue}" || true
    done
    # Remove the per-queue cups-pdf backend instances and their configuration.
    for backend_dir in /usr/lib/cups/backend /usr/libexec/cups/backend; do
        [[ -d "${backend_dir}" ]] || continue
        find "${backend_dir}" -maxdepth 1 -name 'cups-pdf-*' -type l -delete || true
    done
    # The names up to 1.4 (duplex/simplex) are removed as well.
    rm -f /etc/cups/cups-pdf-booklet.conf \
          /etc/cups/cups-pdf-doublesided.conf /etc/cups/cups-pdf-singlesided.conf \
          /etc/cups/cups-pdf-duplex.conf /etc/cups/cups-pdf-simplex.conf
    rm -rf "${CONFIG_DIR}" "${SPOOL_DIR}"
    userdel "${SERVICE_USER}" 2>/dev/null || true
else
    echo "==> Keeping ${CONFIG_DIR} and ${SPOOL_DIR} (use --purge to remove)"
fi

echo "Uninstall complete."
