#!/usr/bin/env python3
"""Offline tests for watcher.py cooldown / cap / session / rule logic. Never POSTs."""
import datetime as dt, os, sys, time
os.environ.pop("WATCH_WEBHOOK_URL", None); os.environ.pop("WATCH_WEBHOOK_AUTH", None)
os.environ["CLOUD_WATCHER"] = "0"; os.environ["WATCH_CLOUD_HEALTHCHECK"] = "0"   # tests must not depend on the box's watch.env flag
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher as w, tempfile
_T = tempfile.mkdtemp()
w.ALERTS = os.path.join(_T, "alerts.jsonl"); w.STATE_FILE = os.path.join(_T, "state.json"); w.PENDING = os.path.join(_T, "pending.jsonl")
import pending as pcli

R = w.ROME
def st(): return dict(cooldowns={}, wakes=[], status={}, levels_fired={})
def fl(sym="SOLEUR", reason="candidate", acct="kraken"):
    return dict(account=acct, symbol=sym, reason=reason, detail="x", metrics={"price": 1})
QUIET = dt.datetime(2026, 11, 1, 0, 30, tzinfo=R)       # next kraken session 02:58 -> not due

def poster_ok(calls):
    def p(payload): calls.append(payload); return 200
    return p

def test_cooldown():
    s = st(); calls = []
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        r1 = w.handle_flags([fl()], s, QUIET, poster=poster_ok(calls))
        r2 = w.handle_flags([fl()], s, QUIET + dt.timedelta(hours=5, minutes=59), poster=poster_ok(calls))
        r3 = w.handle_flags([fl()], s, QUIET + dt.timedelta(hours=6, minutes=1), poster=poster_ok(calls))
        r4 = w.handle_flags([fl(reason="near_candidate")], s, QUIET, poster=poster_ok(calls))  # other reason = own key
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    assert [x["action"] for x in r1] == ["wake"] and r2 == [] and r3[0]["action"] == "wake"
    assert r4[0]["action"] == "wake" and len(calls) == 3

def test_daily_cap():
    s = st(); calls = []
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        res = w.handle_flags([fl(sym=f"S{i}") for i in range(8)], s, QUIET, poster=poster_ok(calls))
        nextday = dt.datetime(2026, 11, 2, 0, 30, tzinfo=R)
        res2 = w.handle_flags([fl(sym="NEW")], s, nextday, poster=poster_ok(calls))
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    acts = [x["action"] for x in res]
    assert acts.count("wake") == 6 and acts.count("cap") == 2 and len(calls) == 7
    assert w.wakes_today(s, QUIET) == 6 and res2[0]["action"] == "wake"   # cap resets on Rome day change

def test_failed_post_not_counted():
    s = st()
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        r = w.handle_flags([fl()], s, QUIET, poster=lambda p: 500)
        again_soon = w.handle_flags([fl()], s, QUIET + dt.timedelta(minutes=10), poster=lambda p: 200)
        later = w.handle_flags([fl()], s, QUIET + dt.timedelta(minutes=31), poster=lambda p: 200)
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    assert r[0]["action"] == "post_failed" and s["wakes"] != [] and again_soon == [] and later[0]["action"] == "wake"
    assert len(s["wakes"]) == 1

def test_not_configured():
    s = st()
    r = w.handle_flags([fl()], s, QUIET, poster=lambda p: (_ for _ in ()).throw(AssertionError("posted")))
    assert r[0]["action"] == "not_configured" and s["wakes"] == []

def test_session_due_skip():
    s = st()
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        t = dt.datetime(2026, 11, 1, 2, 40, tzinfo=R)      # kraken session 02:58 is 18 min away
        r = w.handle_flags([fl(), fl(reason="near_stop")], s, t, poster=lambda p: 200)
        # T212: Monday 10:55 -> 11:21 due in 26 min ; Sunday has no T212 sessions
        mon = dt.datetime(2026, 11, 2, 10, 55, tzinfo=R)
        assert w.session_due_soon("t212", mon)[0]
        assert not w.session_due_soon("t212", dt.datetime(2026, 11, 1, 10, 55, tzinfo=R))[0]
        assert not w.session_due_soon("kraken", dt.datetime(2026, 11, 1, 2, 27, tzinfo=R))[0]
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    assert [x["action"] for x in r] == ["session_due", "wake"]   # near_stop is urgent

def test_market_hours():
    mon = lambda h, m: dt.datetime(2026, 11, 2, h, m, tzinfo=R)
    assert w.market_open("SAP.DE", mon(9, 0)) and not w.market_open("SAP.DE", mon(17, 30))
    assert not w.market_open("AAPL", mon(15, 29)) and w.market_open("AAPL", mon(15, 30))
    assert w.market_open("AAPL", mon(21, 59)) and not w.market_open("AAPL", mon(22, 0))
    assert not w.market_open("AAPL", dt.datetime(2026, 11, 1, 16, 0, tzinfo=R))  # Sunday

def test_positions_link():
    t = dict(id="1", pair="POSEUR", entry="20.0", stop="19.2", notes="")      # R = 0.8 (fictional numbers)
    assert w.eval_position(t, 20.5) == []
    assert w.eval_position(t, 21.25)[0][0] == "reached_1.5R"
    r = w.eval_position(t, 21.7); assert r[0][0] == "trail_start_2R" and abs(r[0][2]["level_2R"] - 21.6) < 1e-9
    assert [x[0] for x in w.eval_position(t, 19.3)] == ["near_stop"]
    t2 = dict(t, stop="19.6", notes="stop 19.2->19.6 11-02 10:00")   # R from initial stop
    assert abs(w.eval_position(t2, 21.3)[0][2]["R"] - 0.8) < 1e-9

def test_candidate_rules():
    base = dict(price=100, sma20=99, sma50=90, sma20_gt_sma50=True, sma20_rising=True, mom20_pct=2.0,
                mom126_pct=5, liquid=True, candidate=False)
    assert w.eval_candidate(dict(base, candidate=True), "crypto")[0] == "candidate"
    assert w.eval_candidate(dict(base, price=99.2, sma20=100), "crypto")[0] == "near"       # 0.8% below
    assert w.eval_candidate(dict(base, price=98, sma20=100), "crypto")[0] is None           # 2% below
    assert w.eval_candidate(dict(base, price=99.5, sma20=100, liquid=False), "crypto")[0] is None
    assert w.eval_candidate(dict(base, mom20_pct=-0.5), "stocks")[0] == "near"
    assert w.eval_candidate(dict(base, price=99.5, sma20=100, mom20_pct=-0.5), "stocks")[0] is None

def test_transition_only():
    s = st(); rows = [dict(pair="SOLEUR", candidate=True, price=1)]
    off = [dict(pair="SOLEUR", candidate=False, price=1, sma20=2, mom20_pct=-5, sma20_gt_sma50=False)]
    assert w.candidate_flags(rows, "crypto", s, "kraken") == []          # first sighting: record only
    assert w.candidate_flags(rows, "crypto", s, "kraken") == []          # still candidate -> no new flag
    assert w.candidate_flags(off, "crypto", s, "kraken") == []
    assert len(w.candidate_flags(rows, "crypto", s, "kraken")) == 1      # off -> candidate = flag
    s2 = st(); s2["_show_all"] = True                                    # dry-run shows current state
    assert len(w.candidate_flags(rows, "crypto", s2, "kraken")) == 1

def test_auth_header():
    os.environ["WATCH_WEBHOOK_URL"] = "u"
    for raw, exp in [("Authorization: Bearer abc", "Bearer abc"), ("Bearer abc", "Bearer abc"), ("abc123", "abc123")]:
        os.environ["WATCH_WEBHOOK_AUTH"] = raw; assert w.webhook_cfg()[1] == exp
    os.environ.pop("WATCH_WEBHOOK_AUTH"); os.environ.pop("WATCH_WEBHOOK_URL")

def _reset_pending():
    if os.path.exists(w.PENDING): os.remove(w.PENDING)

def test_near_stop_ignores_cap():
    s = st(); _reset_pending(); calls = []
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        w.handle_flags([fl(sym=f"S{i}") for i in range(6)], s, QUIET, poster=poster_ok(calls))
        r = w.handle_flags([fl(sym="POSEUR", reason="near_stop"), fl(sym="X")], s, QUIET, poster=poster_ok(calls))
        again = w.handle_flags([fl(sym="POSEUR", reason="near_stop")], s, QUIET + dt.timedelta(hours=1), poster=poster_ok(calls))
        later = w.handle_flags([fl(sym="POSEUR", reason="near_stop")], s, QUIET + dt.timedelta(hours=6, minutes=1), poster=poster_ok(calls))
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    assert [x["action"] for x in r] == ["wake", "cap"]          # near_stop wakes past the cap, normal flag capped
    assert again == [] and later[0]["action"] == "wake"          # cooldown still applies to near_stop
    assert w.wakes_today(s, QUIET) == 8 and len(calls) == 8

def test_pending_written_and_deduped():
    s = st(); _reset_pending()
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        for i in range(6): w.record_wake(s, fl(sym=f"S{i}"), QUIET)                 # cap used up
        w.handle_flags([fl(sym="AAA"), fl(sym="BBB", acct="t212")], s, QUIET, poster=lambda p: 200)
        w.handle_flags([fl(sym="AAA")], s, QUIET + dt.timedelta(hours=6, minutes=1), poster=lambda p: 200)  # after cooldown
        t = dt.datetime(2026, 11, 1, 2, 40, tzinfo=R)                                 # kraken session due
        w.handle_flags([fl(sym="CCC", reason="near_candidate")], st(), t, poster=lambda p: 200)
        w.handle_flags([fl(sym="DDD")], st(), QUIET, poster=lambda p: 200)           # woken -> not pending
    finally:
        os.environ.pop("WATCH_WEBHOOK_URL")
    items = w.read_pending()
    keys = [i["key"] for i in items]
    assert sorted(keys) == ["kraken:AAA:candidate", "kraken:CCC:near_candidate", "t212:BBB:candidate"]
    aaa = next(i for i in items if i["symbol"] == "AAA")
    assert aaa["count"] == 2 and aaa["first_ts"] < aaa["ts"] and aaa["suppressed_by"] == "cap"
    assert next(i for i in items if i["symbol"] == "CCC")["suppressed_by"] == "session_due"
    # CLI: clear only kraken
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): pcli.main(["list", "--account", "kraken"])
    assert "AAA" in buf.getvalue() and "BBB" not in buf.getvalue()
    with contextlib.redirect_stdout(io.StringIO()): pcli.main(["clear", "--account", "kraken"])
    assert [i["key"] for i in w.read_pending()] == ["t212:BBB:candidate"]

def test_market_hours_eu_exchanges():
    mon = lambda h, m: dt.datetime(2026, 11, 2, h, m, tzinfo=R)
    for sym in ("ENI.MI", "MC.PA", "INGA.AS", "SIE.DE"):
        assert not w.market_open(sym, mon(8, 59)) and w.market_open(sym, mon(9, 0))
        assert w.market_open(sym, mon(17, 29)) and not w.market_open(sym, mon(17, 30))
    assert w.market_open("BRK-B", mon(16, 0)) and not w.market_open("BRK-B", mon(10, 0))

def test_universe_watchlist_and_hours():
    syms = w.stock_symbols()
    assert 200 <= len(syms) <= 300 and len(set(syms)) == len(syms), len(syms)
    mon = lambda h, m: dt.datetime(2026, 11, 2, h, m, tzinfo=R)
    for s in syms:   # every universe symbol trades in exactly one of the two windows
        assert w.market_open(s, mon(12, 0)) != w.market_open(s, mon(20, 0)), s
    for s in ("NOKIA.HE", "ABI.BR", "SAN.MC", "VWCE.DE"):
        assert s in syms and w.is_eu(s)

def test_cloud_watcher_flag_makes_box_log_only():
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    try:
        os.environ["CLOUD_WATCHER"] = "1"
        assert w.webhook_cfg()[0] == ""
        r = w.handle_flags([fl(sym="ZZZ")], st(), QUIET, poster=lambda p: (_ for _ in ()).throw(AssertionError("posted")))
        assert r[0]["action"] == "not_configured"
        os.environ["WATCH_CLOUD_HEALTHCHECK"] = "1"   # stale/unreachable cloud -> box falls back to sending
        w._cloud_hc.update(ts=time.time(), ok=False, ok_ts=0.0)
        assert w.webhook_cfg()[0] != ""
        w._cloud_hc.update(ok=True)
        assert w.webhook_cfg()[0] == ""
        os.environ["WATCH_CLOUD_HEALTHCHECK"] = "0"
        w.CLOUD = True                      # inside the cloud run the flag is ignored
        assert w.webhook_cfg()[0] != ""
    finally:
        w.CLOUD = False; os.environ["CLOUD_WATCHER"] = "0"; os.environ.pop("WATCH_WEBHOOK_URL")

def test_positions_from_env():
    raw = '[{"id":"1","account":"kraken","pair":"POSEUR","entry":20.0,"stop":19.2,"initial_stop":19.2,"qty":3}]'
    ps = w.positions_from_env(raw)
    assert ps[0]["pair"] == "POSEUR" and w.initial_stop_of(ps[0]) == 19.2
    assert abs(w.eval_position(ps[0], 21.7)[0][2]["level_2R"] - 21.6) < 1e-9
    assert w.positions_from_env("not json") == [] and w.positions_from_env("") == []
    assert w.positions_from_env('{"positions": []}') == []
    t = dict(id="1", pair="POSEUR", entry="20.0", stop="19.6", initial_stop="19.2", notes="")
    assert abs(w.eval_position(t, 21.3)[0][2]["R"] - 0.8) < 1e-9

def test_cloud_pending_rides_along_and_batches():
    s = st(); _reset_pending(); calls = []
    os.environ["WATCH_WEBHOOK_URL"] = "http://invalid.example/never"
    w.CLOUD = True
    try:
        for i in range(6): w.record_wake(s, fl(sym=f"S{i}"), QUIET)                     # cap used up
        r = w.handle_flags([fl(sym="AAA"), fl(sym="TTT", acct="t212")], s, QUIET, poster=poster_ok(calls))
        assert [x["action"] for x in r] == ["cap", "cap"] and calls == []
        assert w.send_pending_batches(s, QUIET, poster=poster_ok(calls)) == [] and calls == []   # cap still full
        # near_stop wakes past the cap and carries the kraken pending item (not the t212 one)
        r = w.handle_flags([fl(sym="POSEUR", reason="near_stop")], s, QUIET, poster=poster_ok(calls))
        assert r[0]["action"] == "wake" and [i["symbol"] for i in calls[-1]["pending"]] == ["AAA"]
        assert [i["key"] for i in w.read_pending()] == ["t212:TTT:candidate"]
        # next Rome day, Monday 00:30 (no T212 session due): one batch wake for t212
        mon = dt.datetime(2026, 11, 2, 0, 30, tzinfo=R)
        b = w.send_pending_batches(s, mon, poster=poster_ok(calls))
        assert b[0]["account"] == "t212" and calls[-1]["reason"] == "pending_batch" and w.read_pending() == []
        assert w.wakes_today(s, mon) == 1
        # stale items expire
        w.add_pending(dict(account="kraken", symbol="OLD", reason="candidate", action="cap", metrics={}, ts=QUIET.isoformat()))
        assert w.expire_pending(QUIET + dt.timedelta(hours=25)) == []
    finally:
        w.CLOUD = False; os.environ.pop("WATCH_WEBHOOK_URL")

def test_cloud_log_redacts_positions():
    import logging, io
    buf = io.StringIO(); h = logging.StreamHandler(buf); w.log.addHandler(h); w.log.setLevel(logging.INFO)
    w.CLOUD = True
    try:
        f = dict(fl(sym="POSEUR", reason="near_stop"), detail="price 19.3 within 1% of stop 19.25")
        w.handle_flags([f], st(), QUIET, dry_run=True)
    finally:
        w.CLOUD = False; w.log.removeHandler(h)
    out = buf.getvalue()
    assert "position check" in out and "POSEUR" not in out and "19.25" not in out

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("ok", k)
    print(f"{n} tests passed")
