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
  # Prefer sqlite3 CLI if available, otherwise use Python fallback
  if command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 found, applying ALTER statements where needed"

    # check and add proposal.cook_user_id if missing
    if [ "$(sqlite3 "$DBFILE" "SELECT COUNT(*) FROM pragma_table_info('proposal') WHERE name='cook_user_id';")" -eq 0 ]; then
      echo "Adding proposal.cook_user_id"
      sqlite3 "$DBFILE" "PRAGMA foreign_keys=OFF; BEGIN TRANSACTION; ALTER TABLE proposal ADD COLUMN cook_user_id INTEGER; COMMIT; PRAGMA foreign_keys=ON;"
    else
      echo "proposal.cook_user_id already exists"
    fi

    # helper to add user columns if missing
    add_user_col() {
      col="$1"
      if [ "$(sqlite3 "$DBFILE" "SELECT COUNT(*) FROM pragma_table_info('user') WHERE name='"$col"';")" -eq 0 ]; then
        echo "Adding user.$col"
        sqlite3 "$DBFILE" "PRAGMA foreign_keys=OFF; BEGIN TRANSACTION; ALTER TABLE \"user\" ADD COLUMN $col INTEGER DEFAULT 0; COMMIT; PRAGMA foreign_keys=ON;"
      else
        echo "user.$col already exists"
      fi
    }

    add_user_col notify_new_proposal
    add_user_col notify_discussion
    add_user_col notify_broadcast

    echo "Conditional ALTERs (sqlite3) complete. Please restart the app."
    exit 0
  else
    echo "sqlite3 CLI not found; attempting Python fallback that only adds missing columns..."
    python3 - <<'PY'
import sqlite3, os, sys
ROOT = os.getcwd()
db = "instance/ccm.db" if os.path.exists(os.path.join(ROOT, "instance/ccm.db")) else ("ccm.db" if os.path.exists(os.path.join(ROOT, "ccm.db")) else None)
if not db:
    print("DB file not found")
    sys.exit(1)
con = sqlite3.connect(db)
cur = con.cursor()

def has(tbl, col):
    cur.execute(f"PRAGMA table_info('{tbl}')")
    return any(r[1] == col for r in cur.fetchall())

cur.execute("PRAGMA foreign_keys=OFF;")
try:
    cur.execute("BEGIN;")
    if not has('proposal', 'cook_user_id'):
        cur.execute("ALTER TABLE proposal ADD COLUMN cook_user_id INTEGER;")
        print('Added proposal.cook_user_id')
    else:
        print('proposal.cook_user_id already exists')
    for col in ('notify_new_proposal','notify_discussion','notify_broadcast'):
        if not has('user', col):
            cur.execute(f"ALTER TABLE \"user\" ADD COLUMN {col} INTEGER DEFAULT 0;")
            print(f'Added user.{col}')
        else:
            print(f'user.{col} already exists')
    cur.execute("COMMIT;")
finally:
    cur.execute("PRAGMA foreign_keys=ON;")
    con.close()
PY
    echo "Python fallback complete. Please restart the app."
    exit 0
  fi
else
  echo "No sqlite DB file found to alter. Exiting with failure."
  exit 1
fi
