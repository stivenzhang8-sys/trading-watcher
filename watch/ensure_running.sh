#!/usr/bin/env bash
# ensure_running.sh - start watcher.py as a background daemon if it is not running.
#   bash ensure_running.sh            # check once, (re)start if dead
#   bash ensure_running.sh --supervise  # loop forever, check every 5 min (used because cron is unavailable)
#   bash ensure_running.sh --stop     # stop watcher (and supervisor)
# The daemon is started through a LOGIN shell (bash -lc) so it inherits WATCH_WEBHOOK_URL /
# WATCH_WEBHOOK_AUTH like other box processes. Secret values are never printed.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/watcher.pid"; SUPPID="$DIR/supervisor.pid"; OUT="$DIR/daemon.out"

alive() {  # $1 pidfile, $2 pattern
  [ -f "$1" ] || return 1
  local p; p="$(cat "$1" 2>/dev/null)"; [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null && tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "$2"
}

start() {
  if alive "$PIDFILE" "watcher.py"; then echo "$(date '+%F %T') watcher running pid $(cat "$PIDFILE")"; return 0; fi
  rm -f "$PIDFILE"
  setsid nohup bash -lc "cd '$DIR' && exec python3 '$DIR/watcher.py' --daemon" >>"$OUT" 2>&1 < /dev/null &
  for _ in 1 2 3 4 5 6 7 8 9 10; do sleep 1; alive "$PIDFILE" "watcher.py" && break; done
  if alive "$PIDFILE" "watcher.py"; then echo "$(date '+%F %T') watcher started pid $(cat "$PIDFILE")"
  else echo "$(date '+%F %T') watcher FAILED to start (see $OUT)"; return 1; fi
}

case "${1:-}" in
  --supervise)
    if alive "$SUPPID" "ensure_running.sh"; then echo "supervisor already running pid $(cat "$SUPPID")"; exit 0; fi
    echo $$ > "$SUPPID"
    while true; do start >>"$DIR/supervisor.log" 2>&1; sleep 300; done ;;
  --stop)
    alive "$SUPPID" "ensure_running.sh" && kill "$(cat "$SUPPID")" && rm -f "$SUPPID" && echo "supervisor stopped"
    if alive "$PIDFILE" "watcher.py"; then
      wp="$(cat "$PIDFILE")"; kill "$wp"
      for _ in $(seq 1 20); do kill -0 "$wp" 2>/dev/null || break; sleep 0.5; done   # wait for clean exit
      echo "watcher stopped"
    fi ;;
  *)
    start
    # make sure the supervisor loop is up too (cron is not available on this box)
    if ! alive "$SUPPID" "ensure_running.sh"; then
      setsid nohup bash "$DIR/ensure_running.sh" --supervise >/dev/null 2>&1 < /dev/null &
      sleep 1; echo "supervisor started pid $(cat "$SUPPID" 2>/dev/null)"
    fi ;;
esac
