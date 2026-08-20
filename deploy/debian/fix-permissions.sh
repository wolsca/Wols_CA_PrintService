#!/usr/bin/env bash
#
# Wols CA Print Service - ownership and permission repair.
#
# Applies the intended owner, group and mode to every location the service
# touches: the program directory, the configuration, the spool directories
# (including the per-queue drop folders taken from the configuration) and the
# history file. Safe to re-run; install.sh calls it as its last step.
#
# Usage: sudo ./deploy/debian/fix-permissions.sh [--quiet]
#
set -euo pipefail

SERVICE_NAME="wolsca-print-service"
INSTALL_DIR="/opt/${SERVICE_NAME}"
CONFIG_DIR="/etc/wolsca"
CONFIG_FILE="${CONFIG_DIR}/WolsCAPrintService.json"
SPOOL_DIR="/var/spool/wolsca"
SERVICE_USER="root"
QUIET="no"

for arg in "$@"; do
    case "${arg}" in
        --quiet) QUIET="yes" ;;
        *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

say() {
    [[ "${QUIET}" == "yes" ]] || echo "    $*"
}

# The group CUPS and cups-pdf run as; 'lp' on Debian/Ubuntu.
SPOOL_GROUP="lp"
getent group "${SPOOL_GROUP}" >/dev/null 2>&1 || SPOOL_GROUP="${SERVICE_USER}"

# apply_dir <path> <mode>
apply_dir() {
    local path="$1" mode="$2"
    [[ -n "${path}" ]] || return 0
    install -d -o "${SERVICE_USER}" -g "${SPOOL_GROUP}" -m "${mode}" "${path}"
    chown "${SERVICE_USER}:${SPOOL_GROUP}" "${path}"
    chmod "${mode}" "${path}"
    say "dir  ${mode} ${SERVICE_USER}:${SPOOL_GROUP} ${path}"
}

# apply_file <path> <mode> [owner] [group]
apply_file() {
    local path="$1" mode="$2" owner="${3:-${SERVICE_USER}}" group="${4:-${SERVICE_USER}}"
    [[ -f "${path}" ]] || return 0
    chown "${owner}:${group}" "${path}"
    chmod "${mode}" "${path}"
    say "file ${mode} ${owner}:${group} ${path}"
}

# read_paths - prints every directory the configuration refers to, one per line.
read_paths() {
    [[ -f "${CONFIG_FILE}" ]] || return 0
    python3 - "${CONFIG_FILE}" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as handle:
        cfg = json.load(handle)
except Exception:
    sys.exit(0)
paths = cfg.get("paths", {})
for key in ("drop_directory", "temp_directory", "error_directory"):
    value = paths.get(key)
    if value:
        print("DIR\t" + value)
for queue in cfg.get("intake", {}).get("queues", []):
    value = queue.get("directory")
    if value:
        print("DIR\t" + value)
history = cfg.get("history", {}).get("file")
if history:
    print("FILE\t" + history)
PY
}

echo "==> Repairing ownership and permissions"

# 1. Program directory - readable by everyone, writable by root only.
if [[ -d "${INSTALL_DIR}" ]]; then
    chown -R root:root "${INSTALL_DIR}"
    chmod 0755 "${INSTALL_DIR}"
    find "${INSTALL_DIR}" -maxdepth 1 -type f -name '*.py' -exec chmod 0644 {} +
    find "${INSTALL_DIR}" -maxdepth 1 -type f -name '*.json' -exec chmod 0644 {} +
    if [[ -f "${INSTALL_DIR}/requirements.txt" ]]; then
        chmod 0644 "${INSTALL_DIR}/requirements.txt"
    fi
    for name in VERSION BUILD_NUMBER; do
        if [[ -f "${INSTALL_DIR}/${name}" ]]; then
            chmod 0644 "${INSTALL_DIR}/${name}"
        fi
    done
    if [[ -d "${INSTALL_DIR}/venv" ]]; then
        chmod 0755 "${INSTALL_DIR}/venv"
    fi
    say "dir  0755 root:root ${INSTALL_DIR} (files 0644)"
else
    echo "    [Warning] ${INSTALL_DIR} does not exist - run install.sh first." >&2
fi

# 2. Configuration - the directory must be traversable, the file writable by
#    the service (it rewrites the printer target) and readable for diagnostics.
if [[ -d "${CONFIG_DIR}" ]]; then
    chown root:root "${CONFIG_DIR}"
    chmod 0755 "${CONFIG_DIR}"
    say "dir  0755 root:root ${CONFIG_DIR}"
fi
apply_file "${CONFIG_FILE}" 0664 root root
if [[ ! -f "${CONFIG_FILE}" ]]; then
    echo "    [Warning] ${CONFIG_FILE} is missing." >&2
fi

# 3. Spool root and the fixed spool directories.
#    setgid (2775) so files created by cups-pdf keep the lp group.
apply_dir "${SPOOL_DIR}" 2775
for name in PrintFileDrop PrintTemp PrintError; do
    apply_dir "${SPOOL_DIR}/${name}" 2775
done
for name in booklet duplex simplex; do
    apply_dir "${SPOOL_DIR}/PrintFileDrop/${name}" 2775
done

# 4. Whatever the live configuration actually points at.
while IFS=$'\t' read -r kind value; do
    [[ -n "${value:-}" ]] || continue
    case "${kind}" in
        DIR)  apply_dir "${value}" 2775 ;;
        FILE) apply_file "${value}" 0664 ;;
    esac
done < <(read_paths)

# 5. Existing spooled files - make sure a job dropped by cups-pdf under a
#    different umask stays readable and removable by the service.
if [[ -d "${SPOOL_DIR}" ]]; then
    chown -R "${SERVICE_USER}:${SPOOL_GROUP}" "${SPOOL_DIR}"
    find "${SPOOL_DIR}" -type f -exec chmod 0664 {} + 2>/dev/null || true
    find "${SPOOL_DIR}" -type d -exec chmod 2775 {} + 2>/dev/null || true
    say "recursive fixup applied to ${SPOOL_DIR}"
fi

# 6. systemd unit and the drop-in directory.
apply_file "/etc/systemd/system/${SERVICE_NAME}.service" 0644 root root
if [[ -d "/etc/systemd/system/${SERVICE_NAME}.service.d" ]]; then
    chmod 0755 "/etc/systemd/system/${SERVICE_NAME}.service.d"
    find "/etc/systemd/system/${SERVICE_NAME}.service.d" -type f -exec chmod 0644 {} +
fi

# 7. cups-pdf must be allowed to write into the drop directories.
if [[ -d /etc/cups ]]; then
    for conf in /etc/cups/cups-pdf.conf /etc/cups/cups-pdf-*.conf; do
        [[ -f "${conf}" ]] || continue
        apply_file "${conf}" 0644 root "${SPOOL_GROUP}"
    done
fi

echo "    Ownership and permissions are in order."
