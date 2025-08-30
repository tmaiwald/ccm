#!/bin/sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="ccm"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo "Stopping and disabling $SERVICE_NAME (requires sudo)..."
sudo systemctl stop "$SERVICE_NAME" || true
sudo systemctl disable "$SERVICE_NAME" || true

if [ -f "$SERVICE_FILE" ]; then
  echo "Removing unit file $SERVICE_FILE (requires sudo)..."
  sudo rm -f "$SERVICE_FILE"
  echo "Reloading systemd..."
  sudo systemctl daemon-reload
  sudo systemctl reset-failed
else
  echo "Unit file $SERVICE_FILE not found; nothing to remove."
fi

echo "Done."
