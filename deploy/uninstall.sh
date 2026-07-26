#!/usr/bin/env bash
#
# Remove the owlet-homeapi systemd service.
#
#   curl -fsSL https://raw.githubusercontent.com/chinmay28/owlet/master/deploy/uninstall.sh | sudo bash
#
# The configuration in /etc/owlet-homeapi is kept unless --purge is given.

set -euo pipefail

OWLET_PREFIX="${OWLET_PREFIX:-/opt/owlet-homeapi}"
OWLET_USER="${OWLET_USER:-owlet}"
SERVICE_NAME="owlet-homeapi"
CONFIG_DIR="/etc/${SERVICE_NAME}"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PURGE="no"

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE="yes" ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "This uninstaller must run as root." >&2
    exit 1
fi

systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl daemon-reload
rm -rf "$OWLET_PREFIX"

if [ "$PURGE" = "yes" ]; then
    rm -rf "$CONFIG_DIR"
    userdel "$OWLET_USER" 2>/dev/null || true
    echo "Removed ${SERVICE_NAME}, its configuration and the ${OWLET_USER} user."
else
    echo "Removed ${SERVICE_NAME}. Configuration kept in ${CONFIG_DIR}."
    echo "Run with --purge to remove it as well."
fi
