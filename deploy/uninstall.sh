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
SUMMARY_NAME="owlet-summarize"
CONFIG_DIR="/etc/${SERVICE_NAME}"
UNIT_DIR="/etc/systemd/system"
UNITS=("${SERVICE_NAME}.service" "${SUMMARY_NAME}.timer"
       "${SUMMARY_NAME}.service")
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

for unit in "${UNITS[@]}"; do
    systemctl stop "$unit" 2>/dev/null || true
    systemctl disable "$unit" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${unit}"
done
systemctl daemon-reload
rm -rf "$OWLET_PREFIX"

if [ "$PURGE" = "yes" ]; then
    rm -rf "$CONFIG_DIR"
    userdel "$OWLET_USER" 2>/dev/null || true
    echo "Removed ${SERVICE_NAME}, ${SUMMARY_NAME}, the configuration and"
    echo "the ${OWLET_USER} user. Summaries in HomeAPI are untouched."
else
    echo "Removed ${SERVICE_NAME} and ${SUMMARY_NAME}."
    echo "Configuration kept in ${CONFIG_DIR}."
    echo "Run with --purge to remove it as well."
fi
