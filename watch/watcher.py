#!/usr/bin/env python3
"""watcher.py - lightweight, no-AI price watcher for Stiven's Kraken + Trading 212 setup.

Read-only: Kraken PUBLIC market data + Yahoo chart data (same sources/logic as tools/tp.py scan).
Never uses API keys, never places orders. When a symbol newly passes / nearly passes the scan's
entry conditions, or an open position (trades.csv) hits +1.5R / +2R (trail start) / ~1% from the
stop, it appends an alert to alerts.jsonl and (cost-controlled) POSTs a wake to WATCH_WEBHOOK_URL.

Usage (from anywhere):
  python3 watcher.py --daemon                 # run forever (use ensure_running.sh to start)
  python3 watcher.py --once --dry-run         # one cycle, print flags, no POST, state untouched
  python3 watcher.py --once --dry-run --all-markets   # also scan stocks while markets are closed
  python3 watcher.py --test-post              # one connectivity POST (account 'test')
  python3 watcher.py --cloud --state-dir DIR  # GitHub Actions: one pass, positions from env POSITIONS_JSON,
                                              # state + pending queue in DIR, position details redacted in logs
Env: WATCH_WEBHOOK_URL, WATCH_WEBHOOK_AUTH (optional; else alerts are only logged).
     CLOUD_WATCHER=1 (env or watch.env): the GitHub Actions watcher is active -> this box copy never POSTs (log-only).
"""
import argparse, copy, datetime as dt, json, logging, logging.handlers, os, re, signal, sys, time
import types, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TRADING = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TRADING, "tools"))
import tp  # noqa: E402  (reuse scan + indicator logic)

ROME = ZoneInfo("Europe/Rome")
NY = ZoneInfo("America/New_York")
STATE_FILE = os.path.join(HERE, "state.json")
ALERTS = os.path.join(HERE, "alerts.jsonl")
PENDING = os.path.join(HERE, "pending.jsonl")  # flags suppressed by cap / session-due, for sessions to review
LOGFILE = os.path.join(HERE, "watcher.log")
PIDFILE = os.path.join(HERE, "watcher.pid")
ENVFILE = os.path.join(HERE, "watch.env")      # optional KEY=VALUE fallback (chmod 600)
WATCHLIST = os.path.join(TRADING, "tools", "watchlist.txt")

CRYPTO_EVERY_S = 10 * 60
STOCKS_EVERY_S = 15 * 60
COOLDOWN_H = 6.0
DAILY_CAP = 6
NEAR_PCT = 1.0            # "close" = within 1% of the failing threshold
STOP_NEAR_PCT = 1.0
SESSION_LOOKAHEAD_MIN = 30
KRAKEN_SESSIONS = dict(hours=(2, 5, 8, 11, 14, 17, 20, 23), minute=58, weekdays=None)
T212_SESSIONS = dict(hours=(9, 11, 13, 15, 17, 19, 21), minute=21, weekdays=(0, 1, 2, 3, 4))
EU_SUFFIX = (".DE", ".AS", ".PA", ".MI", ".MC", ".BR", ".L", ".SW", ".F", ".VI", ".LS", ".HE")
URGENT = {"near_stop"}    # always wakes: ignores the session-due skip AND the daily cap (cooldown still applies)
POSITION_REASONS = {"reached_1.5R", "trail_start_2R", "near_stop"}
PENDING_MAX_AGE_H = 24.0  # cloud mode: undelivered pending items older than this are dropped
CLOUD = False             # set by --cloud (GitHub Actions single pass)

log = logging.getLogger("watcher")

# ----------------------------------------------------------------------------- env / logging
def load_env_file(path=ENVFILE):
    """Fill missing env vars from watch.env (never overrides the real environment)."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().replace("export ", "")
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))

CLOUD_REPO = os.environ.get("WATCH_REPO", "stivenzhang8-sys/trading-watcher")
CLOUD_STALE_MIN = 45        # box resumes webhooks if no successful scheduled cloud run within this window
_cloud_hc = {"ts": 0.0, "ok": None, "ok_ts": 0.0, "why": ""}

def _cloud_flag():
    """CLOUD_WATCHER=1 from process env, else watch.env (re-read every call: no restart needed)."""
    v = os.environ.get("CLOUD_WATCHER")
    if v is None and os.path.exists(ENVFILE):
        for line in open(ENVFILE):
            line = line.strip().replace("export ", "")
            if line.startswith("CLOUD_WATCHER="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
    return (v or "").strip().lower() in ("1", "true", "yes", "on")

def cloud_healthy():
    """Heartbeat: newest successful scheduled or dispatched run of watcher.yml is < CLOUD_STALE_MIN old (gh CLI, checked
    at most every 10 min). If GitHub's schedule stalls, the box automatically resumes sending webhooks.
    gh errors keep the last good answer for 60 min, then count as unhealthy. WATCH_CLOUD_HEALTHCHECK=0 disables."""
    if os.environ.get("WATCH_CLOUD_HEALTHCHECK", "1") == "0":
        return True
    now = time.time()
    if _cloud_hc["ok"] is not None and now - _cloud_hc["ts"] < 600:
        return _cloud_hc["ok"]
    import subprocess
    ok, why = None, ""
    try:
        # scheduled runs AND workflow_dispatch runs (cron-job.org triggers the workflow every 10 min via the API)
        out = subprocess.run(["gh", "run", "list", "-R", CLOUD_REPO, "-w", "watcher.yml",
                              "-s", "success", "-L", "10", "--json", "createdAt,updatedAt,event"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            runs = [r for r in json.loads(out.stdout or "[]") if r.get("event") in ("schedule", "workflow_dispatch")]
            runs.sort(key=lambda r: r["updatedAt"], reverse=True)
            if runs:
                t = max(dt.datetime.fromisoformat(runs[0][k].replace("Z", "+00:00")).timestamp()
                        for k in ("createdAt", "updatedAt"))
                age = (now - t) / 60
                ok, why = age < CLOUD_STALE_MIN, f"last scheduled success {age:.0f} min ago"
            else:
                ok, why = False, "no successful scheduled cloud run yet"
        else:
            why = "gh error: " + (out.stderr or "").strip()[:120]
    except Exception as e:
        why = f"gh error: {e}"[:140]
    if ok is None:   # could not check
        ok = bool(_cloud_hc["ok"]) and now - _cloud_hc["ok_ts"] < 3600
    if ok != _cloud_hc["ok"]:
        log.info("cloud watcher %s (%s): box webhooks %s", "healthy" if ok else "NOT healthy", why,
                 "suppressed" if ok else "ACTIVE (fallback)")
    _cloud_hc.update(ts=now, ok=ok, why=why)
    if ok:
        _cloud_hc["ok_ts"] = now
    return ok

def cloud_watcher_active():
    """True when CLOUD_WATCHER=1 and the GitHub Actions schedule is alive. Never true inside the cloud run."""
    if CLOUD:
        return False
    return _cloud_flag() and cloud_healthy()

def setup_logging(verbose=True, file=True):
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if file:
        fh = logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=1_000_000, backupCount=3)
        fh.setFormatter(fmt); log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); log.addHandler(sh)

def now_rome():
    return dt.datetime.now(ROME).replace(microsecond=0)

# ----------------------------------------------------------------------------- state
def load_state(path=STATE_FILE):
    try:
        with open(path) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("cooldowns", {}); s.setdefault("wakes", []); s.setdefault("status", {})
    s.setdefault("levels_fired", {})
    return s

def save_state(s, path=STATE_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, path)

# ----------------------------------------------------------------------------- schedule helpers (pure)
def next_session(now, sched):
    """Next scheduled session time (Rome) at or after now."""
    for add in range(0, 8):
        day = (now + dt.timedelta(days=add)).date()
        if sched["weekdays"] is not None and day.weekday() not in sched["weekdays"]:
            continue
        for h in sched["hours"]:
            t = dt.datetime(day.year, day.month, day.day, h, sched["minute"], tzinfo=ROME)
            if t >= now:
                return t
    return None

def session_due_soon(account, now, minutes=SESSION_LOOKAHEAD_MIN):
    sched = KRAKEN_SESSIONS if account == "kraken" else T212_SESSIONS
    n = next_session(now, sched)
    return n is not None and (n - now) <= dt.timedelta(minutes=minutes), n

def is_eu(sym):
    return sym.upper().endswith(EU_SUFFIX)

def market_open(sym, now):
    """EU names 9:00-17:30 Rome; US names 9:30-16:00 New York (=15:30-22:00 Rome normally); weekdays."""
    if is_eu(sym):
        t = now.astimezone(ROME)
        return t.weekday() < 5 and dt.time(9, 0) <= t.time() < dt.time(17, 30)
    t = now.astimezone(NY)
    return t.weekday() < 5 and dt.time(9, 30) <= t.time() < dt.time(16, 0)

# ----------------------------------------------------------------------------- rule evaluation (pure)
def eval_candidate(row, kind):
    """Classify a tp.scan row: 'candidate', 'near' (exactly one soft condition fails by <= NEAR_PCT), or None.
    Conditions (tp.py): price>SMA20, SMA20>SMA50, SMA20 rising, mom20>0, + crypto liquidity / stocks mom126>0."""
    if row.get("candidate"):
        return "candidate", "scan lists it as candidate"
    px, s20 = row.get("price"), row.get("sma20")
    mom20 = row.get("mom20_pct")
    if px is None or s20 is None or mom20 is None:
        return None, None
    hard = [row.get("sma20_gt_sma50"), row.get("sma20_rising")]
    hard.append(row.get("liquid") if kind == "crypto" else (row.get("mom126_pct") or 0) > 0)
    if not all(hard):
        return None, None
    fails = []
    if not px > s20:
        gap = (s20 / px - 1) * 100
        fails.append(("above_sma20", gap, f"price {px:g} is {gap:.2f}% below SMA20 {s20:g}"))
    if not mom20 > 0:
        fails.append(("mom20", -mom20, f"20d momentum {mom20}% (needs > 0)"))
    if len(fails) == 1 and fails[0][1] <= NEAR_PCT:
        return "near", fails[0][2]
    return None, None

def initial_stop_of(trade):
    if trade.get("initial_stop") not in (None, ""):
        return float(trade["initial_stop"])
    m = re.search(r"stop\s+([0-9.]+)->", trade.get("notes") or "")
    return float(m.group(1)) if m else float(trade["stop"])

def eval_position(trade, price):
    """Return list of (reason, detail, metrics) for an open long position."""
    entry = float(trade["entry"]); stop = float(trade["stop"]); ist = initial_stop_of(trade)
    R = entry - ist
    if R <= 0:
        return []
    r15, r2 = entry + 1.5 * R, entry + 2.0 * R
    prog = (price - entry) / R
    met = dict(price=price, entry=entry, stop=stop, initial_stop=ist, R=round(R, 6),
               progress_R=round(prog, 3), level_1_5R=round(r15, 6), level_2R=round(r2, 6),
               dist_to_stop_pct=round((price / stop - 1) * 100, 3), trade_id=trade["id"])
    out = []
    if price >= r2:
        out.append(("trail_start_2R", f"price {price:g} >= +2R {r2:.4f}: start trailing 2xATR below highest close", met))
    elif price >= r15:
        out.append(("reached_1.5R", f"price {price:g} >= +1.5R {r15:.4f}: move stop to entry-0.5R ({entry-0.5*R:.4f})", met))
    if price <= stop * (1 + STOP_NEAR_PCT / 100):
        out.append(("near_stop", f"price {price:g} within {STOP_NEAR_PCT}% of stop {stop:g}" +
                    (" (AT/BELOW stop)" if price <= stop else ""), met))
    return out

# ----------------------------------------------------------------------------- decision engine (pure-ish)
def decide(state, flag, now, configured):
    """Decide what to do with a flag. Returns action in
    {'wake','cooldown','cap','session_due','not_configured'} and updates state bookkeeping
    except for the wake (recorded by record_wake after a successful POST)."""
    key = f"{flag['account']}:{flag['symbol']}:{flag['reason']}"
    last = state["cooldowns"].get(key)
    if last is not None and now.timestamp() - last < COOLDOWN_H * 3600:
        return "cooldown"
    if flag["reason"] not in URGENT:
        due, nxt = session_due_soon(flag["account"], now)
        if due:
            flag["next_session"] = nxt.isoformat()
            state["cooldowns"][key] = now.timestamp()
            return "session_due"
    if not configured:
        state["cooldowns"][key] = now.timestamp()
        return "not_configured"
    if flag["reason"] not in URGENT and wakes_today(state, now) >= DAILY_CAP:
        state["cooldowns"][key] = now.timestamp()   # don't re-log every cycle; retry after cooldown
        return "cap"
    return "wake"

def wakes_today(state, now):
    d = now.astimezone(ROME).date()
    return sum(1 for t in state["wakes"] if dt.datetime.fromtimestamp(t, ROME).date() == d)

def record_wake(state, flag, now):
    key = f"{flag['account']}:{flag['symbol']}:{flag['reason']}"
    state["cooldowns"][key] = now.timestamp()
    state["wakes"].append(now.timestamp())
    cutoff = now.timestamp() - 3 * 86400
    state["wakes"] = [t for t in state["wakes"] if t >= cutoff]

def prune(state, now):
    cutoff = now.timestamp() - 2 * COOLDOWN_H * 3600
    state["cooldowns"] = {k: v for k, v in state["cooldowns"].items() if v >= cutoff}

# ----------------------------------------------------------------------------- webhook
def webhook_cfg():
    url = os.environ.get("WATCH_WEBHOOK_URL", "").strip()
    if url and cloud_watcher_active():
        url = ""   # box copy is log-only while the GitHub Actions watcher sends the wakes
    auth = os.environ.get("WATCH_WEBHOOK_AUTH", "").strip()
    if auth.lower().startswith("authorization:"):
        auth = auth.split(":", 1)[1].strip()
    # value without a scheme (e.g. a bare token) is used exactly as given
    return url, auth

def post_webhook(payload, timeout=15):
    url, auth = webhook_cfg()
    if not url:
        raise RuntimeError("webhook not configured")
    headers = {"Content-Type": "application/json", "User-Agent": "trading-watcher/1.0"}
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code

# ----------------------------------------------------------------------------- data collection
def scan_args():
    return types.SimpleNamespace(tp_fee=None, min_vol_eur=250000, max_spread=0.25)

def stock_symbols():
    try:
        return tp.read_watchlist(WATCHLIST)
    except Exception:
        return list(tp.STOCK_WATCHLIST)

def positions_from_env(raw=None):
    """Cloud mode: open positions from the POSITIONS_JSON secret (list of trades.csv-like dicts:
    id, account, pair, entry, stop, initial_stop[, qty, ...]). Invalid/missing -> []."""
    raw = os.environ.get("POSITIONS_JSON", "") if raw is None else raw
    if not raw.strip():
        log.warning("POSITIONS_JSON not set: position checks skipped")
        return []
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            d = d.get("positions", [])
        out = []
        for t in d:
            if t.get("exit"):
                continue
            out.append({k: ("" if v is None else str(v)) for k, v in t.items()})
            for k in ("id", "account", "pair", "entry", "stop"):
                if not out[-1].get(k):
                    raise ValueError(f"position missing field {k}")
        return out
    except Exception as e:
        log.warning("POSITIONS_JSON invalid (position checks skipped): %s", type(e).__name__)
        return []

_POSITIONS = None
def open_trades():
    global _POSITIONS
    if CLOUD:
        if _POSITIONS is None:
            _POSITIONS = positions_from_env()
        return _POSITIONS
    return [r for r in tp.read_trades() if not r.get("exit")]

KEEP = ("price", "atr_pct", "sma20", "sma50", "above_sma20", "sma20_gt_sma50", "sma20_rising",
        "mom20_pct", "mom63_pct", "mom126_pct", "dist_hi20_pct", "vol24_eur", "spread_pct", "liquid",
        "candidate", "min_stop_price", "target_for_2R_net", "currency")

def candidate_flags(rows, kind, state, account):
    flags = []
    for r in rows:
        sym = r.get("pair") or r.get("symbol")
        status, detail = eval_candidate(r, kind)
        skey = f"{account}:{sym}"
        prev = state["status"].get(skey)
        if state.get("_show_all"):
            prev = None
        elif skey not in state["status"]:
            state["status"][skey] = status
            continue
        state["status"][skey] = status
        if status is None:
            continue
        # flag on transition only (new candidate, or newly near). A symbol seen for the first time
        # (fresh start or newly added to the watchlist) is only recorded, unless state["_show_all"] (dry-run).
        if prev == status or (prev == "candidate" and status == "near"):
            continue
        flags.append(dict(account=account, symbol=sym, reason="candidate" if status == "candidate" else "near_candidate",
                          detail=detail, metrics={k: r[k] for k in KEEP if k in r}))
    return flags

def position_flags(trades, prices, account, state):
    flags = []
    for t in trades:
        px = prices.get(t["pair"].upper())
        if px is None:
            continue
        for reason, detail, met in eval_position(t, px):
            lk = f"{account}:{t['id']}:{reason}"
            if reason in ("reached_1.5R", "trail_start_2R") and state["levels_fired"].get(lk):
                continue
            flags.append(dict(account=account, symbol=t["pair"], reason=reason, detail=detail,
                              metrics=met, _level_key=lk))
    return flags

def crypto_cycle(state):
    rows, errs = tp.scan_crypto(list(tp.CRYPTO_PAIRS), scan_args())
    if errs:
        log.warning("crypto scan errors: %s", errs)
    flags = candidate_flags(rows, "crypto", state, "kraken")
    trades = [t for t in open_trades() if t["account"] == "kraken"]
    prices = {r["pair"]: r["price"] for r in rows}
    missing = [tp.norm_pair(t["pair"]) for t in trades if t["pair"].upper() not in prices]
    if missing:
        prices.update({k: v["last"] for k, v in tp.kraken_tickers(missing).items()})
    flags += position_flags(trades, prices, "kraken", state)
    return flags, len(rows)

def stocks_cycle(state, now, all_markets=False):
    syms = [s for s in stock_symbols() if all_markets or market_open(s, now)]
    trades = [t for t in open_trades() if t["account"] == "t212"]
    pos_syms = [t["pair"] for t in trades if (all_markets or market_open(t["pair"], now)) and t["pair"] not in syms]
    if not syms and not pos_syms:
        return [], 0
    tp._FX.clear()
    rows, errs = tp.scan_stocks(syms + pos_syms, scan_args())
    if errs:
        log.warning("stock scan errors: %s", errs)
    flags = candidate_flags([r for r in rows if r["symbol"] in syms], "stocks", state, "t212")
    prices = {r["symbol"].upper(): r["price"] for r in rows}
    flags += position_flags(trades, prices, "t212", state)
    return flags, len(rows)

# ----------------------------------------------------------------------------- dispatch
def append_alert(rec):
    with open(ALERTS, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")

def flag_key(rec):
    return f"{rec['account']}:{rec['symbol']}:{rec['reason']}"

def read_pending(path=None):
    path = path or PENDING
    out = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out

def write_pending(items, path=None):
    path = path or PENDING
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for it in items:
            f.write(json.dumps(it, default=str) + "\n")
    os.replace(tmp, path)

def add_pending(rec, path=None):
    """Upsert one suppressed flag, deduped per account+symbol+reason (keeps first_ts, refreshes ts/metrics)."""
    items = read_pending(path); key = flag_key(rec)
    old = next((i for i in items if i.get("key") == key), None)
    new = dict(key=key, account=rec["account"], symbol=rec["symbol"], reason=rec["reason"],
               suppressed_by=rec["action"], metrics=rec["metrics"], ts=rec["ts"],
               first_ts=old.get("first_ts", old.get("ts")) if old else rec["ts"],
               count=(old.get("count", 1) + 1) if old else 1)
    items = [i for i in items if i.get("key") != key] + [new]
    write_pending(items, path)

def handle_flags(flags, state, now, dry_run=False, poster=post_webhook):
    url, _ = webhook_cfg()
    configured = bool(url)
    results = []
    for fl in flags:
        lk = fl.pop("_level_key", None)
        action = decide(state, fl, now, configured)
        if action == "cooldown":
            continue
        payload = dict(account=fl["account"], symbol=fl["symbol"], reason=fl["reason"],
                       metrics=dict(fl["metrics"], detail=fl["detail"]), ts=now.isoformat())
        rec = dict(payload, action=action, dry_run=dry_run)
        attached = []
        if action == "wake" and CLOUD:
            payload["source"] = "github-actions"
            attached = [i for i in read_pending() if i.get("account") == fl["account"]]
            if attached:   # flags suppressed earlier (cap / session due) ride along with this wake
                payload["pending"] = attached
        if action == "wake":
            if dry_run:
                rec["action"] = "would_wake"
            else:
                try:
                    code = poster(payload)
                    rec["http_status"] = code
                    if 200 <= code < 300:
                        record_wake(state, fl, now)
                        if attached:
                            sent = {i.get("key") for i in attached}
                            write_pending([i for i in read_pending() if i.get("key") not in sent])
                            rec["pending_delivered"] = len(attached)
                    else:
                        rec["action"] = "post_failed"
                        state["cooldowns"][f"{fl['account']}:{fl['symbol']}:{fl['reason']}"] = now.timestamp() - (COOLDOWN_H * 3600 - 1800)
                except Exception as e:
                    rec["action"] = "post_failed"; rec["error"] = str(e)[:200]
                    state["cooldowns"][f"{fl['account']}:{fl['symbol']}:{fl['reason']}"] = now.timestamp() - (COOLDOWN_H * 3600 - 1800)
        if lk and rec["action"] in ("wake", "session_due", "not_configured"):
            state["levels_fired"][lk] = now.isoformat()
        msg = f"FLAG {fl['account']} {fl['symbol']} {fl['reason']} -> {rec['action']}: {fl['detail']}"
        if CLOUD and fl["reason"] in POSITION_REASONS:   # public Actions logs: never reveal positions
            msg = f"FLAG position check -> {rec['action']}"
        if action == "not_configured":
            msg += " (box log-only: CLOUD_WATCHER=1)" if cloud_watcher_active() else " (webhook not configured)"
        if action == "session_due":
            msg += f" (scheduled session {fl.get('next_session')} sees it)"
        log.info(msg)
        if not dry_run:
            append_alert(rec)
            if rec["action"] in ("cap", "session_due"):
                add_pending(rec)
        results.append(rec)
    return results

def run_cycle(kind, state, now, dry_run=False, all_markets=False):
    t0 = time.time()
    try:
        if kind == "crypto":
            flags, n = crypto_cycle(state)
        else:
            flags, n = stocks_cycle(state, now, all_markets)
    except Exception as e:  # network etc. -> retry next cycle
        log.warning("%s cycle failed (retry next cycle): %s", kind, str(e)[:300])
        return []
    res = handle_flags(flags, state, now, dry_run)
    if CLOUD:
        nc = sum(1 for r in res if r["reason"] not in POSITION_REASONS)
        log.info("%s cycle: %d symbols checked, %d new candidate flags, positions checked, %.1fs", kind, n, nc, time.time() - t0)
    elif n or res:
        log.info("%s cycle: %d symbols checked, %d new flags, %.1fs", kind, n, len(res), time.time() - t0)
    return res

# ----------------------------------------------------------------------------- cloud (GitHub Actions) single pass
def expire_pending(now, max_age_h=PENDING_MAX_AGE_H):
    items = read_pending()
    keep = [i for i in items if now.timestamp() - dt.datetime.fromisoformat(i["ts"]).timestamp() < max_age_h * 3600]
    if len(keep) != len(items):
        write_pending(keep)
        log.info("dropped %d stale pending item(s) older than %.0fh", len(items) - len(keep), max_age_h)
    return keep

def send_pending_batches(state, now, dry_run=False, poster=post_webhook, woke=False):
    """Cloud mode: flags suppressed by the daily cap wait in the pending queue. They ride along with the next
    real wake (handle_flags); if none happens, one 'pending_batch' wake per account is sent as soon as the cap
    allows again (normally the first run after Rome midnight) and no scheduled session of that account is due
    within 30 min. Items suppressed only by 'session_due' never trigger a batch on their own (that session runs
    its own scan); they are delivered with the next wake or expire after PENDING_MAX_AGE_H."""
    url, _ = webhook_cfg()
    out = []
    if woke or not url:
        return out
    items = read_pending()
    for acct in ("kraken", "t212"):
        mine = [i for i in items if i.get("account") == acct]
        if not any(i.get("suppressed_by") == "cap" for i in mine):
            continue
        if wakes_today(state, now) >= DAILY_CAP or session_due_soon(acct, now)[0]:
            continue
        payload = dict(account=acct, symbol="PENDING", reason="pending_batch", source="github-actions",
                       metrics=dict(detail=f"{len(mine)} flag(s) suppressed earlier by the daily cap / session due"),
                       pending=mine, ts=now.isoformat())
        if dry_run:
            out.append(dict(account=acct, reason="pending_batch", action="would_wake", n=len(mine))); continue
        try:
            code = poster(payload)
        except Exception as e:
            code = None; log.warning("pending batch POST failed: %s", str(e)[:200])
        if code and 200 <= code < 300:
            state["wakes"].append(now.timestamp())
            sent = {i.get("key") for i in mine}
            items = [i for i in read_pending() if i.get("key") not in sent]
            write_pending(items)
            log.info("pending batch for %s sent (%d item(s)) -> HTTP %s", acct, len(mine), code)
            out.append(dict(account=acct, reason="pending_batch", action="wake", n=len(mine)))
        else:
            log.warning("pending batch for %s -> HTTP %s (kept, retry next run)", acct, code)
    return out

def cloud_once(state_dir, dry_run=False, all_markets=False):
    """One GitHub Actions pass: crypto every run, stocks only while their market is open, open positions from
    POSITIONS_JSON. State (cooldowns, wakes, scan status, levels fired) and the pending queue live in state_dir
    (persisted between runs by the workflow, encrypted, via actions/cache)."""
    global STATE_FILE, PENDING, ALERTS
    os.makedirs(state_dir, exist_ok=True)
    STATE_FILE = os.path.join(state_dir, "state.json"); PENDING = os.path.join(state_dir, "pending.jsonl")
    ALERTS = os.path.join(state_dir, "alerts.jsonl")
    state = load_state(STATE_FILE)
    work = copy.deepcopy(state) if dry_run else state
    if dry_run:
        work["_show_all"] = True; work["cooldowns"] = {}
    now = now_rome()
    url, _ = webhook_cfg()
    log.info("cloud pass %s; webhook %s; dry_run=%s; wakes today %d/%d; pending %d",
             now.isoformat(), "configured" if url else "NOT configured", dry_run, wakes_today(work, now), DAILY_CAP,
             len(read_pending()))
    if not dry_run:
        expire_pending(now)
    res = run_cycle("crypto", work, now, dry_run) + run_cycle("stocks", work, now, dry_run, all_markets)
    woke = any(r["action"] == "wake" for r in res)
    batches = send_pending_batches(work, now, dry_run, woke=woke or dry_run)
    if not dry_run:
        prune(work, now); work["last_run"] = now.isoformat(); save_state(work, STATE_FILE)
        if os.path.exists(ALERTS) and os.path.getsize(ALERTS) > 200_000:   # keep the cached state small
            lines = open(ALERTS).readlines()[-500:]
            open(ALERTS, "w").writelines(lines)
    summary = [dict(account=r["account"], reason=r["reason"], action=r["action"],
                    symbol="(position)" if r["reason"] in POSITION_REASONS else r["symbol"]) for r in res]
    print(json.dumps(dict(time=now.isoformat(), flags=summary, batches=batches, pending=len(read_pending())),
                     indent=1, default=str))
    return 0

# ----------------------------------------------------------------------------- daemon
_stop = False
def _sig(*_):
    global _stop
    _stop = True

def seed_baseline(state):
    """First start: record current candidate/near status for every symbol WITHOUT flagging, so only
    real transitions wake the AI (the scheduled sessions already see today's static candidates)."""
    now = now_rome()
    for acct, fn in (("kraken", lambda: crypto_cycle(state)), ("t212", lambda: stocks_cycle(state, now, True))):
        if state.get("baseline", {}).get(acct):
            continue
        try:
            fn()
            state.setdefault("baseline", {})[acct] = now.isoformat()
            log.info("baseline seeded for %s (current candidates recorded, not flagged)", acct)
        except Exception as e:
            log.warning("baseline for %s failed, will flag first sightings instead: %s", acct, str(e)[:200])
    save_state(state)

def daemon():
    signal.signal(signal.SIGTERM, _sig); signal.signal(signal.SIGINT, _sig)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    url, _ = webhook_cfg()
    log.info("watcher started pid %d; webhook %s", os.getpid(),
             "suppressed (CLOUD_WATCHER=1 and cloud healthy, log-only)" if cloud_watcher_active() else (("ACTIVE: CLOUD_WATCHER=1 but cloud schedule not healthy, fallback") if _cloud_flag() else ("configured" if url else "not configured")))
    try:
        seed_baseline(load_state())
    except Exception as e:
        log.warning("baseline seeding error: %s", e)
    nxt = {"crypto": time.time() + 60, "stocks": time.time() + 60}
    every = {"crypto": CRYPTO_EVERY_S, "stocks": STOCKS_EVERY_S}
    try:
        while not _stop:
            for kind in ("crypto", "stocks"):
                if time.time() >= nxt[kind]:
                    nxt[kind] = time.time() + every[kind]
                    try:
                        state = load_state(); now = now_rome()
                        run_cycle(kind, state, now)
                        prune(state, now); save_state(state)
                    except Exception as e:
                        log.exception("unexpected error in %s cycle: %s", kind, e)
            for _ in range(30):
                if _stop:
                    break
                time.sleep(1)
    finally:
        log.info("watcher stopping pid %d", os.getpid())
        try:
            if open(PIDFILE).read().strip() == str(os.getpid()):
                os.remove(PIDFILE)
        except Exception:
            pass

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--daemon", action="store_true"); ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="no POST, no state/alerts written")
    ap.add_argument("--all-markets", action="store_true", help="scan stocks even if their market is closed")
    ap.add_argument("--test-post", action="store_true", help="send one connectivity test POST")
    ap.add_argument("--cloud", action="store_true", help="GitHub Actions single pass (see cloud_once)")
    ap.add_argument("--state-dir", default=os.environ.get("WATCH_STATE_DIR", ".watch-state"))
    a = ap.parse_args(argv)
    if a.cloud:
        global CLOUD
        CLOUD = True
        setup_logging(verbose=True, file=False)
        return cloud_once(a.state_dir, a.dry_run, a.all_markets)
    load_env_file()
    setup_logging(verbose=not a.daemon)
    if a.test_post:
        payload = dict(account="test", symbol="TEST", reason="watcher connectivity test - do nothing",
                       ts=now_rome().isoformat())
        try:
            code = post_webhook(payload)
        except Exception as e:
            code = f"error: {str(e)[:120]}"
        log.info("test POST -> HTTP %s", code)
        print(f"HTTP {code}")
        return 0
    if a.daemon:
        daemon(); return 0
    state = load_state()
    work = copy.deepcopy(state) if a.dry_run else state
    if a.dry_run:
        work["_show_all"] = True   # show everything that qualifies right now
        work["cooldowns"] = {}
    now = now_rome()
    res = run_cycle("crypto", work, now, a.dry_run) + run_cycle("stocks", work, now, a.dry_run, a.all_markets)
    if not a.dry_run:
        prune(work, now); save_state(work)
    print(json.dumps(dict(time=now.isoformat(), flags=res), indent=1, default=str))
    return 0

if __name__ == "__main__":
    sys.exit(main())
