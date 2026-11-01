#!/usr/bin/env python3
"""pending.py - review / clear watcher flags that were NOT woken (daily cap or scheduled session due).
  python3 watch/pending.py list  --account kraken|t212
  python3 watch/pending.py clear --account kraken|t212 [--symbol LINKEUR] [--reason candidate]
Run `list` at the start of each scheduled session, act on what matters, then `clear`."""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import watcher as w

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["list", "clear"])
    ap.add_argument("--account", choices=["kraken", "t212"], required=True)
    ap.add_argument("--symbol"); ap.add_argument("--reason")
    ap.add_argument("--file", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    items = w.read_pending(a.file)
    match = lambda i: (i.get("account") == a.account and (not a.symbol or i.get("symbol", "").upper() == a.symbol.upper())
                       and (not a.reason or i.get("reason") == a.reason))
    sel = [i for i in items if match(i)]
    if a.action == "list":
        for i in sel:
            m = i.get("metrics", {})
            print(f"# {i['ts']} {i['symbol']} {i['reason']} ({i.get('suppressed_by')}, x{i.get('count', 1)}): {m.get('detail', '')}")
        print(json.dumps(dict(account=a.account, pending=sel), default=str))
    else:
        w.write_pending([i for i in items if not match(i)], a.file)
        print(f"# cleared {len(sel)} pending item(s) for {a.account}")
        print(json.dumps(dict(account=a.account, cleared=len(sel))))
    return 0

if __name__ == "__main__":
    sys.exit(main())
