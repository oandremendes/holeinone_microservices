#!/bin/bash
# =============================================================================
# Invoice Classification - One-Click VPS Deployment
# =============================================================================
# Usage: sudo ./deploy.sh
#
# This script is idempotent - safe to re-run. It will:
#   - Create the service user (if not exists)
#   - Install/update system dependencies
#   - Deploy/update application files (preserving config)
#   - Set up Python venv and install/update dependencies
#   - Configure rclone for Google Drive (if drivek.json available)
#   - Create/update systemd services and timer
#   - Enable and start all services
# =============================================================================

set -euo pipefail

#############################################
# CONFIGURATION - Edit these as needed
#############################################
APP_USER="invclassificator"
APP_DIR="/home/${APP_USER}/invoice_classification"
GDRIVE_MOUNT="/home/${APP_USER}/GoogleDrive"
GDRIVE_REMOTE_NAME="gdrive"
TIMER_INTERVAL="${TIMER_INTERVAL:-5min}"    # Override: TIMER_INTERVAL=1min sudo ./deploy.sh
PYTHON_CMD="python3"

# Source directory (where this script lives)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#############################################
# Output helpers
#############################################
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR ]${NC} $*"; }
step()  { echo -e "\n${BOLD}── Step $1: $2${NC}"; }

#############################################
# Pre-flight checks
#############################################
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use: sudo ./deploy.sh)"
    exit 1
fi

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════╗"
echo "║  Invoice Classification - VPS Deployment     ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"
info "User:           ${APP_USER}"
info "Install dir:    ${APP_DIR}"
info "GDrive mount:   ${GDRIVE_MOUNT}"
info "Timer interval: ${TIMER_INTERVAL}"

#############################################
# Step 1: Create service user
#############################################
step 1 "Service user"

if id "${APP_USER}" &>/dev/null; then
    ok "User '${APP_USER}' already exists (uid=$(id -u ${APP_USER}))"
else
    useradd --system --create-home --home-dir "/home/${APP_USER}" \
            --shell /bin/bash "${APP_USER}"
    ok "User '${APP_USER}' created"
fi

# Ensure home directory exists with correct permissions
mkdir -p "/home/${APP_USER}"
chown "${APP_USER}:${APP_USER}" "/home/${APP_USER}"

#############################################
# Step 2: Install system dependencies
#############################################
step 2 "System dependencies"

PACKAGES=(
    tesseract-ocr
    tesseract-ocr-por
    poppler-utils
    python3
    python3-venv
    python3-pip
    rclone
    fuse3
    rsync
)

NEED_INSTALL=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -l "$pkg" 2>/dev/null | grep -q "^ii"; then
        NEED_INSTALL+=("$pkg")
    fi
done

if [[ ${#NEED_INSTALL[@]} -gt 0 ]]; then
    info "Installing: ${NEED_INSTALL[*]}"
    apt-get update -qq
    apt-get install -y -qq "${NEED_INSTALL[@]}"
    ok "Packages installed"
else
    ok "All system packages already installed"
fi

# Ensure FUSE access for the service user
if getent group fuse &>/dev/null; then
    if ! groups "${APP_USER}" 2>/dev/null | grep -qw fuse; then
        usermod -aG fuse "${APP_USER}"
        ok "Added ${APP_USER} to fuse group"
    fi
fi

# Enable user_allow_other in FUSE config (needed for rclone)
if [[ -f /etc/fuse.conf ]]; then
    if ! grep -q "^user_allow_other" /etc/fuse.conf; then
        echo "user_allow_other" >> /etc/fuse.conf
        ok "Enabled user_allow_other in /etc/fuse.conf"
    fi
fi

#############################################
# Step 3: Deploy application files
#############################################
step 3 "Application files"

mkdir -p "${APP_DIR}"

rsync -a --delete \
    --exclude='venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='invoices_example/' \
    --exclude='invoices_test/' \
    --exclude='MATCHED/' \
    --exclude='REVIEW/' \
    --exclude='LIDL/' \
    --exclude='live_test/' \
    --exclude='new_invoices_to_process/' \
    --exclude='teofilo_nc/' \
    --exclude='old/' \
    --exclude='templates/' \
    --exclude='.claude/' \
    --exclude='config.json' \
    --exclude='drivek.json' \
    --exclude='*-service-account.json' \
    --exclude='deploy.sh' \
    "${SCRIPT_DIR}/" "${APP_DIR}/"

ok "Application files synced to ${APP_DIR}"

#############################################
# Step 4: Python virtual environment
#############################################
step 4 "Python environment"

if [[ ! -d "${APP_DIR}/venv" ]]; then
    sudo -u "${APP_USER}" ${PYTHON_CMD} -m venv "${APP_DIR}/venv"
    ok "Virtual environment created"
else
    ok "Virtual environment already exists"
fi

# On headless VPS, use opencv-python-headless instead of opencv-python
TEMP_REQ=$(mktemp)
sed 's/opencv-python>=/opencv-python-headless>=/' "${APP_DIR}/requirements.txt" > "${TEMP_REQ}"

sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --quiet -r "${TEMP_REQ}"
rm -f "${TEMP_REQ}"
ok "Python dependencies installed/updated (headless OpenCV)"

#############################################
# Step 5: Configuration files
#############################################
step 5 "Configuration"

# --- config.json ---
if [[ -f "${APP_DIR}/config.json" ]]; then
    ok "config.json preserved (existing)"
elif [[ -f "${SCRIPT_DIR}/config.json" ]]; then
    cp "${SCRIPT_DIR}/config.json" "${APP_DIR}/config.json"
    ok "config.json copied from source"
else
    cp "${APP_DIR}/config.example.json" "${APP_DIR}/config.json"
    warn "config.json created from template - edit with real API keys:"
    warn "  sudo nano ${APP_DIR}/config.json"
fi

# --- drivek.json ---
if [[ -f "${APP_DIR}/drivek.json" ]]; then
    ok "drivek.json preserved (existing)"
elif [[ -f "${SCRIPT_DIR}/drivek.json" ]]; then
    cp "${SCRIPT_DIR}/drivek.json" "${APP_DIR}/drivek.json"
    ok "drivek.json copied from source"
else
    warn "drivek.json not found. Google Drive mount requires it:"
    warn "  sudo cp /path/to/drivek.json ${APP_DIR}/drivek.json"
    warn "  sudo chown ${APP_USER}:${APP_USER} ${APP_DIR}/drivek.json"
    warn "  sudo chmod 600 ${APP_DIR}/drivek.json"
    warn "  Then re-run this script or: sudo systemctl restart rclone-gdrive-${APP_USER}"
fi

# Fix ownership and permissions
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
chmod 600 "${APP_DIR}/config.json" 2>/dev/null || true
chmod 600 "${APP_DIR}/drivek.json" 2>/dev/null || true

#############################################
# Step 6: Configure rclone
#############################################
step 6 "rclone configuration"

RCLONE_CONFIG_DIR="/home/${APP_USER}/.config/rclone"
RCLONE_CONFIG="${RCLONE_CONFIG_DIR}/rclone.conf"

mkdir -p "${RCLONE_CONFIG_DIR}"

if [[ -f "${APP_DIR}/drivek.json" ]]; then
    cat > "${RCLONE_CONFIG}" <<EOF
[${GDRIVE_REMOTE_NAME}]
type = drive
scope = drive
service_account_file = ${APP_DIR}/drivek.json
EOF
    chown -R "${APP_USER}:${APP_USER}" "${RCLONE_CONFIG_DIR}"
    chmod 600 "${RCLONE_CONFIG}"
    ok "rclone remote '${GDRIVE_REMOTE_NAME}' configured"
else
    if [[ -f "${RCLONE_CONFIG}" ]]; then
        ok "rclone config preserved (existing, but drivek.json missing from source)"
    else
        warn "rclone not configured (drivek.json missing)"
    fi
fi

# Create mount point
mkdir -p "${GDRIVE_MOUNT}"
chown "${APP_USER}:${APP_USER}" "${GDRIVE_MOUNT}"

#############################################
# Step 7: Processing script
#############################################
step 7 "Processing script"

cat > "${APP_DIR}/process_invoices.sh" <<'SCRIPTEOF'
#!/bin/bash
# Invoice Classification Auto-Processor
# Runs via systemd timer to process new invoices from Google Drive

set -e

# Configuration
PROJECT_DIR="@@APP_DIR@@"
VENV_DIR="${PROJECT_DIR}/venv"
BASE_DIR="@@GDRIVE_MOUNT@@/ScanSnap"
LOG_TAG="invoice-classifier"

# Folders to monitor
FOLDERS=(
    "$BASE_DIR"
    "$BASE_DIR/Receipts"
)

# Check if base directory exists (rclone mounted)
if [ ! -d "$BASE_DIR" ]; then
    logger -t "$LOG_TAG" "Base directory not found: $BASE_DIR (rclone not mounted?)"
    exit 0
fi

# Activate virtual environment
cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

# Process each folder
for SOURCE_DIR in "${FOLDERS[@]}"; do
    if [ ! -d "$SOURCE_DIR" ]; then
        continue
    fi

    PDF_COUNT=$(find "$SOURCE_DIR" -maxdepth 1 -type f \( -name "*.pdf" -o -name "*.PDF" \) 2>/dev/null | wc -l)

    if [ "$PDF_COUNT" -eq 0 ]; then
        continue
    fi

    logger -t "$LOG_TAG" "Found $PDF_COUNT PDF(s) in $SOURCE_DIR"

    python classifier.py process "$SOURCE_DIR" --upload 2>&1 | while read -r line; do
        logger -t "$LOG_TAG" "$line"
    done
done

logger -t "$LOG_TAG" "Processing complete"
SCRIPTEOF

# Replace placeholders with actual paths
sed -i "s|@@APP_DIR@@|${APP_DIR}|g" "${APP_DIR}/process_invoices.sh"
sed -i "s|@@GDRIVE_MOUNT@@|${GDRIVE_MOUNT}|g" "${APP_DIR}/process_invoices.sh"
chmod +x "${APP_DIR}/process_invoices.sh"
chown "${APP_USER}:${APP_USER}" "${APP_DIR}/process_invoices.sh"
ok "process_invoices.sh created with correct paths"

#############################################
# Step 8: Systemd services
#############################################
step 8 "Systemd services"

# --- rclone Google Drive mount ---
cat > "/etc/systemd/system/rclone-gdrive-${APP_USER}.service" <<EOF
[Unit]
Description=rclone Google Drive mount for ${APP_USER}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=notify
User=${APP_USER}
Group=${APP_USER}
ExecStartPre=/bin/mkdir -p ${GDRIVE_MOUNT}
ExecStart=/usr/bin/rclone mount ${GDRIVE_REMOTE_NAME}: ${GDRIVE_MOUNT} \\
    --config ${RCLONE_CONFIG} \\
    --drive-shared-with-me \\
    --vfs-cache-mode full \\
    --vfs-cache-max-age 1h \\
    --poll-interval 30s \\
    --log-level INFO \\
    --log-file /var/log/rclone-${APP_USER}.log
ExecStop=/bin/fusermount -u ${GDRIVE_MOUNT}
Restart=on-failure
RestartSec=10
Environment=HOME=/home/${APP_USER}

[Install]
WantedBy=multi-user.target
EOF
ok "rclone-gdrive-${APP_USER}.service created"

# --- Invoice classifier service ---
cat > /etc/systemd/system/invoice-classifier.service <<EOF
[Unit]
Description=Invoice Classification Processor
After=rclone-gdrive-${APP_USER}.service
Wants=rclone-gdrive-${APP_USER}.service

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/process_invoices.sh
StandardOutput=journal
StandardError=journal
SyslogIdentifier=invoice-classifier
Environment=HOME=/home/${APP_USER}

[Install]
WantedBy=multi-user.target
EOF
ok "invoice-classifier.service created"

# --- Timer ---
cat > /etc/systemd/system/invoice-classifier.timer <<EOF
[Unit]
Description=Run Invoice Classification every ${TIMER_INTERVAL}

[Timer]
OnBootSec=2min
OnUnitActiveSec=${TIMER_INTERVAL}
Persistent=true

[Install]
WantedBy=timers.target
EOF
ok "invoice-classifier.timer created (${TIMER_INTERVAL})"

#############################################
# Step 9: Enable and start services
#############################################
step 9 "Starting services"

systemctl daemon-reload
ok "systemd daemon reloaded"

# Enable services (idempotent)
systemctl enable "rclone-gdrive-${APP_USER}.service" --quiet 2>/dev/null
systemctl enable invoice-classifier.timer --quiet 2>/dev/null

# Start rclone if config is available
if [[ -f "${APP_DIR}/drivek.json" ]] && [[ -f "${RCLONE_CONFIG}" ]]; then
    systemctl restart "rclone-gdrive-${APP_USER}.service" && \
        ok "rclone mount service started" || \
        warn "rclone mount service failed to start - check: journalctl -u rclone-gdrive-${APP_USER}"
else
    warn "rclone service enabled but NOT started (drivek.json or rclone config missing)"
fi

# Start timer
systemctl restart invoice-classifier.timer
ok "Invoice classifier timer started"

# Create log file with correct permissions
touch "/var/log/rclone-${APP_USER}.log"
chown "${APP_USER}:${APP_USER}" "/var/log/rclone-${APP_USER}.log"

#############################################
# Summary
#############################################
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Deployment Complete                         ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  User:       ${APP_USER}"
echo "  App dir:    ${APP_DIR}"
echo "  GDrive:     ${GDRIVE_MOUNT}"
echo "  Timer:      every ${TIMER_INTERVAL}"
echo ""
echo -e "${BOLD}  Service commands:${NC}"
echo "    systemctl status rclone-gdrive-${APP_USER}       # GDrive mount status"
echo "    systemctl status invoice-classifier.timer        # Timer status"
echo "    systemctl list-timers invoice-classifier.timer   # Next run time"
echo "    journalctl -t invoice-classifier -f              # Classifier logs"
echo "    journalctl -u rclone-gdrive-${APP_USER} -f       # rclone logs"
echo "    tail -f /var/log/rclone-${APP_USER}.log          # rclone file log"
echo ""
echo -e "${BOLD}  Change timer interval:${NC}"
echo "    TIMER_INTERVAL=1min sudo ./deploy.sh             # Re-deploy with 1min"
echo ""

# Check for remaining manual steps
MANUAL_STEPS=()
if [[ ! -f "${APP_DIR}/drivek.json" ]]; then
    MANUAL_STEPS+=("Copy Google Service Account key:")
    MANUAL_STEPS+=("  sudo cp /path/to/drivek.json ${APP_DIR}/drivek.json")
    MANUAL_STEPS+=("  sudo chown ${APP_USER}:${APP_USER} ${APP_DIR}/drivek.json")
    MANUAL_STEPS+=("  sudo chmod 600 ${APP_DIR}/drivek.json")
    MANUAL_STEPS+=("  sudo systemctl restart rclone-gdrive-${APP_USER}")
    MANUAL_STEPS+=("")
fi
if grep -q "YOUR_.*_KEY_HERE" "${APP_DIR}/config.json" 2>/dev/null; then
    MANUAL_STEPS+=("Edit API keys in config.json:")
    MANUAL_STEPS+=("  sudo nano ${APP_DIR}/config.json")
    MANUAL_STEPS+=("")
fi

if [[ ${#MANUAL_STEPS[@]} -gt 0 ]]; then
    echo -e "${YELLOW}${BOLD}  Manual steps remaining:${NC}"
    for line in "${MANUAL_STEPS[@]}"; do
        echo -e "${YELLOW}    ${line}${NC}"
    done
fi
