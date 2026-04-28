#!/bin/zsh
cd "$(dirname "$0")"

# Drep eventuelle prosesser på port 8000
PIDS=$(lsof -t -iTCP:8000 -sTCP:LISTEN 2>/dev/null)
if [ -n "$PIDS" ]; then
  echo "Stopper eksisterende server (PID: $PIDS)..."
  kill -9 $PIDS 2>/dev/null
  sleep 0.5
fi

source .venv/bin/activate
echo "Starter SELGO API på http://127.0.0.1:8000 ..."
python -m uvicorn main:app --reload
