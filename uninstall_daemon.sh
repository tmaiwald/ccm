#!/bin/sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="ccm"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
AUTOMATION_SERVICE_NAME="ccm-regular-meals"
AUTOMATION_SERVICE_FILE="/etc/systemd/system/$AUTOMATION_SERVICE_NAME.service"
AUTOMATION_TIMER_FILE="/etc/systemd/system/$AUTOMATION_SERVICE_NAME.timer"

echo "Stopping and disabling $SERVICE_NAME (requires sudo)..."
sudo systemctl stop "$SERVICE_NAME" || true
sudo systemctl disable "$SERVICE_NAME" || true

echo "Stopping and disabling $AUTOMATION_SERVICE_NAME timer (requires sudo)..."
sudo systemctl stop "$AUTOMATION_SERVICE_NAME.timer" || true
sudo systemctl disable "$AUTOMATION_SERVICE_NAME.timer" || true
sudo systemctl stop "$AUTOMATION_SERVICE_NAME.service" || true

if [ -f "$SERVICE_FILE" ]; then
  echo "Removing unit file $SERVICE_FILE (requires sudo)..."
  sudo rm -f "$SERVICE_FILE"
else
  echo "Unit file $SERVICE_FILE not found; nothing to remove."
fi

if [ -f "$AUTOMATION_SERVICE_FILE" ] || [ -f "$AUTOMATION_TIMER_FILE" ]; then
  echo "Removing automation unit files (requires sudo)..."
  sudo rm -f "$AUTOMATION_SERVICE_FILE" "$AUTOMATION_TIMER_FILE"
else
  echo "Automation unit files not found; nothing to remove."
fi

echo "Reloading systemd..."
sudo systemctl daemon-reload
sudo systemctl reset-failed

echo "Done."
