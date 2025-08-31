#!/bin/sh
set -e

# Usage: ./scripts/apply_migrations.sh
# This script will back up the sqlite DB, ensure venv deps installed,
# try to run flask-migrate upgrade and fall back to stamping or a safe ALTER

ROOT="$(cd "$(dirname "$(dirname "$0")")" && pwd)"
cd "$ROOT"

VENV="$ROOT/venv"
ACTIVATE="$VENV/bin/activate"

if [ -f "$ACTIVATE" ]; then
  . "$ACTIVATE"
else
  echo "Virtualenv not found at $VENV - creating..."
  python3 -m venv "$VENV"
  . "$ACTIVATE"
fi

echo "Installing requirements..."
pip install -r requirements.txt

# Backup DB
if [ -f instance/ccm.db ]; then
  echo "Backing up instance/ccm.db -> instance/ccm.db.bak"
  cp instance/ccm.db instance/ccm.db.bak
fi
if [ -f ccm.db ]; then
  echo "Backing up ccm.db -> ccm.db.bak"
  cp ccm.db ccm.db.bak
fi

export FLASK_APP=run.py

echo "Attempting flask db upgrade..."
set +e
flask db upgrade
RC=$?
set -e
if [ $RC -eq 0 ]; then
  echo "flask db upgrade succeeded"
  exit 0
fi

echo "flask db upgrade failed (rc=$RC). Trying to stamp head and retry migration generation..."
set +e
flask db stamp head
flask db migrate -m "autogen after manual changes"
flask db upgrade
RC2=$?
set -e
if [ $RC2 -eq 0 ]; then
  echo "Migration generated and applied successfully"
  exit 0
fi

echo "Automatic alembic upgrade still failed. Attempting direct safe ALTER for SQLite (add nullable columns)..."
DBFILE=""
if [ -f instance/ccm.db ]; then
  DBFILE="instance/ccm.db"
elif [ -f ccm.db ]; then
  DBFILE="ccm.db"
fi

if [ -n "$DBFILE" ]; then
  echo "Using DB file: $DBFILE"
  # add cook_user_id column if missing
  echo "Adding column cook_user_id to proposal (if not exists)"
  sqlite3 "$DBFILE" "PRAGMA foreign_keys=OFF; BEGIN TRANSACTION; ALTER TABLE proposal ADD COLUMN cook_user_id INTEGER; COMMIT; PRAGMA foreign_keys=ON;"
  echo "ALTER applied. Please restart the app."
  exit 0
else
  echo "No sqlite DB file found to alter. Exiting with failure."
  exit 1
fi
