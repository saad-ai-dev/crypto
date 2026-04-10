#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$APP_DIR/../.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-crypto-trader}"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${FRONTEND_PORT:-8787}"
MONGO_MODE="${MONGO_MODE:-local}"          # local|external
MONGO_URI="${MONGO_URI:-mongodb://127.0.0.1:27017}"
MONGO_DB="${MONGO_DB:-crypto_trading_live}"
SKIP_OPTIMIZE="${SKIP_OPTIMIZE:-0}"
AUTO_INSTALL_DEPS_ON_BOOT="${AUTO_INSTALL_DEPS_ON_BOOT:-0}"
INSTALL_DOCKER_FOR_MONGO="${INSTALL_DOCKER_FOR_MONGO:-1}"

log() {
  printf '[deploy_ec2] %s\n' "$1"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

detect_pkg_mgr() {
  if have_cmd apt-get; then
    echo "apt"
    return
  fi
  if have_cmd dnf; then
    echo "dnf"
    return
  fi
  if have_cmd yum; then
    echo "yum"
    return
  fi
  echo "unsupported"
}

install_base_packages() {
  local pm
  pm="$(detect_pkg_mgr)"
  case "$pm" in
    apt)
      log "Installing base packages via apt"
      as_root apt-get update
      as_root apt-get install -y python3 python3-venv python3-pip curl jq lsof ca-certificates
      ;;
    dnf)
      log "Installing base packages via dnf"
      as_root dnf install -y python3 python3-pip curl jq lsof ca-certificates
      ;;
    yum)
      log "Installing base packages via yum"
      as_root yum install -y python3 python3-pip curl jq lsof ca-certificates
      ;;
    *)
      log "Unsupported package manager. Install python3, python3-venv, pip, curl, jq, lsof manually."
      exit 1
      ;;
  esac
}

install_docker_if_needed() {
  if have_cmd docker; then
    return
  fi

  if [ "$INSTALL_DOCKER_FOR_MONGO" != "1" ]; then
    log "Docker is missing and INSTALL_DOCKER_FOR_MONGO=0"
    exit 1
  fi

  local pm
  pm="$(detect_pkg_mgr)"
  case "$pm" in
    apt)
      log "Installing Docker via apt"
      as_root apt-get update
      as_root apt-get install -y docker.io
      ;;
    dnf)
      log "Installing Docker via dnf"
      as_root dnf install -y docker
      ;;
    yum)
      log "Installing Docker via yum"
      as_root yum install -y docker
      ;;
    *)
      log "Unsupported package manager for Docker install"
      exit 1
      ;;
  esac
}

ensure_local_mongo() {
  if [ "$MONGO_MODE" != "local" ]; then
    log "Using external MongoDB: $MONGO_URI"
    return
  fi

  install_docker_if_needed

  log "Ensuring Docker is running"
  as_root systemctl enable --now docker

  if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -q '^docker$'; then
    as_root usermod -aG docker "$RUN_USER" || true
  fi

  local mongo_container="crypto-mongo"
  local mongo_data_dir="/var/lib/crypto-mongo"

  as_root mkdir -p "$mongo_data_dir"
  as_root chown -R "$RUN_USER":"$RUN_GROUP" "$mongo_data_dir" || true

  if ! as_root docker ps -a --format '{{.Names}}' | grep -q "^${mongo_container}$"; then
    log "Creating MongoDB container ${mongo_container}"
    as_root docker run -d \
      --name "$mongo_container" \
      --restart unless-stopped \
      -p 27017:27017 \
      -v "$mongo_data_dir:/data/db" \
      mongo:7 >/dev/null
  else
    log "Starting existing MongoDB container ${mongo_container}"
    as_root docker start "$mongo_container" >/dev/null || true
  fi

  MONGO_URI="mongodb://127.0.0.1:27017"
}

ensure_python_env() {
  if ! have_cmd python3; then
    log "python3 is required but not found after package install"
    exit 1
  fi

  cd "$REPO_DIR"

  if [ ! -d ".venv" ]; then
    log "Creating virtual environment"
    python3 -m venv .venv
  fi

  # shellcheck disable=SC1091
  source "$REPO_DIR/.venv/bin/activate"

  log "Installing Python dependencies"
  python -m pip install --upgrade pip setuptools wheel
  if [ -f "$APP_DIR/requirements.txt" ]; then
    pip install -r "$APP_DIR/requirements.txt"
  fi

  log "Compiling Python files"
  python -m py_compile $(find "$APP_DIR" -type f -name '*.py' -not -path '*/.venv/*') "$REPO_DIR/services/frontend/server.py"

  log "Validating run scripts"
  bash -n "$REPO_DIR/start.sh"
  bash -n "$APP_DIR/run_all.sh"
  if [ -f "$APP_DIR/fetch_live_cache.sh" ]; then
    bash -n "$APP_DIR/fetch_live_cache.sh"
  fi
}

write_runtime_wrapper() {
  local wrapper="$APP_DIR/start_production.sh"

  cat > "$wrapper" <<WRAP
#!/usr/bin/env bash
set -euo pipefail
cd "$REPO_DIR"
export FRONTEND_HOST="$FRONTEND_HOST"
export FRONTEND_PORT="$FRONTEND_PORT"
export START_FRONTEND=1
export AUTO_INSTALL_DEPS="$AUTO_INSTALL_DEPS_ON_BOOT"
export SKIP_OPTIMIZE="$SKIP_OPTIMIZE"
export MONGO_URI="$MONGO_URI"
export MONGO_DB="$MONGO_DB"
export MONGO_REQUIRED=1
exec "$REPO_DIR/start.sh"
WRAP

  chmod +x "$wrapper"
  chown "$RUN_USER":"$RUN_GROUP" "$wrapper" || true
}

write_systemd_unit() {
  local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
  local log_file="/var/log/${SERVICE_NAME}.log"

  as_root touch "$log_file"
  as_root chown "$RUN_USER":"$RUN_GROUP" "$log_file"

  cat > /tmp/${SERVICE_NAME}.service <<UNIT
[Unit]
Description=Crypto Live Trading Stack (Frontend + Live Trader)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$REPO_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/start_production.sh
Restart=always
RestartSec=8
KillSignal=SIGINT
TimeoutStopSec=45
StandardOutput=append:$log_file
StandardError=append:$log_file

[Install]
WantedBy=multi-user.target
UNIT

  as_root mv /tmp/${SERVICE_NAME}.service "$unit_path"
  as_root systemctl daemon-reload
  as_root systemctl enable --now "$SERVICE_NAME"
}

show_status() {
  log "Service status"
  as_root systemctl --no-pager --full status "$SERVICE_NAME" || true

  log "Recent logs"
  as_root journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true

  log "Deploy complete"
  log "Frontend URL: http://$(curl -sS --max-time 2 ifconfig.me || echo '<EC2_PUBLIC_IP>'):${FRONTEND_PORT}"
  log "Service name: $SERVICE_NAME"
  log "Log file: /var/log/${SERVICE_NAME}.log"
}

main() {
  log "App dir: $APP_DIR"
  log "Repo dir: $REPO_DIR"
  log "Run user: $RUN_USER"
  log "Mongo mode: $MONGO_MODE"

  install_base_packages
  ensure_local_mongo
  ensure_python_env
  write_runtime_wrapper
  write_systemd_unit
  show_status
}

main "$@"
