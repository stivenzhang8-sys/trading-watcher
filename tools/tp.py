#!/usr/bin/env python3
"""tp.py - deterministic trading toolkit for Kraken (spot) and Trading 212 trading sessions.

READ-ONLY: this tool never places, cancels or modifies orders. It only calls
public market-data endpoints, `kraken balance` / `kraken open-orders` (read-only
CLI commands) and Trading 212 GET endpoints. API keys are read from env and never printed.

Subcommands: scan | size | check | trail | log | stats   (see README.md)
Every command prints a JSON object to stdout (last line block) preceded by short human text
on stderr-free stdout lines prefixed with '# '. Use --json-only to get just the JSON.
"""
import argparse, base64, csv, datetime as dt, json, math, os, subprocess, sys, time
import urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TRADING_DIR = os.path.dirname(HERE)
TRADES_CSV = os.environ.get("TP_TRADES_CSV", os.path.join(TRADING_DIR, "trades.csv"))
CACHE_DIR = os.path.join(HERE, "cache")

# ----------------------------------------------------------------------------- config
CFG = {
    "kraken": dict(
        kind="crypto", start_capital=1000.0, max_pos_frac=0.25, risk_pct=1.0,
        fee_entry=0.004,       # maker (post-only limit) entry, Tier 1
        fee_stop=0.008,        # stop-loss fills as market -> taker
        fee_target=0.008,      # conservative: taker exit (use --tp-fee 0.004 for a resting limit TP)
        min_stop_atr=1.5, min_stop_pct=3.0, min_rr_net=2.0, max_target_atr=4.0,
        max_open=3, max_open_risk_pct=3.0, cooldown_h=24, max_trips_month=12,
        equity_floor=0.0,      # real start_capital / equity_floor: tools/account.local.json
    ),
    "t212": dict(
        kind="stocks", start_capital=1000.0, max_pos_frac=0.35, risk_pct=1.0,
        fx_fee=0.0015,         # per leg, only for non-EUR instruments
        min_stop_atr=2.0, min_stop_pct=0.0, min_rr_net=2.0, max_target_atr=4.5,
        max_open=3, max_open_risk_pct=4.0, cooldown_h=24, max_trips_month=12,
        equity_floor=0.0, qty_decimals=4, min_notional_eur=1.0,
    ),
}
# Account-size values (start_capital, equity_floor) are private: they live in tools/account.local.json
# (gitignored, box only, e.g. {"kraken": {"start_capital": ..., "equity_floor": ...}}) and override the
# placeholder defaults above. The public GitHub copy only uses the scan functions, which don't need them.
_LOCAL_CFG = os.path.join(HERE, "account.local.json")
if os.path.exists(_LOCAL_CFG):
    with open(_LOCAL_CFG) as _f:
        for _acct, _vals in json.load(_f).items():
            CFG.setdefault(_acct, {}).update(_vals)
# NOTE on max_target_atr: with stop >= 1.5xATR and target >= 2R *net of fees*, the smallest
# legal target is ~3.6-4.4xATR on Kraken (fees ~1.2-1.6% round trip), and >= 4xATR for
# stocks with a 2xATR stop. A literal 3xATR cap would reject every trade, so the defaults are
# 4.0 (crypto) / 4.5 (stocks). Override with --max-target-atr.

CRYPTO_PAIRS = ["XBTEUR", "ETHEUR", "SOLEUR", "XRPEUR", "LINKEUR", "ADAEUR", "DOTEUR",
                "AVAXEUR", "NEAREUR", "LTCEUR", "XDGEUR", "SUIEUR", "BCHEUR", "TRXEUR",
                "XLMEUR", "UNIEUR", "ATOMEUR"]
STOCK_WATCHLIST = ["SPY", "QQQ", "VWCE.DE", "SXR8.DE", "EUNL.DE", "AAPL", "MSFT", "NVDA",
                   "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V", "LLY", "ASML.AS", "SAP.DE"]
DEFAULT_WATCHLIST = os.path.join(HERE, "watchlist.txt")  # default for scan stocks; STOCK_WATCHLIST is the fallback
LEGACY = {"XXBT": "XBT", "XBT": "XBT", "BTC": "XBT", "XETH": "ETH", "XXRP": "XRP", "XLTC": "LTC",
          "XXLM": "XLM", "XXDG": "XDG", "DOGE": "XDG", "ZEUR": "EUR", "ZUSD": "USD", "XETC": "ETC"}
UA = {"User-Agent": "Mozilla/5.0"}

# ----------------------------------------------------------------------------- http helpers
def http_json(url, headers=None, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa
            last = e
            time.sleep(1.0 + i)
    raise RuntimeError(f"GET failed {url.split('?')[0]}: {last}")

def kraken_public(method, **params):
    q = urllib.parse.urlencode(params)
    d = http_json(f"https://api.kraken.com/0/public/{method}?{q}")
    if d.get("error"):
        raise RuntimeError(f"Kraken {method} error: {d['error']}")
    return d["result"]

def kraken_cli(*args):
    """Read-only kraken CLI calls only (balance, open-orders)."""
    allowed = {"balance", "open-orders"}
    if args[0] not in allowed:
        raise RuntimeError("refusing non read-only kraken command")
    out = subprocess.run(["kraken", *args, "-o", "json"], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"kraken {args[0]} failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)

def t212_get(path):
    k, s = os.environ.get("T212_API_KEY"), os.environ.get("T212_API_SECRET")
    if not k or not s:
        raise RuntimeError("T212_API_KEY/T212_API_SECRET not set")
    auth = base64.b64encode(f"{k}:{s}".encode()).decode()
    return http_json("https://live.trading212.com/api/v0" + path,
                     headers={"Authorization": "Basic " + auth, **UA})

# ----------------------------------------------------------------------------- indicators (pure)
def true_ranges(h, l, c):
    return [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]

def atr_wilder(h, l, c, n=14):
    tr = true_ranges(h, l, c)
    if len(tr) < n:
        return None
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a

def sma(x, n):
    return sum(x[-n:]) / n if len(x) >= n else None

def ema(x, n):
    if len(x) < n:
        return None
    k = 2 / (n + 1)
    e = sum(x[:n]) / n
    for v in x[n:]:
        e = v * k + e * (1 - k)
    return e

def pct(a, b):
    return None if (a is None or b in (None, 0)) else (a / b - 1) * 100

def rnd(x, d=4):
    return None if x is None else round(x, d)

def trend_metrics(h, l, c, last=None):
    """c = completed daily closes (oldest first). last = live price (defaults to c[-1])."""
    px = last if last is not None else c[-1]
    a = atr_wilder(h, l, c, 14)
    s20, s50, e20, e50 = sma(c, 20), sma(c, 50), ema(c, 20), ema(c, 50)
    s20_prev = sma(c[:-5], 20) if len(c) >= 25 else None
    mom20 = pct(px, c[-21]) if len(c) >= 21 else None
    mom63 = pct(px, c[-64]) if len(c) >= 64 else None
    mom126 = pct(px, c[-127]) if len(c) >= 127 else None
    hi20 = max(h[-20:]) if len(h) >= 20 else None
    lo20 = min(l[-20:]) if len(l) >= 20 else None
    up = bool(s20 and s50 and mom20 is not None and px > s20 > s50 and mom20 > 0
              and s20_prev is not None and s20 > s20_prev)
    return dict(price=px, atr=a, atr_pct=rnd(a / px * 100 if a else None, 2),
                sma20=s20, sma50=s50, ema20=e20, ema50=e50,
                above_sma20=bool(s20 and px > s20), above_sma50=bool(s50 and px > s50),
                sma20_gt_sma50=bool(s20 and s50 and s20 > s50),
                sma20_rising=bool(s20 and s20_prev and s20 > s20_prev),
                mom20_pct=rnd(mom20, 2), mom63_pct=rnd(mom63, 2), mom126_pct=rnd(mom126, 2),
                dist_hi20_pct=rnd(pct(px, hi20), 2), dist_lo20_pct=rnd(pct(px, lo20), 2),
                uptrend=up)

# ----------------------------------------------------------------------------- fee / R math (pure)
def fee_model(account, ccy="EUR", tp_fee=None):
    c = CFG[account]
    if account == "kraken":
        f = dict(entry=c["fee_entry"], stop=c["fee_stop"], target=c["fee_target"])
    else:
        fx = 0.0 if ccy.upper() == "EUR" else c["fx_fee"]
        f = dict(entry=fx, stop=fx, target=fx)
    if tp_fee is not None:
        f["target"] = tp_fee
    return f

def r_math(entry, stop, target, fees):
    """Per-unit, long only. Returns gross/net risk & reward, R multiple net, break-even WR."""
    if stop >= entry:
        raise ValueError("stop must be below entry (long only)")
    risk_g = entry - stop
    risk_n = risk_g + entry * fees["entry"] + stop * fees["stop"]
    out = dict(risk_per_unit_gross=risk_g, risk_per_unit_net=risk_n,
               stop_dist_pct=risk_g / entry * 100)
    if target is not None:
        rew_g = target - entry
        rew_n = rew_g - entry * fees["entry"] - target * fees["target"]
        out.update(reward_per_unit_gross=rew_g, reward_per_unit_net=rew_n,
                   target_dist_pct=rew_g / entry * 100,
                   rr_gross=rew_g / risk_g, rr_net=rew_n / risk_n,
                   breakeven_winrate_pct=(risk_n / (risk_n + rew_n) * 100) if rew_n > 0 else 100.0)
    return out

def min_target_for_rr(entry, stop, fees, rr=2.0):
    """Smallest target price giving rr_net >= rr."""
    risk_n = (entry - stop) + entry * fees["entry"] + stop * fees["stop"]
    return (entry + entry * fees["entry"] + rr * risk_n) / (1 - fees["target"])

def floor_dec(x, d):
    m = 10 ** d
    return math.floor(x * m + 1e-9) / m

# ----------------------------------------------------------------------------- sizing (pure)
def compute_size(account, equity, risk_pct, entry, stop, fees, fx_to_eur=1.0,
                 ordermin=None, costmin=None, lot_decimals=8, max_notional_eur=None):
    """Prices in instrument currency; fx_to_eur converts 1 unit of that ccy to EUR."""
    c = CFG[account]
    if max_notional_eur is None:
        max_notional_eur = c["start_capital"] * c["max_pos_frac"]
    risk_budget = equity * risk_pct / 100
    rm = r_math(entry, stop, None, fees)
    risk_unit_eur = rm["risk_per_unit_net"] * fx_to_eur
    qty_risk = risk_budget / risk_unit_eur
    qty_cap = max_notional_eur / (entry * fx_to_eur * (1 + fees["entry"]))
    qty = floor_dec(min(qty_risk, qty_cap), lot_decimals)
    capped = qty_cap < qty_risk
    notional = qty * entry * fx_to_eur
    problems = []
    if ordermin is not None and qty < ordermin:
        problems.append(f"qty {qty} < Kraken ordermin {ordermin}")
    if costmin is not None and notional < costmin:
        problems.append(f"notional {notional:.2f} < costmin {costmin}")
    if qty <= 0:
        problems.append("qty rounds to 0")
    return dict(qty=qty, notional_eur=round(notional, 2),
                entry_fee_eur=round(notional * fees["entry"], 4),
                risk_eur=round(qty * risk_unit_eur, 4), risk_budget_eur=round(risk_budget, 4),
                risk_pct_of_equity=round(qty * risk_unit_eur / equity * 100, 3) if equity else None,
                capped_by_max_notional=capped, max_notional_eur=round(max_notional_eur, 2),
                qty_uncapped=round(qty_risk, 8), stop_dist_pct=round(rm["stop_dist_pct"], 3),
                ok=not problems, problems=problems)

# ----------------------------------------------------------------------------- trail (pure)
def trail_stop(entry, initial_stop, price, highest_close, atr, current_stop=None,
               lock_at_r=1.5, lock_to_r=-0.5, trail_from_r=2.0, trail_atr=2.0):
    R = entry - initial_stop
    if R <= 0:
        raise ValueError("initial stop must be below entry")
    cur = current_stop if current_stop is not None else initial_stop
    best = max(price, highest_close if highest_close is not None else price)
    prog = (best - entry) / R
    stage, rec = "initial (no breakeven at +1R)", initial_stop
    if prog >= lock_at_r:
        stage, rec = f"locked {lock_to_r:+}R (reached +{lock_at_r}R)", entry + lock_to_r * R
    if prog >= trail_from_r and highest_close is not None:
        t = highest_close - trail_atr * atr
        if t > rec:
            stage, rec = f"trailing {trail_atr}xATR below highest daily close", t
    new = max(rec, cur)
    action = "RAISE" if new > cur + 1e-12 else "HOLD"
    notes = []
    if new >= price:
        notes.append("price is at/below the recommended stop: stop should already have triggered / exit")
    return dict(R_per_unit=R, progress_R=round(prog, 3), price_R=round((price - entry) / R, 3),
                stage=stage, rule_stop=rec, current_stop=cur, recommended_stop=new,
                recommended_stop_R=round((new - entry) / R, 3), action=action, notes=notes)

# ----------------------------------------------------------------------------- trade log
FIELDS = ["id", "date", "account", "pair", "side", "setup", "entry", "stop", "target", "qty",
          "risk_eur", "exit_date", "exit", "exit_reason", "result_r", "fees", "pnl_eur", "notes"]

def read_trades(path=None):
    path = path or TRADES_CSV
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def write_trades(rows, path=None):
    path = path or TRADES_CSV
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, path)

def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.strip().replace("T", " ")[:19]).replace(tzinfo=None)
    except ValueError:
        return None

def now_local():
    return dt.datetime.now().replace(microsecond=0)

def log_open(rows, account, pair, setup, entry, stop, target, qty, fees_eur=None,
             date=None, notes="", fx_to_eur=1.0, ccy="EUR"):
    fees = fee_model(account, ccy)
    if fees_eur is None:
        fees_eur = qty * entry * fees["entry"] * fx_to_eur
    risk = (qty * (entry - stop) * fx_to_eur + fees_eur + qty * stop * fees["stop"] * fx_to_eur)
    nid = max([int(r["id"]) for r in rows if str(r.get("id", "")).isdigit()] + [0]) + 1
    row = dict(id=nid, date=date or now_local().strftime("%Y-%m-%d %H:%M"), account=account,
               pair=pair.upper(), side="long", setup=setup, entry=entry, stop=stop,
               target=target if target is not None else "", qty=qty, risk_eur=round(risk, 4),
               exit_date="", exit="", exit_reason="", result_r="", fees=round(fees_eur, 4),
               pnl_eur="", notes=notes)
    rows.append(row)
    return row

def log_close(rows, tid, exit_price, reason, exit_fee_eur=None, date=None, notes=None,
              fx_to_eur=1.0, ccy="EUR"):
    r = next((x for x in rows if str(x["id"]) == str(tid)), None)
    if r is None:
        raise ValueError(f"trade id {tid} not found")
    if r.get("exit"):
        raise ValueError(f"trade id {tid} already closed")
    fees = fee_model(r["account"], ccy)
    qty, entry = float(r["qty"]), float(r["entry"])
    if exit_fee_eur is None:
        rate = fees["stop"] if reason == "stop" else fees["target"]
        exit_fee_eur = qty * exit_price * rate * fx_to_eur
    total_fees = (fnum(r.get("fees")) or 0) + exit_fee_eur
    pnl = qty * (exit_price - entry) * fx_to_eur - total_fees
    risk = fnum(r["risk_eur"])
    r.update(exit_date=date or now_local().strftime("%Y-%m-%d %H:%M"), exit=exit_price,
             exit_reason=reason, fees=round(total_fees, 4), pnl_eur=round(pnl, 4),
             result_r=round(pnl / risk, 3) if risk else "")
    if notes:
        r["notes"] = (r.get("notes") + " | " if r.get("notes") else "") + notes
    return r

def compute_stats(rows, account=None, now=None):
    now = now or now_local()
    rows = [r for r in rows if not account or r["account"] == account]
    closed = [r for r in rows if r.get("exit") not in ("", None)]
    open_ = [r for r in rows if r.get("exit") in ("", None)]

    def block(rs):
        rr = [fnum(r["result_r"]) for r in rs if fnum(r["result_r"]) is not None]
        pe = [fnum(r["pnl_eur"]) for r in rs if fnum(r["pnl_eur"]) is not None]
        n = len(rs)
        if n == 0:
            return dict(n=0)
        wins = [r for r in rs if (fnum(r["pnl_eur"]) or 0) > 0]
        losses = [r for r in rs if (fnum(r["pnl_eur"]) or 0) <= 0]
        avg = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
        aw_r = avg([fnum(r["result_r"]) for r in wins]); al_r = avg([fnum(r["result_r"]) for r in losses])
        aw_e = avg([fnum(r["pnl_eur"]) for r in wins]); al_e = avg([fnum(r["pnl_eur"]) for r in losses])
        wr = len(wins) / n
        # max drawdown on cumulative R (chronological by exit date)
        eq, peak, mdd = 0.0, 0.0, 0.0
        for r in sorted(rs, key=lambda x: x.get("exit_date") or ""):
            eq += fnum(r["result_r"]) or 0
            peak = max(peak, eq); mdd = min(mdd, eq - peak)
        return dict(n=n, wins=len(wins), losses=len(losses), win_rate_pct=round(wr * 100, 1),
                    avg_win_r=round(aw_r, 3), avg_loss_r=round(al_r, 3),
                    avg_win_eur=round(aw_e, 2), avg_loss_eur=round(al_e, 2),
                    expectancy_r=round(wr * aw_r + (1 - wr) * al_r, 3),
                    expectancy_eur=round(wr * aw_e + (1 - wr) * al_e, 3),
                    total_r=round(sum(rr), 3), total_pnl_eur=round(sum(pe), 2),
                    fees_eur=round(sum(fnum(r["fees"]) or 0 for r in rs), 2),
                    max_drawdown_r=round(mdd, 3))

    ym = now.strftime("%Y-%m")
    mtd = [r for r in closed if (r.get("exit_date") or "").startswith(ym)]
    setups = sorted({r["setup"] for r in closed})
    accts = sorted({r["account"] for r in closed})
    trips_month = {a: sum(1 for r in rows if r["account"] == a and r["date"].startswith(ym))
                   for a in sorted({r["account"] for r in rows})}
    return dict(account=account or "all", closed=block(closed), open_trades=len(open_),
                open_risk_eur=round(sum(fnum(r["risk_eur"]) or 0 for r in open_), 2),
                month=ym, mtd_net_pnl_eur=round(sum(fnum(r["pnl_eur"]) or 0 for r in mtd), 2),
                mtd_closed=len(mtd), entries_this_month=trips_month,
                per_setup={s: block([r for r in closed if r["setup"] == s]) for s in setups},
                per_account={a: block([r for r in closed if r["account"] == a]) for a in accts},
                note="win/loss = net pnl > 0 / <= 0; R = net pnl / risk_eur logged at entry")

# ----------------------------------------------------------------------------- market data
_PAIRINFO = None
def kraken_pairinfo(pairs=None):
    global _PAIRINFO
    if _PAIRINFO is None:
        res = kraken_public("AssetPairs")
        _PAIRINFO = {}
        for k, v in res.items():
            info = dict(key=k, altname=v["altname"], wsname=v.get("wsname"),
                        ordermin=fnum(v.get("ordermin")), costmin=fnum(v.get("costmin")),
                        lot_decimals=int(v.get("lot_decimals", 8)),
                        pair_decimals=int(v.get("pair_decimals", 5)), status=v.get("status"),
                        base=v.get("base"), quote=v.get("quote"))
            _PAIRINFO[v["altname"]] = info
            _PAIRINFO[k] = info
    return _PAIRINFO

def norm_pair(p):
    p = p.upper().replace("/", "").replace("-", "")
    p = p.replace("BTC", "XBT").replace("DOGE", "XDG")
    if not p.endswith(("EUR", "USD")):
        p += "EUR"
    return p

def is_yahoo_symbol(sym):
    """True for T212 / Yahoo equity-ETF tickers (exchange suffix like .DE, .MI, .PA)."""
    return bool(sym) and "." in str(sym).strip()

def resolve_daily_atr(pair, account="kraken"):
    """14-day Wilder ATR from Yahoo for stocks/ETFs, Kraken OHLC for crypto pairs."""
    if account == "t212" or is_yahoo_symbol(pair):
        y = yahoo_daily(pair)
        return atr_wilder(y["h"], y["l"], y["c"], 14)
    h, l, c, _ = kraken_daily(norm_pair(pair))
    return atr_wilder(h, l, c, 14)

def kraken_daily(pair):
    res = kraken_public("OHLC", pair=pair, interval=1440)
    key = [k for k in res if k != "last"][0]
    rows = res[key][:-1]  # drop in-progress candle
    h = [float(r[2]) for r in rows]; l = [float(r[3]) for r in rows]; c = [float(r[4]) for r in rows]
    return h, l, c, int(rows[-1][0])

def kraken_tickers(pairs):
    try:
        res = kraken_public("Ticker", pair=",".join(pairs))
    except RuntimeError as e:
        # One unknown pair (e.g. a tokenized asset like AAPLx.T with no EUR spot pair) fails the whole
        # batch: retry one by one and skip the pairs Kraken doesn't know.
        if "Unknown asset pair" not in str(e):
            raise
        res = {}
        for p in pairs:
            try:
                res.update(kraken_public("Ticker", pair=p))
            except RuntimeError as e1:
                if "Unknown asset pair" not in str(e1):
                    raise
    info = kraken_pairinfo()
    out = {}
    for k, v in res.items():
        alt = info.get(k, {}).get("altname", k)
        out[alt] = dict(last=float(v["c"][0]), bid=float(v["b"][0]), ask=float(v["a"][0]),
                        vol24=float(v["v"][1]), vwap24=float(v["p"][1]), open=float(v["o"]),
                        high24=float(v["h"][1]), low24=float(v["l"][1]))
    return out

def read_watchlist(path):
    """Symbols from a watchlist file: whitespace/comma separated, '#' starts a comment. De-duplicated, order kept."""
    out = []
    for line in open(path):
        out += [x for x in line.split("#", 1)[0].replace(",", " ").split()]
    return list(dict.fromkeys(out))

YCACHE_DIR = os.path.join(CACHE_DIR, "yahoo")

def _ycache_path(sym, rng):
    return os.path.join(YCACHE_DIR, f"{sym.replace('/', '_')}_{rng}.json")

def _ycache_load(sym, rng):
    try:
        with open(_ycache_path(sym, rng)) as f:
            return json.load(f)
    except Exception:
        return None

def _ycache_fresh(c, now=None):
    """Daily bars are refreshed once per (Rome) day, and once more after the session that was
    in progress at fetch time has closed (so the just-completed bar gets picked up)."""
    if not c:
        return False
    now = now or time.time()
    if dt.datetime.fromtimestamp(c["fetched_ts"]).date() != dt.datetime.fromtimestamp(now).date():
        return False
    end = c.get("period_end") or 0
    return not (now >= end > c["fetched_ts"])

def _yahoo_fetch_daily(sym, rng="1y"):
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={rng}&interval=1d")
    q = d["chart"]["result"][0]
    meta = q["meta"]; i = q["indicators"]["quote"][0]
    rows = [[t, h, l, c, v] for t, h, l, c, v in zip(q["timestamp"], i["high"], i["low"], i["close"], i["volume"])
            if c is not None and h is not None and l is not None]
    tp_ = meta.get("currentTradingPeriod", {}).get("regular", {})
    c = dict(sym=sym, rng=rng, fetched_ts=time.time(), rows=rows, currency=meta.get("currency", "USD"),
             name=meta.get("shortName") or meta.get("longName") or sym,
             price=meta.get("regularMarketPrice"), period_start=tp_.get("start"), period_end=tp_.get("end"))
    os.makedirs(YCACHE_DIR, exist_ok=True)
    tmp = _ycache_path(sym, rng) + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(c, f)
    os.replace(tmp, _ycache_path(sym, rng))
    return c

def yahoo_live_prices(syms, batch=20):
    """Last price + current regular trading period for many symbols via Yahoo spark (batched)."""
    out = {}
    syms = list(dict.fromkeys(syms))
    for k in range(0, len(syms), batch):
        chunk = syms[k:k + batch]
        try:
            d = http_json("https://query1.finance.yahoo.com/v7/finance/spark?symbols="
                          + urllib.parse.quote(",".join(chunk), safe=",") + "&range=1d&interval=1d")
            for r in d.get("spark", {}).get("result", []) or []:
                try:
                    m = r["response"][0]["meta"]
                    tp_ = m.get("currentTradingPeriod", {}).get("regular", {})
                    if m.get("regularMarketPrice") is not None:
                        out[r["symbol"]] = dict(price=float(m["regularMarketPrice"]), time=m.get("regularMarketTime"),
                                                period_start=tp_.get("start"), period_end=tp_.get("end"))
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
        except Exception:
            pass  # callers fall back to the chart price
        time.sleep(0.15)
    return out

def yahoo_prefetch(syms, rng="1y", workers=4):
    """Refresh stale cached daily bars in parallel (few workers to avoid throttling). Returns {sym: error}."""
    from concurrent.futures import ThreadPoolExecutor
    stale = [s for s in dict.fromkeys(syms) if not _ycache_fresh(_ycache_load(s, rng))]
    errs = {}
    def one(s):
        try:
            _yahoo_fetch_daily(s, rng)
        except Exception as e:
            errs[s] = str(e)[:200]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, stale))
    return errs

def yahoo_daily(sym, rng="1y", live=None, use_cache=True):
    """Completed daily bars (cached, refreshed daily) + live price (from `live` = yahoo_live_prices item,
    else the chart's regularMarketPrice). An open session's partial bar is dropped."""
    c = _ycache_load(sym, rng) if use_cache else None
    if not _ycache_fresh(c):
        c = _yahoo_fetch_daily(sym, rng)
    rows = c["rows"]
    ps = (live or {}).get("period_start") or c.get("period_start")
    pe = (live or {}).get("period_end") or c.get("period_end")
    partial = False
    nowts = time.time()
    if rows and ps and pe and ps <= nowts < pe and rows[-1][0] >= ps:
        rows = rows[:-1]; partial = True
    price = (live or {}).get("price") or c.get("price") or rows[-1][3]
    return dict(h=[r[1] for r in rows], l=[r[2] for r in rows], c=[r[3] for r in rows],
                v=[r[4] or 0 for r in rows], last_ts=rows[-1][0], currency=c.get("currency", "USD"),
                price=price, partial_dropped=partial, name=c.get("name") or sym,
                cached_at=dt.datetime.fromtimestamp(c["fetched_ts"]).strftime("%Y-%m-%d %H:%M"))

_FX = {}
def fx_to_eur(ccy):
    ccy = (ccy or "EUR")
    if ccy.upper() == "EUR":
        return 1.0
    div = 100.0 if ccy in ("GBp", "GBX") else 1.0
    base = "GBP" if div == 100.0 else ccy.upper()
    if base not in _FX:
        d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{base}EUR=X?range=5d&interval=1d")
        _FX[base] = float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
    return _FX[base] / div

# ----------------------------------------------------------------------------- live account state (read-only)
def kraken_asset_pair(asset):
    a = asset.split(".")[0]
    a = LEGACY.get(a, a)
    return a, (a + "EUR")

def kraken_state(rows=None):
    rows = rows if rows is not None else read_trades()
    bal = kraken_cli("balance")
    oo = kraken_cli("open-orders").get("open", {})
    held, pairs = {}, set()
    cash_eur = 0.0; usd = 0.0
    for asset, amt in bal.items():
        amt = float(amt)
        if amt <= 0:
            continue
        a, p = kraken_asset_pair(asset)
        if a == "EUR":
            cash_eur += amt
        elif a == "USD":
            usd += amt
        else:
            held[p] = held.get(p, 0) + amt; pairs.add(p)
    tick = kraken_tickers(sorted(pairs)) if pairs else {}
    eurusd = kraken_tickers(["EURUSD"]).get("EURUSD", {}).get("last") if usd else None
    stops = {}
    for oid, o in oo.items():
        d = o["descr"]
        if d["side"] == "sell" and d["ordertype"] in ("stop-loss", "stop-loss-limit", "trailing-stop"):
            p = d["pair"]
            s = stops.setdefault(p, dict(qty=0.0, price=0.0, ids=[]))
            s["qty"] += float(o["volume"]) - float(o.get("volume_executed", 0))
            s["price"] = max(s["price"], float(d["price"])); s["ids"].append(oid)
    fees = fee_model("kraken")
    positions, open_risk = [], 0.0
    for p, q in held.items():
        px = tick.get(p, {}).get("last")
        if px is None:
            continue
        val = q * px
        if val < 1.0:  # dust
            continue
        logrow = next((r for r in reversed(rows) if r["account"] == "kraken" and r["pair"] == p and not r.get("exit")), None)
        entry = fnum(logrow["entry"]) if logrow else px
        st = stops.get(p)
        covered = min(q, st["qty"]) if st else 0.0
        stop_px = st["price"] if st else None
        risk = 0.0
        if st:
            risk += covered * max(0.0, entry - stop_px) + covered * stop_px * fees["stop"] \
                + covered * entry * fees["entry"]  # entry fee already paid counts as part of the loss
        risk += (q - covered) * px  # unprotected part counts as fully at risk
        open_risk += risk
        positions.append(dict(pair=p, qty=q, price=px, value_eur=round(val, 2), entry=entry,
                              stop=stop_px, stop_order_ids=st["ids"] if st else [],
                              unprotected_qty=round(q - covered, 8), risk_eur=round(risk, 4),
                              logged_trade_id=logrow["id"] if logrow else None))
    equity = cash_eur + sum(x["value_eur"] for x in positions) + (usd / eurusd if usd and eurusd else 0)
    return dict(equity_eur=round(equity, 2), cash_eur=round(cash_eur, 2), usd=usd,
                positions=positions, open_positions=len(positions), open_risk_eur=round(open_risk, 4),
                open_orders=len(oo))

def t212_state():
    cash = t212_get("/equity/account/cash")
    pf = t212_get("/equity/portfolio")
    orders = t212_get("/equity/orders")
    stops = {}
    for o in orders:
        if o.get("type") in ("STOP", "STOP_LIMIT") and float(o.get("quantity", 0)) < 0:
            s = stops.setdefault(o["ticker"], dict(qty=0.0, price=0.0))
            s["qty"] += -float(o["quantity"]); s["price"] = max(s["price"], float(o.get("stopPrice") or 0))
    positions, open_risk = [], 0.0
    for p in pf:
        t = p["ticker"]; q = float(p["quantity"]); avg = float(p["averagePrice"]); px = float(p["currentPrice"])
        ccy = "USD" if t.endswith("_US_EQ") else "EUR"
        try:
            fx = fx_to_eur(ccy)
        except Exception:
            fx = 1.0
        st = stops.get(t); covered = min(q, st["qty"]) if st else 0.0
        risk = (covered * max(0.0, avg - st["price"]) if st else 0.0) * fx + (q - covered) * px * fx
        open_risk += risk
        positions.append(dict(ticker=t, qty=q, avg=avg, price=px, stop=st["price"] if st else None,
                              unprotected_qty=q - covered, risk_eur=round(risk, 4)))
    return dict(equity_eur=float(cash.get("total", 0)), cash_eur=float(cash.get("free", 0)),
                positions=positions, open_positions=len(positions), open_risk_eur=round(open_risk, 4),
                open_orders=len(orders))

# ----------------------------------------------------------------------------- gate (pure)
def gate(account, entry, stop, target, atr, fees, equity, open_positions, open_risk_eur,
         new_risk_eur, rows, pair, now=None, size_problems=(), cash_eur=None, notional_eur=None,
         held_pairs=(), max_target_atr=None):
    c = CFG[account]; now = now or now_local()
    reasons, passes, warns = [], [], []
    def chk(ok, msg_ok, msg_fail):
        (passes if ok else reasons).append(msg_ok if ok else msg_fail)
    rm = r_math(entry, stop, target, fees)
    sd = entry - stop
    need = max(c["min_stop_atr"] * atr, entry * c["min_stop_pct"] / 100)
    chk(sd >= need - 1e-12,
        f"stop dist {rm['stop_dist_pct']:.2f}% = {sd/atr:.2f}xATR >= {c['min_stop_atr']}xATR/{c['min_stop_pct']}%",
        f"stop too tight: {rm['stop_dist_pct']:.2f}% = {sd/atr:.2f}xATR; need >= {c['min_stop_atr']}xATR"
        + (f" and >= {c['min_stop_pct']}%" if c['min_stop_pct'] else "") + f" -> stop <= {entry-need:.6g}")
    mt = min_target_for_rr(entry, stop, fees, c["min_rr_net"])
    chk(rm["rr_net"] >= c["min_rr_net"] - 1e-9,
        f"R:R net of fees {rm['rr_net']:.2f} >= {c['min_rr_net']}",
        f"R:R net of fees {rm['rr_net']:.2f} < {c['min_rr_net']} -> target must be >= {mt:.6g}")
    mta = max_target_atr if max_target_atr is not None else c["max_target_atr"]
    tatr = (target - entry) / atr
    chk(tatr <= mta + 1e-9, f"target {tatr:.2f}xATR <= {mta}xATR",
        f"target too far: {tatr:.2f}xATR > {mta}xATR (needs {(mt-entry)/atr:.2f}xATR for {c['min_rr_net']}R net)")
    chk(open_positions < c["max_open"], f"open positions {open_positions} < {c['max_open']}",
        f"max open positions reached ({open_positions}/{c['max_open']})")
    tot = open_risk_eur + new_risk_eur
    cap = equity * c["max_open_risk_pct"] / 100
    chk(tot <= cap + 1e-9, f"total open risk {tot:.2f} <= {cap:.2f} EUR ({c['max_open_risk_pct']}%)",
        f"total open risk {tot:.2f} EUR ({open_risk_eur:.2f} open + {new_risk_eur:.2f} new) > cap {cap:.2f} ({c['max_open_risk_pct']}% of {equity:.2f})")
    chk(equity >= c["equity_floor"], f"equity {equity:.2f} >= floor {c['equity_floor']}",
        f"equity {equity:.2f} below floor {c['equity_floor']}: no new positions")
    hp = {h.upper() for h in held_pairs} | {h.upper().split(".")[0] for h in held_pairs}
    chk(pair.upper() not in hp and pair.upper().split(".")[0] not in hp, "no existing position in pair",
        f"already holding {pair}: no duplicate/pyramid")
    # cooldown after stop-out (loss) on same pair
    cd = None
    for r in rows:
        if r["account"] == account and r["pair"].upper() == pair.upper() and r.get("exit_reason") == "stop" \
                and (fnum(r.get("result_r")) or 0) < 0:
            t = parse_ts(r.get("exit_date"))
            if t and now - t < dt.timedelta(hours=c["cooldown_h"]):
                cd = t
    chk(cd is None, f"no stop-out on {pair} in last {c['cooldown_h']}h",
        f"cooldown: {pair} stopped out at {cd} (< {c['cooldown_h']}h ago)")
    ym = now.strftime("%Y-%m")
    trips = sum(1 for r in rows if r["account"] == account and r["date"].startswith(ym))
    chk(trips < c["max_trips_month"], f"entries this month {trips} < {c['max_trips_month']}",
        f"monthly cap reached: {trips} entries in {ym} (max {c['max_trips_month']})")
    for p in size_problems:
        reasons.append("size: " + p)
    if cash_eur is not None and notional_eur is not None:
        chk(notional_eur * (1 + fees["entry"]) <= cash_eur, f"cash {cash_eur:.2f} covers notional",
            f"insufficient cash: need {notional_eur*(1+fees['entry']):.2f}, have {cash_eur:.2f}")
    if rm["rr_gross"] < 1:
        warns.append("gross R:R < 1")
    return dict(verdict="PASS" if not reasons else "FAIL", fail_reasons=reasons, passed=passes,
                warnings=warns, rr_net=round(rm["rr_net"], 3), rr_gross=round(rm["rr_gross"], 3),
                breakeven_winrate_pct=round(rm["breakeven_winrate_pct"], 1),
                stop_dist_pct=round(rm["stop_dist_pct"], 3), stop_atr_mult=round(sd / atr, 3),
                target_atr_mult=round(tatr, 3), min_target_for_2R_net=round(mt, 8),
                fees=fees)

# ----------------------------------------------------------------------------- commands
def emit(obj, human, args):
    if not getattr(args, "json_only", False):
        for line in human:
            print("# " + line)
    print(json.dumps(obj, indent=None if getattr(args, "compact", False) else 1, default=str))

def scan_crypto(pairs, args):
    info = kraken_pairinfo()
    tick = kraken_tickers(pairs)
    fees = fee_model("kraken", tp_fee=args.tp_fee)
    out, errs = [], {}
    btc_mom = None
    for p in pairs:
        try:
            h, l, c, lastts = kraken_daily(p)
            t = tick[p]
            m = trend_metrics(h, l, c, t["last"])
            spread = (t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 100
            vol_eur = t["vol24"] * t["vwap24"]
            stop_pct = max(CFG["kraken"]["min_stop_atr"] * m["atr_pct"], CFG["kraken"]["min_stop_pct"])
            px = t["last"]; stop = px * (1 - stop_pct / 100)
            mt = min_target_for_rr(px, stop, fees, 2.0)
            liquid = vol_eur >= args.min_vol_eur and spread <= args.max_spread
            pi = info.get(p, {})
            row = dict(pair=p, price=px, atr_pct=m["atr_pct"], atr=rnd(m["atr"], 8),
                       sma20=rnd(m["sma20"], 6), sma50=rnd(m["sma50"], 6), ema20=rnd(m["ema20"], 6),
                       ema50=rnd(m["ema50"], 6), above_sma20=m["above_sma20"], above_sma50=m["above_sma50"],
                       sma20_gt_sma50=m["sma20_gt_sma50"], sma20_rising=m["sma20_rising"],
                       mom20_pct=m["mom20_pct"], mom63_pct=m["mom63_pct"], chg24h_pct=rnd(pct(px, t["open"]), 2),
                       dist_hi20_pct=m["dist_hi20_pct"], vol24_eur=round(vol_eur), spread_pct=round(spread, 4),
                       liquid=liquid, uptrend=m["uptrend"], candidate=bool(m["uptrend"] and liquid),
                       min_stop_pct=round(stop_pct, 2), min_stop_price=round(stop, pi.get("pair_decimals", 6)),
                       target_for_2R_net=round(mt, pi.get("pair_decimals", 6)),
                       target_for_2R_net_pct=round((mt / px - 1) * 100, 2),
                       target_for_2R_atr=round((mt - px) / m["atr"], 2),
                       ordermin=pi.get("ordermin"), costmin=pi.get("costmin"),
                       last_daily_close_utc=dt.datetime.fromtimestamp(lastts, dt.timezone.utc).strftime("%Y-%m-%d"))
            if p == "XBTEUR":
                btc_mom = m["mom20_pct"]
            out.append(row)
            time.sleep(0.35)
        except Exception as e:
            errs[p] = str(e)[:200]
    for r in out:
        r["rs_vs_btc20_pct"] = rnd(r["mom20_pct"] - btc_mom, 2) if btc_mom is not None and r["mom20_pct"] is not None else None
    out.sort(key=lambda r: (not r["candidate"], -(r["mom20_pct"] or -999)))
    return out, errs

def scan_stocks(syms, args):
    out, errs = [], {}
    live = yahoo_live_prices(syms)          # batched last prices (one request per 20 symbols)
    errs.update(yahoo_prefetch(syms))       # daily bars: only stale caches are refetched
    for s in syms:
        if s in errs:
            continue
        try:
            y = yahoo_daily(s, live=live.get(s))
            m = trend_metrics(y["h"], y["l"], y["c"], y["price"])
            ccy = y["currency"]
            fees = fee_model("t212", "EUR" if ccy == "EUR" else "USD")
            stop_pct = CFG["t212"]["min_stop_atr"] * m["atr_pct"]
            px = y["price"]; stop = px * (1 - stop_pct / 100)
            mt = min_target_for_rr(px, stop, fees, 2.0)
            try:
                fx = fx_to_eur(ccy)
            except Exception:
                fx = None
            adv = sum(c * v for c, v in zip(y["c"][-20:], y["v"][-20:])) / 20
            mom_ok = (m["mom126_pct"] or 0) > 0
            row = dict(symbol=s, name=y["name"], currency=ccy, price=px, atr_pct=m["atr_pct"],
                       atr=rnd(m["atr"], 4), sma20=rnd(m["sma20"], 4), sma50=rnd(m["sma50"], 4),
                       above_sma20=m["above_sma20"], above_sma50=m["above_sma50"],
                       sma20_gt_sma50=m["sma20_gt_sma50"], sma20_rising=m["sma20_rising"],
                       mom20_pct=m["mom20_pct"], mom63_pct=m["mom63_pct"], mom126_pct=m["mom126_pct"],
                       dist_hi20_pct=m["dist_hi20_pct"], avg_dollar_vol20=round(adv),
                       avg_eur_vol20=round(adv * fx) if fx else None, spread_pct=None,
                       uptrend=m["uptrend"], candidate=bool(m["uptrend"] and mom_ok),
                       min_stop_pct=round(stop_pct, 2), min_stop_price=round(stop, 4),
                       target_for_2R_net=round(mt, 4), target_for_2R_atr=round((mt - px) / m["atr"], 2),
                       fx_fee_per_leg_pct=fees["entry"] * 100,
                       last_daily_bar=dt.datetime.fromtimestamp(y["last_ts"], dt.timezone.utc).strftime("%Y-%m-%d"))
            row["live_price"] = s in live
            out.append(row)
        except Exception as e:
            errs[s] = str(e)[:200]
    out.sort(key=lambda r: (not r["candidate"], -(r["mom63_pct"] or -999)))
    return out, errs

def cmd_scan(args):
    if args.mode == "crypto":
        pairs = [norm_pair(p) for p in (args.pairs.split(",") if args.pairs else CRYPTO_PAIRS)]
        rows, errs = scan_crypto(pairs, args)
        human = [f"Kraken daily scan {now_local():%Y-%m-%d %H:%M} (Europe/Rome). Candidate = price>SMA20>SMA50, SMA20 rising, 20d mom>0, vol24>={args.min_vol_eur:.0f} EUR, spread<={args.max_spread}%"]
        for r in rows[:args.top]:
            human.append(f"{'*' if r['candidate'] else ' '} {r['pair']:8} {r['price']:<12g} ATR {r['atr_pct']:>5}%  mom20 {r['mom20_pct']:>6}%  >SMA20 {str(r['above_sma20'])[0]} >SMA50 {str(r['above_sma50'])[0]}  vol {r['vol24_eur']/1e3:,.0f}k  spr {r['spread_pct']:.3f}%  stop<= {r['min_stop_price']} tgt2R>= {r['target_for_2R_net']} ({r['target_for_2R_atr']}xATR)")
    else:
        wl = args.watchlist or (DEFAULT_WATCHLIST if os.path.exists(DEFAULT_WATCHLIST) else None)
        syms = args.symbols.split(",") if args.symbols else (
            read_watchlist(wl) if wl else STOCK_WATCHLIST)
        rows, errs = scan_stocks(syms, args)
        human = [f"Stocks daily scan (Yahoo) {now_local():%Y-%m-%d %H:%M} (Europe/Rome). Candidate = price>SMA20>SMA50, SMA20 rising, 20d mom>0, 6m mom>0"]
        for r in rows[:args.top]:
            human.append(f"{'*' if r['candidate'] else ' '} {r['symbol']:8} {r['currency']} {r['price']:<10g} ATR {r['atr_pct']:>5}%  mom20 {r['mom20_pct']:>6}%  mom63 {r['mom63_pct']}%  mom126 {r['mom126_pct']}%  stop<= {r['min_stop_price']} tgt2R>= {r['target_for_2R_net']}")
    cands = [r.get("pair") or r.get("symbol") for r in rows if r["candidate"]]
    human.append(f"Candidates: {', '.join(cands) if cands else 'none'}" + (f" | errors: {list(errs)}" if errs else ""))
    emit(dict(mode=args.mode, time=now_local().isoformat(), candidates=cands, rows=rows, errors=errs), human, args)

def resolve_market(args, account):
    """Returns (atr, ccy, fx, pairinfo, last_price)."""
    if account == "kraken" and not is_yahoo_symbol(args.pair):
        p = norm_pair(args.pair); args.pair = p
        pi = kraken_pairinfo().get(p) or {}
        atr = args.atr; last = None
        if atr is None and args.atr_pct is not None:
            atr = args.entry * args.atr_pct / 100
        if atr is None:
            atr = resolve_daily_atr(p, "kraken")
        return atr, "EUR", 1.0, pi, last
    ccy = args.ccy
    atr = args.atr
    if atr is None and args.atr_pct is not None:
        atr = args.entry * args.atr_pct / 100
    if atr is None or ccy is None:
        y = yahoo_daily(args.pair)
        ccy = ccy or y["currency"]
        if atr is None:
            atr = atr_wilder(y["h"], y["l"], y["c"], 14)
    fx = args.fx if args.fx else fx_to_eur(ccy)
    return atr, ccy, fx, {}, None

def do_size(args, account, fees, fx, pi, equity):
    if account == "kraken":
        return compute_size(account, equity, args.risk, args.entry, args.stop, fees, fx,
                            ordermin=pi.get("ordermin"), costmin=pi.get("costmin"),
                            lot_decimals=pi.get("lot_decimals", 8))
    c = CFG["t212"]
    s = compute_size(account, equity, args.risk, args.entry, args.stop, fees, fx,
                     lot_decimals=args.qty_decimals or c["qty_decimals"])
    if s["notional_eur"] < c["min_notional_eur"]:
        s["problems"].append(f"notional {s['notional_eur']} < {c['min_notional_eur']} EUR"); s["ok"] = False
    return s

def live_state(account, args):
    if args.offline:
        return None
    try:
        return kraken_state() if account == "kraken" else t212_state()
    except Exception as e:
        return dict(error=str(e)[:200])

def cmd_size(args):
    acct = args.account
    fees = fee_model(acct, args.ccy or "EUR", args.tp_fee)
    pi, fx = {}, 1.0
    if acct == "kraken":
        args.pair = norm_pair(args.pair) if args.pair else None
        if args.pair and not args.offline:
            pi = kraken_pairinfo().get(args.pair, {})
    else:
        if args.ccy is None and args.pair and not args.offline:
            args.ccy = yahoo_daily(args.pair)["currency"]
        args.ccy = args.ccy or "EUR"
        fees = fee_model(acct, args.ccy, args.tp_fee)
        fx = args.fx or (1.0 if args.ccy == "EUR" or args.offline else fx_to_eur(args.ccy))
    equity = args.equity
    st = None
    if equity is None:
        st = live_state(acct, args)
        equity = (st or {}).get("equity_eur") or CFG[acct]["start_capital"]
    s = do_size(args, acct, fees, fx, pi, equity)
    if args.target:
        s["r_math"] = {k: rnd(v, 6) for k, v in r_math(args.entry, args.stop, args.target, fees).items()}
    s.update(account=acct, pair=args.pair, equity_eur=equity, risk_pct=args.risk, fees=fees, fx_to_eur=fx)
    human = [f"{acct} {args.pair or ''}: equity {equity:.2f} EUR, risk {args.risk}% -> qty {s['qty']} "
             f"(notional {s['notional_eur']} EUR, risk {s['risk_eur']} EUR incl. fees{', CAPPED by max notional '+str(s['max_notional_eur']) if s['capped_by_max_notional'] else ''})",
             "OK" if s["ok"] else "PROBLEM: " + "; ".join(s["problems"])]
    emit(s, human, args)

def cmd_check(args):
    acct = args.account
    atr, ccy, fx, pi, _ = (args.atr, args.ccy or "EUR", args.fx or 1.0, {}, None) if args.offline else resolve_market(args, acct)
    if args.offline and atr is None:
        if args.atr_pct is None:
            raise SystemExit("--offline needs --atr or --atr-pct")
        atr = args.entry * args.atr_pct / 100
    if acct == "kraken":
        args.pair = norm_pair(args.pair)
    fees = fee_model(acct, ccy, args.tp_fee)
    rows = read_trades()
    st = live_state(acct, args) if (args.equity is None or args.open_positions is None or args.open_risk_eur is None) else None
    stv = st if st and "error" not in st else {}
    equity = args.equity if args.equity is not None else stv.get("equity_eur", CFG[acct]["start_capital"])
    open_rows = [r for r in rows if r["account"] == acct and not r.get("exit")]
    open_pos = args.open_positions if args.open_positions is not None else stv.get("open_positions", len(open_rows))
    open_risk = args.open_risk_eur if args.open_risk_eur is not None else stv.get(
        "open_risk_eur", sum(fnum(r["risk_eur"]) or 0 for r in open_rows))
    held = {p["pair"] for p in stv.get("positions", []) if "pair" in p} | \
           {p["ticker"].split("_")[0] for p in stv.get("positions", []) if "ticker" in p} | \
           {r["pair"] for r in open_rows}
    pair_key = args.pair.upper()
    size = do_size(args, acct, fees, fx, pi, equity)
    if args.qty is not None:  # evaluate a specific quantity instead of the sized one
        rm = r_math(args.entry, args.stop, None, fees)
        size["qty"] = args.qty; size["notional_eur"] = round(args.qty * args.entry * fx, 2)
        size["risk_eur"] = round(args.qty * rm["risk_per_unit_net"] * fx, 4)
        if size["notional_eur"] > size["max_notional_eur"] + 1e-9:
            size["problems"].append(f"notional {size['notional_eur']} > max {size['max_notional_eur']}")
        if pi.get("ordermin") and args.qty < pi["ordermin"]:
            size["problems"].append(f"qty < ordermin {pi['ordermin']}")
        if size["risk_eur"] > equity * args.risk / 100 * 1.05:
            size["problems"].append(f"risk {size['risk_eur']} EUR > {args.risk}% of equity")
    g = gate(acct, args.entry, args.stop, args.target, atr, fees, equity, open_pos, open_risk,
             size["risk_eur"], rows, pair_key, size_problems=size["problems"],
             cash_eur=stv.get("cash_eur") if acct == "kraken" else None, notional_eur=size["notional_eur"],
             held_pairs=held, max_target_atr=args.max_target_atr)
    g.update(account=acct, pair=args.pair, entry=args.entry, stop=args.stop, target=args.target,
             atr=rnd(atr, 8), atr_pct=rnd(atr / args.entry * 100, 3), currency=ccy, fx_to_eur=fx,
             equity_eur=equity, open_positions=open_pos, open_risk_eur=open_risk, size=size,
             live_state_error=(st or {}).get("error"))
    human = [f"{g['verdict']}: {acct} {args.pair} entry {args.entry} stop {args.stop} target {args.target} | "
             f"R:R net {g['rr_net']} (BE win rate {g['breakeven_winrate_pct']}%), stop {g['stop_atr_mult']}xATR, target {g['target_atr_mult']}xATR, qty {size['qty']} risk {size['risk_eur']} EUR"]
    human += ["FAIL: " + r for r in g["fail_reasons"]]
    emit(g, human, args)
    return 0 if g["verdict"] == "PASS" else 2

def cmd_trail(args):
    atr = args.atr
    if atr is None:
        if args.atr_pct is not None:
            atr = args.price * args.atr_pct / 100
        elif args.pair:
            # Stocks/ETFs (DB1.DE, …) always use Yahoo — even if --account was left at the
            # default "kraken" (T212 session rules historically omit --account on trail).
            atr = resolve_daily_atr(args.pair, args.account)
        else:
            raise SystemExit("need --atr, --atr-pct or --pair")
    hc = args.highest_close
    t = trail_stop(args.entry, args.initial_stop, args.price, hc, atr, args.current_stop)
    t.update(atr=rnd(atr, 8))
    if args.decimals is not None:
        t["recommended_stop"] = floor_dec(t["recommended_stop"], args.decimals)
    human = [f"{t['action']}: stop {t['current_stop']} -> {t['recommended_stop']} ({t['recommended_stop_R']}R) | progress {t['progress_R']}R, stage: {t['stage']}"] + t["notes"]
    emit(t, human, args)

def cmd_log(args):
    rows = read_trades()
    fx = args.fx or 1.0
    if args.action == "open":
        r = log_open(rows, args.account, args.pair, args.setup, args.entry, args.stop, args.target,
                     args.qty, args.fees, args.date, args.notes or "", fx, args.ccy or "EUR")
    elif args.action == "close":
        r = log_close(rows, args.id, args.exit, args.reason, args.fees, args.date, args.notes, fx, args.ccy or "EUR")
    elif args.action == "update-stop":
        r = next(x for x in rows if str(x["id"]) == str(args.id))
        old = r["stop"]; r["stop"] = args.stop
        r["notes"] = (r.get("notes") + " | " if r.get("notes") else "") + f"stop {old}->{args.stop} {now_local():%m-%d %H:%M}"
    else:
        raise SystemExit("unknown log action")
    write_trades(rows)
    emit(dict(action=args.action, trade=r, file=TRADES_CSV), [f"logged ({args.action}) trade #{r['id']} {r['pair']} -> {TRADES_CSV}"], args)

def cmd_stats(args):
    s = compute_stats(read_trades(), args.account)
    c = s["closed"]
    human = [f"{s['account']}: {c.get('n',0)} closed, open {s['open_trades']} (open risk {s['open_risk_eur']} EUR), MTD net {s['mtd_net_pnl_eur']} EUR, entries this month {s['entries_this_month']}"]
    if c.get("n"):
        human.append(f"win {c['win_rate_pct']}% avgW {c['avg_win_r']}R/{c['avg_win_eur']}EUR avgL {c['avg_loss_r']}R/{c['avg_loss_eur']}EUR expectancy {c['expectancy_r']}R / {c['expectancy_eur']} EUR, maxDD {c['max_drawdown_r']}R")
    emit(s, human, args)

def cmd_state(args):
    st = kraken_state() if args.account == "kraken" else t212_state()
    emit(st, [f"{args.account}: equity {st['equity_eur']} EUR, cash {st['cash_eur']}, positions {st['open_positions']}, open risk {st['open_risk_eur']} EUR"], args)

def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json-only", action="store_true"); ap.add_argument("--compact", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan"); s.add_argument("mode", choices=["crypto", "stocks"])
    s.add_argument("--pairs"); s.add_argument("--symbols"); s.add_argument("--watchlist")
    s.add_argument("--top", type=int, default=20); s.add_argument("--min-vol-eur", type=float, default=250000)
    s.add_argument("--max-spread", type=float, default=0.25); s.add_argument("--tp-fee", type=float)

    def trade_args(p, need_target):
        p.add_argument("--account", choices=["kraken", "t212"], required=True)
        p.add_argument("--pair", help="Kraken pair (LINKEUR, LINK, BTC) or Yahoo symbol (AAPL, VWCE.DE)")
        p.add_argument("--entry", type=float, required=True); p.add_argument("--stop", type=float, required=True)
        p.add_argument("--target", type=float, required=need_target)
        p.add_argument("--equity", type=float); p.add_argument("--risk", type=float, default=1.0)
        p.add_argument("--atr", type=float); p.add_argument("--atr-pct", type=float)
        p.add_argument("--ccy"); p.add_argument("--fx", type=float); p.add_argument("--tp-fee", type=float)
        p.add_argument("--qty-decimals", type=int); p.add_argument("--offline", action="store_true")
    s = sub.add_parser("size"); trade_args(s, False)
    s = sub.add_parser("check"); trade_args(s, True)
    s.add_argument("--qty", type=float, help="check this exact qty instead of the sized qty")
    s.add_argument("--open-positions", type=int); s.add_argument("--open-risk-eur", type=float)
    s.add_argument("--max-target-atr", type=float)

    s = sub.add_parser("trail")
    s.add_argument("--account", choices=["kraken", "t212"], default="kraken"); s.add_argument("--pair")
    s.add_argument("--entry", type=float, required=True); s.add_argument("--initial-stop", type=float, required=True)
    s.add_argument("--current-stop", type=float); s.add_argument("--price", type=float, required=True)
    s.add_argument("--highest-close", type=float); s.add_argument("--atr", type=float); s.add_argument("--atr-pct", type=float)
    s.add_argument("--decimals", type=int)

    s = sub.add_parser("log"); s.add_argument("action", choices=["open", "close", "update-stop"])
    s.add_argument("--id"); s.add_argument("--account", choices=["kraken", "t212"]); s.add_argument("--pair")
    s.add_argument("--setup", default="untagged"); s.add_argument("--entry", type=float); s.add_argument("--stop", type=float)
    s.add_argument("--target", type=float); s.add_argument("--qty", type=float); s.add_argument("--fees", type=float)
    s.add_argument("--exit", type=float); s.add_argument("--reason", choices=["stop", "target", "trail", "manual", "time"])
    s.add_argument("--date"); s.add_argument("--notes"); s.add_argument("--fx", type=float); s.add_argument("--ccy")

    s = sub.add_parser("stats"); s.add_argument("--account", choices=["kraken", "t212"])
    s = sub.add_parser("state"); s.add_argument("--account", choices=["kraken", "t212"], required=True)
    return ap

def main(argv=None):
    args = build_parser().parse_args(argv)
    fn = dict(scan=cmd_scan, size=cmd_size, check=cmd_check, trail=cmd_trail, log=cmd_log,
              stats=cmd_stats, state=cmd_state)[args.cmd]
    rc = fn(args)
    return rc or 0

if __name__ == "__main__":
    sys.exit(main())
