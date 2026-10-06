#!/bin/bash
# Pull Charleston OrderTrac and publish a new snapshot when the numbers change.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${HOME}/FAF-pricelist-2.0/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing ${PYTHON}" >&2
  exit 1
fi
export ORDERTRAC_STORAGE_STATE
ORDERTRAC_STORAGE_STATE="$(cat "${HOME}/.grok/secrets/ordertrac-charleston-session/storage_state.json")"
export DASHBOARD_PASSWORD
DASHBOARD_PASSWORD="$(
  "$PYTHON" -c '
from pathlib import Path
for line in Path.home().joinpath(".grok/secrets/charleston-sales-dashboard.env").read_text().splitlines():
    if line.startswith("DASHBOARD_PASSWORD="):
        print(line.split("=", 1)[1].strip(), end="")
'
)"
"$PYTHON" build_site.py
new_hash="$(cat site/plain-hash.txt)"
old_hash=""
if [[ -f .last-hash ]]; then
  old_hash="$(cat .last-hash)"
fi
if [[ "$new_hash" == "$old_hash" ]]; then
  echo "Sales unchanged."
  exit 0
fi
cp site/data.json data.json
git add data.json
git commit -m "Refresh CAF Quick View sales."
git push origin main
printf '%s\n' "$new_hash" > .last-hash
echo "Published a new sales snapshot."
