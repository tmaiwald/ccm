#!/bin/sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/venv/bin/python"
REQUIREMENTS="$PROJECT_DIR/requirements.txt"
SERVICE_NAME="ccm"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
CURRENT_USER="$(whoami)"

echo "Project directory: $PROJECT_DIR"

if [ ! -x "$VENV_PY" ]; then
  echo "Virtualenv not found, creating at $PROJECT_DIR/venv..."
  python3 -m venv "$PROJECT_DIR/venv"
  . "$PROJECT_DIR/venv/bin/activate"
  pip install --upgrade pip
  if [ -f "$REQUIREMENTS" ]; then
    echo "Installing requirements from $REQUIREMENTS..."
    pip install -r "$REQUIREMENTS"
  fi
else
  echo "Using existing virtualenv: $VENV_PY"
fi

echo "Writing systemd unit to $SERVICE_FILE (requires sudo)..."

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Cleverly Connected Meals (CCM) Flask app
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
ExecStart=$VENV_PY $PROJECT_DIR/run.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo "Done. Service '$SERVICE_NAME' enabled and started. Check status with: sudo systemctl status $SERVICE_NAME"
