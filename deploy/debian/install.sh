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

echo "==> 1/8 Installing OS packages and managing port 631"
export DEBIAN_FRONTEND=noninteractive
apt-get update

# A minimal Debian installation (netinst without any task selected) has none of
# the tools the service, the installer and the self-test call out to. Everything
# listed here is either imported, executed or needed to build the virtualenv, so
# the lists below are the complete runtime dependency set.
#
#   ca-certificates  HTTPS to GitHub (update check) and to the printer over ipps
#   curl / wget      update download and the Supervisor/ntfy calls
#   (git itself is a prerequisite: this script comes from the checkout)
#   iproute2         'ss -ltnp' in the network self-test phase
#   iputils-ping     'ping' in the printer self-test phase
#   procps / psmisc  process inspection, used while diagnosing a hung job
#   systemd          journalctl for the log step and the service unit itself
#   libnss-mdns      resolves <host>.local, so Windows/Android names work
#   python3-dev,     build fallback for the pip wheels when no binary wheel
#   build-essential  exists for the platform (arm64, unusual Python versions)
BASE_PACKAGES=(
    ca-certificates
    curl
    wget
    python3
    python3-venv
    python3-pip
    python3-setuptools
    python3-dev
    build-essential
    avahi-daemon
    avahi-utils
    libnss-mdns
    iproute2
    iputils-ping
    procps
    psmisc
    coreutils
    findutils
    grep
    sed
    tar
    gzip
    xz-utils
    less
    file
    systemd
    tzdata
    locales
)

#   cups-client      lp, lpstat, lpadmin, cupsenable, cupsaccept, cupsctl
#   cups-ipp-utils   ipptool, used by the dispatcher and the printer phase
#   cups-filters     the PDF -> printer filter chain of the output queue
#   ghostscript      needed by those filters for PDF/PostScript conversion
#   poppler-utils    pdftoppm/pdfinfo, used while inspecting incoming jobs
CUPS_PACKAGES=(
    cups
    cups-daemon
    cups-client
    cups-bsd
    cups-filters
    cups-ipp-utils
    printer-driver-cups-pdf
    ghostscript
    poppler-utils
)

# Debian 13 renamed or dropped a few of these, so an unknown package must not
# abort the whole installation: only what apt really knows is passed on.
install_packages() {
    local wanted=("$@")
    local available=() missing=()
    for pkg in "${wanted[@]}"; do
        if apt-cache show "${pkg}" >/dev/null 2>&1; then
            available+=("${pkg}")
        else
            missing+=("${pkg}")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "    Not offered by this distribution, skipped: ${missing[*]}"
    fi
    if [[ ${#available[@]} -gt 0 ]]; then
        apt-get install -y --no-install-recommends "${available[@]}"
    fi
}

if ! command -v git >/dev/null 2>&1; then
    echo "    [Warning] 'git' is not installed. It is a prerequisite of this checkout;"
    echo "              without it the update buttons cannot fetch a release."
fi

echo "    Installing base dependencies (${#BASE_PACKAGES[@]} packages)..."
install_packages "${BASE_PACKAGES[@]}"

if [[ "${WITH_CUPS}" == "yes" ]]; then
    echo "    [Hybrid Mode] Installing CUPS and the printing tool chain..."
    install_packages "${CUPS_PACKAGES[@]}"

    # Ensure CUPS is running and shared to the network
    systemctl enable --now cups
    cupsctl --share-printers --remote-any || true
else
    echo "    [Native Mode] Base dependencies only."

    # Ensure legacy CUPS is stopped so Python can bind to port 631
    if systemctl is-active --quiet cups; then
        echo "    Stopping legacy CUPS service to free port 631..."
        systemctl stop cups cups.socket || true
        systemctl disable cups cups.socket || true
    fi
fi

# Bonjour/mDNS discovery
systemctl enable --now avahi-daemon || true

echo "==> 2/8 Creating service user '${SERVICE_USER}'"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin \
            --groups lp "${SERVICE_USER}"
else
    usermod -aG lp "${SERVICE_USER}" || true
fi

echo "==> 3/8 Creating directories"
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
install -d -o root -g root -m 0755 "${CONFIG_DIR}"
for dir in PrintFileDrop PrintTemp PrintError; do
    install -d -o "${SERVICE_USER}" -g lp -m 2775 "${SPOOL_DIR}/${dir}"
done
# One drop directory per visible print queue
for dir in booklet duplex simplex; do
    install -d -o "${SERVICE_USER}" -g lp -m 2775 "${SPOOL_DIR}/PrintFileDrop/${dir}"
done
chown "${SERVICE_USER}:lp" "${SPOOL_DIR}"
chmod 2775 "${SPOOL_DIR}"

echo "==> 4/8 Copying application files"
install -o root -g root -m 0644 "${SRC_DIR}/admin.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/config.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/diagnostics.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/file_watcher.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/hardware_dispatcher.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/installer.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/ipp_server.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/main.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/mqtt_service.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/notifier.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/pdf_processor.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/printer_capabilities.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/updater.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/version.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/web_app.py" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${SRC_DIR}/web_strings.json" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${REPO_ROOT}/requirements.txt" "${INSTALL_DIR}/"

# The version files travel next to the modules, so the service reports the right
# version when it runs from /opt instead of from the checkout.
install -o root -g root -m 0644 "${REPO_ROOT}/VERSION" "${INSTALL_DIR}/"
install -o root -g root -m 0644 "${REPO_ROOT}/BUILD_NUMBER" "${INSTALL_DIR}/"

install -o root -g root -m 0755 "${REPO_ROOT}/deploy/debian/fix-permissions.sh" "${INSTALL_DIR}/"

if [[ ! -f "${CONFIG_DIR}/WolsCAPrintService.json" ]]; then
    install -o root -g root -m 0664 \
        "${REPO_ROOT}/deploy/debian/WolsCAPrintService.linux.json" \
        "${CONFIG_DIR}/WolsCAPrintService.json"
    echo "    Installed default configuration - review ${CONFIG_DIR}/WolsCAPrintService.json"
else
    echo "    Existing configuration kept: ${CONFIG_DIR}/WolsCAPrintService.json"
fi

echo "==> 5/8 Creating the Python virtualenv"
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/venv/bin/pip" install segno || \
    echo "    (optional 'segno' package not installed; /qr shows the plain URL)"

echo "==> 6/8 Configuring Architecture-Specific Network Settings"
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

echo "==> 7/8 Registering the systemd unit"
install -o root -g root -m 0644 \
    "${REPO_ROOT}/deploy/debian/${SERVICE_NAME}.service" \
    "/etc/systemd/system/${SERVICE_NAME}.service"

# The unit must run as the user this script actually created. A unit shipped
# with a different User= than SERVICE_USER makes systemd fail with
# 'status=217/USER' before Python is even started, which looks exactly like a
# service that starts and dies in a restart loop.
if [[ "${SERVICE_USER}" != "root" ]]; then
    sed -i -e "s/^User=.*/User=${SERVICE_USER}/" \
           -e "s/^Group=.*/Group=${SERVICE_USER}/" \
           "/etc/systemd/system/${SERVICE_NAME}.service"
fi
echo "    Unit runs as $(grep -m1 '^User=' "/etc/systemd/system/${SERVICE_NAME}.service" | cut -d= -f2)"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

echo "==> 8/8 Applying ownership and permissions to all locations"
# Runs last so it also covers what --install-printer just created or rewrote.
bash "${REPO_ROOT}/deploy/debian/fix-permissions.sh"

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
INSTALLED_VERSION="$(cat "${REPO_ROOT}/VERSION" 2>/dev/null || echo '0.0').$(cat "${REPO_ROOT}/BUILD_NUMBER" 2>/dev/null || echo 0)"
echo "Installation complete (version ${INSTALLED_VERSION})."
echo "  Status:  systemctl status ${SERVICE_NAME}"

echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Config:  ${CONFIG_DIR}/WolsCAPrintService.json"
echo "  Web app: http://$(hostname).local:${WEB_PORT}/"
if [[ "${WITH_CUPS}" == "yes" ]]; then
    echo "  Architecture: Hybrid (CUPS intake, Python processing)"
else
    echo "  Architecture: Native Zero-Trust IPP (Python standalone)"
fi