#!/usr/bin/env bash
#
# One line installer for the owlet-homeapi systemd service.
#
#   curl -fsSL https://raw.githubusercontent.com/chinmay28/owlet/master/deploy/install.sh \
#     | sudo OWLET_EMAIL=you@example.com OWLET_PASSWORD='secret' \
#            HOMEAPI_URL=http://homeapi.local:9999 bash
#
# Running the same command again upgrades an existing install in place and
# keeps the configuration in /etc/owlet-homeapi/owlet-homeapi.env.

set -euo pipefail

OWLET_REPO="${OWLET_REPO:-https://github.com/chinmay28/owlet.git}"
OWLET_REF="${OWLET_REF:-master}"
OWLET_PREFIX="${OWLET_PREFIX:-/opt/owlet-homeapi}"
OWLET_USER="${OWLET_USER:-owlet}"
SERVICE_NAME="owlet-homeapi"
CONFIG_DIR="/etc/${SERVICE_NAME}"
CONFIG_FILE="${CONFIG_DIR}/${SERVICE_NAME}.env"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SRC_DIR="${OWLET_PREFIX}/src"
VENV_DIR="${OWLET_PREFIX}/venv"

# Settings that live in the environment file. Any of them may be passed to
# this script as an environment variable to seed or update its value.
CONFIG_VARS=(
    OWLET_EMAIL
    OWLET_PASSWORD
    OWLET_POLL_INTERVAL
    OWLET_REACTIVATE_INTERVAL
    OWLET_DEVICE
    OWLET_ATTRIBUTES
    OWLET_PUBLISH_MODE
    OWLET_LOG_LEVEL
    HOMEAPI_URL
    HOMEAPI_CATEGORY
    HOMEAPI_KEY_PREFIX
    HOMEAPI_TIMEOUT
)

# Defaults for anything neither configured before nor passed in.
declare -A CONFIG_DEFAULTS=(
    [OWLET_EMAIL]=""
    [OWLET_PASSWORD]=""
    [OWLET_POLL_INTERVAL]="2"
    [OWLET_REACTIVATE_INTERVAL]="10"
    [OWLET_DEVICE]=""
    [OWLET_ATTRIBUTES]=""
    [OWLET_PUBLISH_MODE]="latest"
    [OWLET_LOG_LEVEL]="info"
    [HOMEAPI_URL]="http://localhost:9999"
    [HOMEAPI_CATEGORY]="owlet"
    [HOMEAPI_KEY_PREFIX]="owlet"
    [HOMEAPI_TIMEOUT]="10"
)

log() {
    printf '\033[1;32m==>\033[0m %s\n' "$*"
}

warn() {
    printf '\033[1;33m==> WARNING:\033[0m %s\n' "$*" >&2
}

die() {
    printf '\033[1;31m==> ERROR:\033[0m %s\n' "$*" >&2
    exit 1
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "This installer must run as root, pipe it into 'sudo bash'."
    fi

    command -v systemctl >/dev/null 2>&1 ||
        die "systemd is required but systemctl was not found."
}

install_prereqs() {
    local missing=()

    command -v git >/dev/null 2>&1 || missing+=(git)
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    python3 -c 'import venv' >/dev/null 2>&1 || missing+=(python3-venv)

    if [ ${#missing[@]} -eq 0 ]; then
        return
    fi

    if ! command -v apt-get >/dev/null 2>&1; then
        die "Please install these packages manually: ${missing[*]}"
    fi

    log "Installing prerequisites: ${missing[*]}"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
}

create_user() {
    if id -u "$OWLET_USER" >/dev/null 2>&1; then
        return
    fi

    log "Creating system user ${OWLET_USER}"
    useradd --system --no-create-home --home-dir "$OWLET_PREFIX" \
        --shell /usr/sbin/nologin "$OWLET_USER"
}

fetch_source() {
    if [ -d "${SRC_DIR}/.git" ]; then
        log "Updating source in ${SRC_DIR} (ref ${OWLET_REF})"
        git -C "$SRC_DIR" remote set-url origin "$OWLET_REPO"
        git -C "$SRC_DIR" fetch --quiet --tags --force origin
    else
        log "Cloning ${OWLET_REPO} into ${SRC_DIR}"
        rm -rf "$SRC_DIR"
        mkdir -p "$OWLET_PREFIX"
        git clone --quiet "$OWLET_REPO" "$SRC_DIR"
        git -C "$SRC_DIR" fetch --quiet --tags --force origin
    fi

    # Resolve the ref as a remote branch first, then as a tag or commit.
    local target
    if target=$(git -C "$SRC_DIR" rev-parse --verify --quiet \
        "refs/remotes/origin/${OWLET_REF}"); then
        :
    elif target=$(git -C "$SRC_DIR" rev-parse --verify --quiet \
        "${OWLET_REF}^{commit}"); then
        :
    else
        die "Could not resolve ref '${OWLET_REF}' in ${OWLET_REPO}"
    fi

    git -C "$SRC_DIR" checkout --quiet --detach "$target"
    log "Source is at $(git -C "$SRC_DIR" rev-parse --short HEAD)"
}

install_package() {
    if [ ! -x "${VENV_DIR}/bin/python" ]; then
        log "Creating virtualenv in ${VENV_DIR}"
        rm -rf "$VENV_DIR"
        python3 -m venv "$VENV_DIR"
    fi

    log "Installing owlet-homeapi into ${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel
    # Dependencies are installed explicitly so that pip does not pull the
    # obsolete PyPI "argparse" backport over the standard library module.
    "${VENV_DIR}/bin/pip" install --quiet --upgrade requests python-dateutil
    "${VENV_DIR}/bin/pip" install --quiet --upgrade --no-deps "$SRC_DIR"

    [ -x "${VENV_DIR}/bin/owlet-homeapi" ] ||
        die "Installation failed, ${VENV_DIR}/bin/owlet-homeapi is missing."

    # The tree stays owned by root and world readable: the service only
    # needs to read and execute it, never to write to it.
    chmod -R a+rX "$OWLET_PREFIX"
}

# Read the current value of a setting from the environment file without
# sourcing it, so that no shell expansion is applied to a password.
existing_value() {
    local name="$1" line

    [ -f "$CONFIG_FILE" ] || return 0

    line=$(grep -E "^[[:space:]]*${name}=" "$CONFIG_FILE" | tail -n 1) || true
    [ -n "$line" ] || return 0

    line="${line#*=}"
    if [ "${line:0:1}" = '"' ] && [ "${line: -1}" = '"' ]; then
        line="${line:1:${#line}-2}"
        line="${line//\\\"/\"}"
        line="${line//\\\\/\\}"
    fi

    printf '%s' "$line"
}

write_config() {
    local name value tmp
    declare -gA RESOLVED=()

    mkdir -p "$CONFIG_DIR"
    tmp=$(mktemp "${CONFIG_FILE}.XXXXXX")

    {
        echo "# Managed by ${OWLET_REPO} deploy/install.sh."
        echo "# Edit as you like, values are preserved across upgrades."
        echo "# Run 'systemctl restart ${SERVICE_NAME}' after editing."
        echo
    } >"$tmp"

    for name in "${CONFIG_VARS[@]}"; do
        # An environment variable given to the installer wins, then the
        # value already configured, then the default.
        if [ -n "${!name+x}" ]; then
            value="${!name}"
        else
            value="$(existing_value "$name")"
            if [ -z "$value" ]; then
                value="${CONFIG_DEFAULTS[$name]}"
            fi
        fi

        RESOLVED[$name]="$value"

        value="${value//\\/\\\\}"
        value="${value//\"/\\\"}"
        printf '%s="%s"\n' "$name" "$value" >>"$tmp"
    done

    chown "root:${OWLET_USER}" "$tmp"
    chmod 0640 "$tmp"
    mv "$tmp" "$CONFIG_FILE"
    log "Configuration written to ${CONFIG_FILE}"
}

install_unit() {
    log "Installing ${UNIT_FILE}"
    sed -e "s|@USER@|${OWLET_USER}|g" \
        -e "s|@PREFIX@|${OWLET_PREFIX}|g" \
        -e "s|@CONFIG@|${CONFIG_FILE}|g" \
        "${SRC_DIR}/deploy/${SERVICE_NAME}.service" >"${UNIT_FILE}.tmp"
    mv "${UNIT_FILE}.tmp" "$UNIT_FILE"
    chmod 0644 "$UNIT_FILE"
    systemctl daemon-reload
}

start_service() {
    if [ -z "${RESOLVED[OWLET_EMAIL]}" ] ||
       [ -z "${RESOLVED[OWLET_PASSWORD]}" ]; then
        systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
        warn "OWLET_EMAIL and OWLET_PASSWORD are not configured yet."
        warn "Add them to ${CONFIG_FILE}, then run:"
        warn "  sudo systemctl start ${SERVICE_NAME}"
        return
    fi

    log "Enabling and starting ${SERVICE_NAME}"
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE_NAME"

    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log "${SERVICE_NAME} is running."
    else
        warn "${SERVICE_NAME} did not stay up, recent log output:"
        journalctl -u "$SERVICE_NAME" -n 20 --no-pager >&2 || true
        exit 1
    fi
}

main() {
    require_root
    install_prereqs
    create_user
    fetch_source
    install_package
    write_config
    install_unit
    start_service

    cat <<EOF

  Installed.

    Configuration : ${CONFIG_FILE}
    Service       : systemctl status ${SERVICE_NAME}
    Live logs     : journalctl -u ${SERVICE_NAME} -f
    Restart       : sudo systemctl restart ${SERVICE_NAME}

EOF
}

main "$@"
