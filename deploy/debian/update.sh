#!/usr/bin/env bash
#
# Wols CA Print Service - update the checkout and re-install.
#
# This is the manual counterpart of the 'Update now' button: it does exactly
# what updater.py runs, and nothing else.
#
#   git fetch --all --tags --prune
#   git reset --hard origin/<branch>
#   chmod +x deploy/debian/*.sh
#   deploy/debian/install.sh [extra options]
#   systemctl status wolsca-print-service
#
# Usage: sudo ./deploy/debian/update.sh [--branch <name>] [install.sh options]
#
# Every option that is not --branch is passed on to install.sh, so
# 'sudo ./deploy/debian/update.sh --without-cups' works as well. CUPS is
# installed by default; --with-cups is accepted and does nothing.
#
# WARNING: 'git reset --hard' throws away local changes in the checkout. The
# configuration in /etc/wolsca is not part of the checkout, so it is kept.
#
set -euo pipefail

# Everything lives in one function that is called on the very last line: bash
# reads a script in chunks while it runs it, and 'git reset --hard' rewrites
# this very file halfway through. With the whole body parsed up front the
# running update cannot be cut off by its own update.
main() {
local BRANCH=""
local INSTALL_ARGS=()
local REPO_ROOT

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)
            BRANCH="${2:-}"
            if [[ -z "${BRANCH}" ]]; then
                echo "--branch needs a branch name." >&2
                exit 1
            fi
            shift 2
            ;;
        --branch=*)
            BRANCH="${1#*=}"
            shift
            ;;
        *)
            INSTALL_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}" || exit 1

if [[ ! -d .git ]]; then
    echo "${REPO_ROOT} is not a git checkout - nothing to update." >&2
    echo "The updater needs the checkout that update.source_directory points at." >&2
    exit 1
fi

# The branch the service itself updates from, so a manual run cannot silently
# pull another branch than the 'Update now' button does.
if [[ -z "${BRANCH}" ]]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
fi
if [[ -z "${BRANCH}" || "${BRANCH}" == "HEAD" ]]; then
    BRANCH="main"
fi

echo "==> Updating ${REPO_ROOT} from origin/${BRANCH}"
git fetch --all --tags --prune
git reset --hard "origin/${BRANCH}"
git --no-pager log -1 --format='    now at %h %s'

# A checkout from Windows, a ZIP download or a copy over SMB loses the
# executable bit, and 'git reset --hard' does not restore it either.
chmod +x "${REPO_ROOT}"/deploy/debian/*.sh 2>/dev/null || true

echo "==> Running the installer"
"${REPO_ROOT}/deploy/debian/install.sh" ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}

echo "==> Service status"
# The unit is running by now; --no-pager keeps this usable over ssh and the
# '|| true' stops a non-zero 'systemctl status' from failing the whole update.
systemctl --no-pager --full status wolsca-print-service || true
}

main "$@"
