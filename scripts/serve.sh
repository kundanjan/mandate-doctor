#!/usr/bin/env bash
# Detached stack: ngrok tunnel + API server + in-server incremental trainer.
# Survives terminal/opencode close (setsid + nohup). Logs: /tmp/md-*.log
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate

pkill -f "uvicorn mandate_doctor" 2>/dev/null || true
pkill -f "ngrok http 8000" 2>/dev/null || true
sleep 1

setsid nohup ~/.local/bin/ngrok http 8000 > /tmp/md-ngrok.log 2>&1 &
sleep 2
setsid nohup python -m uvicorn mandate_doctor.api.app:app \
  --host 0.0.0.0 --port 8000 > /tmp/md-uvicorn.log 2>&1 &
echo "uvicorn PID: $!"
sleep 2
curl -s --max-time 5 http://localhost:8000/health && echo ""
curl -s --max-time 4 http://localhost:4040/api/tunnels | grep -o 'https://[^"]*' | head -1
echo "Stack is detached — safe to close this terminal."
