#!/usr/bin/env bash
# Runs on every codespace start: starts the web app (port 8787) and refreshes the free MoRTH/NHIDCL index, both in the
# background. Needs the Codespaces secrets CRUSTDATA_API_KEY and CC_ACCESS_CODE (8+ characters). Logs: /tmp/*.log
cd "$(dirname "$0")/.." || exit 0
if [ -z "${CRUSTDATA_API_KEY:-}" ] || [ "${#CC_ACCESS_CODE}" -lt 8 ]; then
  echo "Not starting the web app: add the Codespaces secrets CRUSTDATA_API_KEY and CC_ACCESS_CODE, then restart." >&2
  exit 0
fi
pkill -f "scripts/serve_web.py" 2>/dev/null || true
setsid nohup python -m contractor_check index --no-crustdata >/tmp/index.log 2>&1 < /dev/null &
setsid nohup python scripts/serve_web.py >/tmp/serve_web.log 2>&1 < /dev/null &
echo "Web app starting on port 8787 (log: /tmp/serve_web.log); debarment index refresh running (log: /tmp/index.log)."
