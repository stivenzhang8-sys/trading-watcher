# trading-watcher

No-AI, read-only price watcher for a small Kraken spot + Trading 212 setup. It never places orders and
uses only public market data (Kraken public API, Yahoo chart/spark). See [watch/README.md](watch/README.md).

- `.github/workflows/watcher.yml`: one check pass every 10 minutes on GitHub Actions (cron `*/10 * * * *`,
  plus manual `workflow_dispatch`, dry run by default).
- `.github/workflows/keepalive.yml`: monthly re-enable + timestamp commit so the schedule is not disabled
  after 60 days without activity.
- `watch/watcher.py`: watcher (box daemon or `--cloud` single pass); `tools/tp.py`: scan/indicator logic;
  `tools/watchlist.txt`: stock universe.

Nothing private is stored here: open positions arrive through the encrypted secret `POSITIONS_JSON`, the webhook
through `WATCH_WEBHOOK_URL` / `WATCH_WEBHOOK_AUTH`, and the run state is encrypted (`WATCH_STATE_KEY`) before it
is put in the Actions cache. Logs redact position flags.
