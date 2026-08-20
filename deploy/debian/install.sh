#!/usr/bin/env bash
#
# Wols CA Print Service - Debian / Ubuntu installer.
#
# Installs the service into /opt/wolsca-print-service with its own virtualenv,
# deploys the configuration to /etc/wolsca, creates the spool directories and
# registers the systemd unit. Safe to re-run (idempotent).
#
# Tested on Debian 12/13 and Ubuntu 22.04/24.04 (any apt based derivative).
#
# Usage: sudo ./deploy/debian/install.sh
#
set -euo pipefail

SERVICE_NAME="wolsca-print-service"
INSTALL_DIR="/opt/${SERVICE_NAME}"
CONFIG_DIR="/etc/wolsca"
SPOOL_DIR="/var/spool/wolsca"
SERVICE_USER="root"

if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer must run as root (use sudo)." >&2
    exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer needs apt-get (Debian, Ubuntu or a derivative)." >&2
    exit 1
fi

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "==> Detected ${PRETTY_NAME:-unknown distribution}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="${REPO_ROOT}/Wols_CA_PrintService"

echo "==> 1/7 Installing OS packages and freeing IPP port 631"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip avahi-daemon avahi-utils

# Ensure legacy CUPS is stopped and disabled so our Native Python IPP Server can bind to port 631
if systemctl is-active --quiet cups; then
    echo "    Stopping legacy CUPS service to free port 631..."
    systemctl stop cups cups.socket || true
    systemctl disable cups cups.socket || true
fi

# Bonjour/mDNS makes the web app and the native print queue discoverable as <host>.local.
systemctl enable --now avahi-daemon || true

echo "==> 2/7 Creating service user '${SERVICE_USER}'"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin \
            --groups lp "${SERVICE_USER}"
else
    usermod -aG lp "${SERVICE_USER}" || true
fi

echo "==> 3/7 Creating directories"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
for dir in PrintFileDrop PrintTemp PrintError; do
    install -d -o "${SERVICE_USER}" -g lp -m 2775 "${SPOOL_DIR}/${dir}"
done
# One drop directory per visible print queue
for dir in booklet duplex simplex; do
    install -d -o "${SERVICE_USER}" -g lp -m 2775 "${SPOOL_DIR}/PrintFileDrop/${dir}"
done
chown "${SERVICE_USER}:lp" "${SPOOL_DIR}"
chmod 2775 "${SPOOL_DIR}"

echo "==> 4/7 Copying application files"
# Copy all modular components
install -o root -g root -m 0644 "${SRC_DIR}/config.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/file_watcher.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/hardware_dispatcher.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/installer.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/ipp_server.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/main.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/mqtt_service.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/pdf_processor.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/web_app.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/web_strings.json" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${REPO_ROOT}/requirements.txt" "${INSTALL_DIR}/"

if [[ ! -f "${CONFIG_DIR}/WolsCAPrintService.json" ]]; then
    install -o root -g "${SERVICE_USER}" -m 0660 \
        "${REPO_ROOT}/deploy/debian/WolsCAPrintService.linux.json" \
        "${CONFIG_DIR}/WolsCAPrintService.json"
    echo "    Installed default configuration - review ${CONFIG_DIR}/WolsCAPrintService.json"
else
    chmod 0660 "${CONFIG_DIR}/WolsCAPrintService.json"
    echo "    Existing configuration kept: ${CONFIG_DIR}/WolsCAPrintService.json"
fi

echo "==> 5/7 Creating the Python virtualenv"
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/venv/bin/pip" install segno || \
    echo "    (optional 'segno' package not installed; /qr shows the plain URL)"

echo "==> 6/7 Configuring mDNS (Avahi) for Native IPP Discovery"
# Broadcasts the native Python IPP server to Apple iOS and Android devices
cat <<EOF > /etc/avahi/services/wolsca-ipp.service
<?xml version="1.0" standalone='no'?><!--*-nxml-*-->
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">Wols CA Native Print Server on %h</name>
  <service>
    <type>_ipp._tcp</type>
    <subtype>_universal._sub._ipp._tcp</subtype>
    <port>631</port>
    <txt-record>txtvers=1</txt-record>
    <txt-record>qtotal=1</txt-record>
    <txt-record>pdl=application/pdf</txt-record>
    <txt-record>rp=printers/WolsCA_Booklet</txt-record>
  </service>
</service-group>
EOF
systemctl reload-or-restart avahi-daemon || true

echo "==> 7/7 Registering the systemd unit"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/deploy/debian/${SERVICE_NAME}.service" \
    "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

WEB_PORT="$(python3 - "${CONFIG_DIR}/WolsCAPrintService.json" <<'PY' 2>/dev/null || echo 8080
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get("web", {}).get("port", 8080))
except Exception:
    print(8080)
PY
)"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    echo "==> Opening the firewall (ufw) for IPP, mDNS and the web app"
    ufw allow 631/tcp   >/dev/null || true
    ufw allow 5353/udp  >/dev/null || true
    ufw allow "${WEB_PORT}/tcp" >/dev/null || true
fi

echo
echo "Installation complete."
echo "  Status:  systemctl status ${SERVICE_NAME}"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Config:  ${CONFIG_DIR}/WolsCAPrintService.json"
echo "  Web app: http://$(hostname).local:${WEB_PORT}/"
echo "  QR code: http://$(hostname).local:${WEB_PORT}/qr"
echo "  Native IPP Endpoint: ipp://$(hostname).local:631/printers/WolsCA_Booklet"
echo
echo "The native Zero-Trust Python IPP server is now active on port 631."