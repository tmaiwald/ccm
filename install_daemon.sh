#!/bin/sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PY="$PROJECT_DIR/venv/bin/python"
REQUIREMENTS="$PROJECT_DIR/requirements.txt"
SERVICE_NAME="ccm"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
AUTOMATION_SERVICE_NAME="ccm-regular-meals"
AUTOMATION_SERVICE_FILE="/etc/systemd/system/$AUTOMATION_SERVICE_NAME.service"
AUTOMATION_TIMER_FILE="/etc/systemd/system/$AUTOMATION_SERVICE_NAME.timer"
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

echo "Writing automation service to $AUTOMATION_SERVICE_FILE (requires sudo)..."

sudo tee "$AUTOMATION_SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=CCM regular meal automation job
After=network.target

[Service]
Type=oneshot
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
ExecStart=$PROJECT_DIR/venv/bin/flask --app run.py process-regular-meals
StandardOutput=journal
StandardError=journal
EOF

echo "Writing automation timer to $AUTOMATION_TIMER_FILE (requires sudo)..."

sudo tee "$AUTOMATION_TIMER_FILE" > /dev/null <<EOF
[Unit]
Description=Run CCM regular meal automation every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
Unit=$AUTOMATION_SERVICE_NAME.service

[Install]
WantedBy=timers.target
EOF

echo "Reloading systemd and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
sudo systemctl enable --now "$AUTOMATION_SERVICE_NAME.timer"

echo "Done. Service '$SERVICE_NAME' enabled and started."
echo "Automation timer '$AUTOMATION_SERVICE_NAME.timer' enabled and started."
echo "Check status with: sudo systemctl status $SERVICE_NAME && sudo systemctl status $AUTOMATION_SERVICE_NAME.timer"
