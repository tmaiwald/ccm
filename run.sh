#!/bin/sh
set -e
cd "$(dirname "$0")"
# create venv if missing
if [ ! -d venv ]; then
  python3 -m venv venv
fi
. venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# run the app
python run.py
