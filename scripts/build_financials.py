"""Build financials JSON for a list of tickers from SEC EDGAR.

Usage:
  python scripts/build_financials.py --universe data/universe.json --out data/financials.json
  python scripts/build_financials.py --sp500 data/sp500.json --out data/sp500_financials.json --slim

Needs the SEC_USER_AGENT environment variable. Safe to re-run any time: it rebuilds from the
SEC's current data, so a newly filed 10-Q or 10-K shows up on the next run.
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import sec_facts  # noqa: E402


def load_tickers(args):
    if args.universe:
        with open(args.universe, encoding="utf-8") as f:
            return json.load(f)["companies"]
    with open(args.sp500, encoding="utf-8") as f:
        return json.load(f)["companies"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe")
    ap.add_argument("--sp500")
    ap.add_argument("--out", required=True)
    ap.add_argument("--slim", action="store_true", help="skip per-quarter rows (smaller file)")
    args = ap.parse_args()
    if not (args.universe or args.sp500):
        ap.error("give --universe or --sp500")

    companies = load_tickers(args)
    tmap = sec_facts.ticker_map()
    out, problems = {}, []
    for c in companies:
        t = c["ticker"].upper().replace(".", "-")
        info = tmap.get(t)
        if not info:
            problems.append(f"{t}: not found in SEC ticker list")
            continue
        try:
            facts, subs = sec_facts.fetch_company(info["cik"])
            if not facts:
                problems.append(f"{t}: no XBRL facts")
                continue
            rec = sec_facts.build_record(facts, subs, ticker=t, name=c.get("name") or info["name"], slim=args.slim)
            for k in ("bucket", "kidNote", "needsConfirm"):
                if k in c:
                    rec[k] = c[k]
            missing = [k for k in ("ttm", "shares") if not rec.get(k)]
            if missing:
                problems.append(f"{t}: missing {', '.join(missing)}")
            out[t] = rec
        except Exception as e:  # one bad company must not sink the whole run
            problems.append(f"{t}: {type(e).__name__}: {e}")

    payload = {"generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "source": "SEC EDGAR companyfacts", "problems": problems, "companies": out}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"wrote {len(out)} companies to {args.out}; {len(problems)} problem(s)")
    for p in problems:
        print("  -", p)


if __name__ == "__main__":
    main()
