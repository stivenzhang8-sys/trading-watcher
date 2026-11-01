#!/usr/bin/env bash
# sync_positions.sh - rewrite the GitHub Actions secret POSITIONS_JSON from the OPEN rows of trades.csv,
# so the cloud watcher (github.com/$WATCH_REPO, .github/workflows/watcher.yml) checks the current positions.
# Run it after ANY position change (open, close, stop moved) once trades.csv is updated.
#   bash watch/sync_positions.sh            # update the secret (prints only the number of positions)
#   bash watch/sync_positions.sh --print    # show the JSON locally, don't touch GitHub
# Needs `gh` logged in with repo scope. The JSON goes to gh through stdin, never into the repo or logs.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${WATCH_REPO:-stivenzhang8-sys/trading-watcher}"
json="$(python3 - "$DIR" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1]); sys.argv = sys.argv[:1]
import watcher as w
out = []
for t in w.tp.read_trades():
    if t.get("exit"):
        continue
    e, s, i = float(t["entry"]), float(t["stop"]), w.initial_stop_of(t)
    R = e - i
    out.append(dict(id=t["id"], account=t["account"], pair=t["pair"].upper(), side=t.get("side") or "long",
                    entry=e, stop=s, initial_stop=i, qty=float(t["qty"] or 0),
                    level_1_5R=round(e + 1.5 * R, 6), trail_start_2R=round(e + 2 * R, 6), date=t.get("date", "")))
print(json.dumps(out, separators=(",", ":")))
PY
)"
n="$(printf '%s' "$json" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
if [ "${1:-}" = "--print" ]; then printf '%s\n' "$json"; exit 0; fi
printf '%s' "$json" | gh secret set POSITIONS_JSON --repo "$REPO" >/dev/null
echo "$(date '+%F %T') POSITIONS_JSON updated on $REPO: $n open position(s)"
