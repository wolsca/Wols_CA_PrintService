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
# Usage: sudo ./deploy/debian/install.sh [--with-cups]
#
set -euo pipefail

SERVICE_NAME="wolsca-print-service"
INSTALL_DIR="/opt/${SERVICE_NAME}"
CONFIG_DIR="/etc/wolsca"
SPOOL_DIR="/var/spool/wolsca"
SERVICE_USER="root"
WITH_CUPS="no"

for arg in "$@"; do
    case "${arg}" in
        --with-cups) WITH_CUPS="yes" ;;
        *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
    esac
done

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

echo "==> 1/7 Installing OS packages and managing port 631"
export DEBIAN_FRONTEND=noninteractive
apt-get update

if [[ "${WITH_CUPS}" == "yes" ]]; then
    echo "    [Hybrid Mode] Installing CUPS and dependencies..."
    apt-get install -y python3 python3-venv python3-pip avahi-daemon avahi-utils cups printer-driver-cups-pdf cups-client cups-ipp-utils

    # Ensure CUPS is running and shared to the network
    systemctl enable --now cups
    cupsctl --share-printers --remote-any || true
else
    echo "    [Native Mode] Installing base dependencies..."
    apt-get install -y python3 python3-venv python3-pip avahi-daemon avahi-utils

    # Ensure legacy CUPS is stopped so Python can bind to port 631
    if systemctl is-active --quiet cups; then
        echo "    Stopping legacy CUPS service to free port 631..."
        systemctl stop cups cups.socket || true
        systemctl disable cups cups.socket || true
    fi
fi

# Bonjour/mDNS discovery
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
install -o root -g root -m 0644 "${SRC_DIR}/config.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/diagnostics.py" "${INSTALL_DIR}/"
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
    install -o root -g "${SERVICE_USER}" -m 0666 \
        "${REPO_ROOT}/deploy/debian/WolsCAPrintService.linux.json" \
        "${CONFIG_DIR}/WolsCAPrintService.json"
    echo "    Installed default configuration - review ${CONFIG_DIR}/WolsCAPrintService.json"
else
    # Correct permissions on existing file to prevent Errno 13
    chmod 0666 "${CONFIG_DIR}/WolsCAPrintService.json"
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

echo "==> 6/7 Configuring Architecture-Specific Network Settings"
mkdir -p /etc/systemd/system/${SERVICE_NAME}.service.d

if [[ "${WITH_CUPS}" == "yes" ]]; then
    echo "    [Hybrid Mode] Deploying CUPS queues and cleaning up native overrides..."
    rm -f /etc/avahi/services/wolsca-ipp.service
    rm -f /etc/systemd/system/${SERVICE_NAME}.service.d/override.conf
    systemctl reload-or-restart avahi-daemon || true

    WOLSCA_CONFIG="${CONFIG_DIR}/WolsCAPrintService.json" \
        "${INSTALL_DIR}/venv/bin/python" \
        "${INSTALL_DIR}/main.py" --install-printer
else
    echo "    [Native Mode] Configuring mDNS (Avahi) and setting port capabilities..."
    echo -e "[Service]\nAmbientCapabilities=CAP_NET_BIND_SERVICE" > /etc/systemd/system/${SERVICE_NAME}.service.d/override.conf

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
fi

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
if [[ "${WITH_CUPS}" == "yes" ]]; then
    echo "  Architecture: Hybrid (CUPS intake, Python processing)"
else
    echo "  Architecture: Native Zero-Trust IPP (Python standalone)"
fi