"""Share prices for the weekly snapshot and for quarter-end screens.

Two ways to get prices, behind one tiny interface:
  * TwelveData  - free-tier API (needs TWELVE_DATA_API_KEY). Check its current terms and limits.
  * ManualCSV   - a file you fill in by hand: data/prices_manual.csv  (date,ticker,close)

Everything fetched is stored in data/price_cache.json, so a run that hits the daily rate limit
simply stops, and the next run picks up where it left off.

Commands:
  python scripts/prices.py snapshot                      # weekly: universe + SPY/GLD/USO
  python scripts/prices.py asof 2026-09-30 --tickers-file data/sp500.json --max-calls 700
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time

CACHE_PATH = "data/price_cache.json"
BENCHMARKS = {"SPY": "S&P 500 (fund)", "GLD": "Gold (fund)", "USO": "Oil (fund)"}


class RateLimited(Exception):
    pass


class TwelveData:
    """https://twelvedata.com  - free tier is small, so calls are throttled."""
    name = "twelvedata"

    def __init__(self, api_key, calls_per_minute=7):
        self.key = api_key
        self.gap = 60.0 / calls_per_minute
        self._last = 0.0

    def close_on_or_before(self, ticker, date):
        import requests
        wait = self.gap - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        start = (dt.date.fromisoformat(date) - dt.timedelta(days=10)).isoformat()
        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": ticker.replace("-", "."), "interval": "1day", "start_date": start,
            "end_date": date, "order": "DESC", "outputsize": 10, "apikey": self.key}, timeout=30)
        body = r.json()
        if body.get("status") == "error":
            if body.get("code") == 429:
                raise RateLimited(body.get("message", "rate limited"))
            return None  # symbol unknown, no data, etc.
        values = body.get("values") or []
        if not values:
            return None
        return values[0]["datetime"][:10], float(values[0]["close"])


class ManualCSV:
    """Reads data/prices_manual.csv with columns: date,ticker,close"""
    name = "manual"

    def __init__(self, path="data/prices_manual.csv"):
        self.rows = {}
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.rows.setdefault(r["ticker"].upper(), {})[r["date"]] = float(r["close"])

    def close_on_or_before(self, ticker, date):
        days = {d: c for d, c in self.rows.get(ticker.upper(), {}).items() if d <= date}
        if not days:
            return None
        d = max(days)
        return d, days[d]


def make_provider():
    key = os.environ.get("TWELVE_DATA_API_KEY")
    return TwelveData(key) if key else ManualCSV()


def load_cache(path=CACHE_PATH):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)


def fetch_prices(tickers, asof, provider, cache, max_calls=700):
    """Fill cache[asof][ticker] = {date, close} (or None if the provider has nothing).
    Stops cleanly at the rate limit or max_calls. Returns (fetched, remaining)."""
    day = cache.setdefault(asof, {})
    todo = [t for t in tickers if t not in day]
    fetched = 0
    for t in todo:
        if fetched >= max_calls:
            break
        try:
            hit = provider.close_on_or_before(t, asof)
        except RateLimited as e:
            print(f"rate limit reached: {e}")
            break
        day[t] = {"date": hit[0], "close": hit[1]} if hit else None
        fetched += 1
    return fetched, len([t for t in tickers if t not in day])


def last_friday(today=None):
    today = today or dt.date.today()
    return today - dt.timedelta(days=(today.weekday() - 4) % 7)


def last_quarter_end(today=None):
    today = today or dt.date.today()
    q_end_month = ((today.month - 1) // 3) * 3  # month number of the previous quarter end (0 = Dec prior year)
    if q_end_month == 0:
        return dt.date(today.year - 1, 12, 31)
    nxt = dt.date(today.year, q_end_month + 1, 1)
    return nxt - dt.timedelta(days=1)


def tickers_from(path):
    with open(path, encoding="utf-8") as f:
        return [c["ticker"].upper() for c in json.load(f)["companies"]]


def cmd_snapshot(args):
    provider = make_provider()
    cache = load_cache()
    fri = last_friday().isoformat()
    prior = (dt.date.fromisoformat(fri) - dt.timedelta(days=7)).isoformat()
    tickers = tickers_from("data/universe.json") + list(BENCHMARKS)
    for asof in (fri, prior):
        n, left = fetch_prices(tickers, asof, provider, cache, max_calls=200)
        print(f"{asof}: fetched {n}, {left} left")
    save_cache(cache)

    def px(day, t):
        v = cache.get(day, {}).get(t)
        return v["close"] if v else None
    rows = {}
    for t in tickers:
        now, before = px(fri, t), px(prior, t)
        rows[t] = {"close": now, "weekAgo": before,
                   "weekChangePct": round((now / before - 1) * 100, 2) if now and before else None}
    out = {"asOf": fri, "source": provider.name, "benchmarks": BENCHMARKS, "prices": rows}
    with open("data/market_weekly.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    hist_path = "data/market_history.json"
    hist = json.load(open(hist_path, encoding="utf-8")) if os.path.exists(hist_path) else []
    hist = [h for h in hist if h["asOf"] != fri] + [{"asOf": fri, "closes": {t: r["close"] for t, r in rows.items()}}]
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(hist[-260:], f, separators=(",", ":"))  # about five years of weekly history
    print("wrote data/market_weekly.json")


def cmd_asof(args):
    provider = make_provider()
    cache = load_cache()
    asof = args.date if args.date != "auto" else last_quarter_end().isoformat()
    tickers = tickers_from(args.tickers_file)
    n, left = fetch_prices(tickers, asof, provider, cache, max_calls=args.max_calls)
    save_cache(cache)
    print(f"{asof}: fetched {n}, {left} still to fetch (re-run later to continue)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    a = sub.add_parser("asof")
    a.add_argument("date", help="YYYY-MM-DD or 'auto' for the last quarter end")
    a.add_argument("--tickers-file", required=True)
    a.add_argument("--max-calls", type=int, default=700)
    args = ap.parse_args()
    {"snapshot": cmd_snapshot, "asof": cmd_asof}[args.cmd](args)


if __name__ == "__main__":
    main()
