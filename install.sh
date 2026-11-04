#!/usr/bin/env bash
set -e

# Resolve directory of this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Identify the real non-root user even if run with sudo
if [ -n "${SUDO_USER}" ]; then
    CURRENT_USER="${SUDO_USER}"
    USER_HOME="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
else
    CURRENT_USER="$(id -un)"
    USER_HOME="${HOME}"
fi

SERVICE_NAME="llm-router"
SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=== Installing ${SERVICE_NAME} ==="
echo "Working directory: ${SCRIPT_DIR}"
echo "Running service as user: ${CURRENT_USER} (home: ${USER_HOME})"
echo

# 1. Create Python virtual environment if it doesn't exist
if [ ! -d "${SCRIPT_DIR}/venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "${SCRIPT_DIR}/venv"
fi

# 2. Upgrade pip and install dependencies
echo "Installing dependencies from requirements.txt..."
"${SCRIPT_DIR}/venv/bin/pip" install --upgrade pip --quiet
"${SCRIPT_DIR}/venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" --quiet

# 3. Create config.json from example if missing
if [ ! -f "${SCRIPT_DIR}/config.json" ]; then
    echo "config.json not found, copying from config.example.json..."
    cp "${SCRIPT_DIR}/config.example.json" "${SCRIPT_DIR}/config.json"
    echo "IMPORTANT: Please edit ${SCRIPT_DIR}/config.json with your actual settings!"
fi

# 4. Detect path to agy binary or use standard locations
AGY_BIN_DIR="$(dirname "$(command -v agy 2>/dev/null || echo "${USER_HOME}/.local/bin/agy")")"
SERVICE_PATH="${AGY_BIN_DIR}:${USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"

# 5. Generate and install systemd service unit
echo "Installing systemd service unit to ${SYSTEMD_PATH}..."
sudo bash -c "cat > '${SYSTEMD_PATH}'" <<EOF
[Unit]
Description=LLM Router API Service
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
Environment="PATH=${SERVICE_PATH}"
Environment="PYTHONUNBUFFERED=1"
ExecStart=${SCRIPT_DIR}/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 6. Reload systemd daemon and enable service
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo
echo "=== Installation complete ==="
echo "1. Verify configuration:"
echo "   ${SCRIPT_DIR}/venv/bin/python check_config.py"
echo
echo "2. Start the service:"
echo "   sudo systemctl start ${SERVICE_NAME}"
echo
echo "3. Check status & logs:"
echo "   sudo systemctl status ${SERVICE_NAME}"
echo "   journalctl -u ${SERVICE_NAME} -f"
