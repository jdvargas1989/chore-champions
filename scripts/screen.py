"""Find "diamonds in the rough": companies that look cheap for what they earn.

Educational only. A cheap-looking stock can be cheap for a good reason, so every pick is
shown with reasons it might be cheap (the "is the market wrong, or right?" lesson).

How it works (all numbers come from SEC filings plus the quarter-end share price):
  market cap = price x shares          EV = market cap + debt - cash
  Metrics, each ranked against companies in the SAME sector:
    EV / operating profit      (lower is cheaper)      weight 1.0
    free cash flow yield       (higher is cheaper)     weight 1.0
    price / sales              (lower is cheaper)      weight 0.75
    net debt / operating profit(lower is safer)        weight 0.75
    net current assets / mkt cap (Graham's idea)       weight 0.5
  Score = weighted average percentile (0 = priciest, 100 = cheapest looking).
  Filters: profitable, positive free cash flow, market cap >= $2B, not a bank/insurer/REIT
  (those need different measures; v1 leaves them out).

Usage:
  python scripts/screen.py --financials data/sp500_financials.json --asof 2026-09-30 --out data/diamonds.json
"""
import argparse
import datetime as dt
import json
import os
import statistics

MIN_MARKET_CAP = 2e9
TOP_N = 10
WEIGHTS = {"evEbit": 1.0, "fcfYield": 1.0, "ps": 0.75, "netDebtEbit": 0.75, "ncavMc": 0.5}
LOWER_IS_BETTER = {"evEbit", "ps", "netDebtEbit"}


def sector_of(sic):
    """Coarse sector from the SEC's SIC code. Approximate on purpose."""
    if sic is None:
        return "Other"
    maj = sic // 100
    if 6000 <= sic <= 6799:
        return "Financials"
    if 2830 <= sic <= 2839 or sic in (3841, 3842, 3844, 3845, 8000, 8050, 8060, 8062, 8071, 8082, 8090):
        return "Health Care"
    if maj in (13, 29):
        return "Energy"
    if maj == 49:
        return "Utilities"
    if maj in (48, 27):
        return "Communication"
    if maj in (10, 12, 14, 24, 26, 28, 32, 33):
        return "Materials"
    if maj in (20, 21, 54, 52) or sic in (2840, 2844):
        return "Consumer Staples"
    if maj in (22, 23, 25, 30, 31, 36, 37, 39, 53, 55, 56, 57, 58, 59, 70, 78, 79) and maj != 36:
        return "Consumer Discretionary"
    if maj in (73, 35, 36, 38, 87):
        return "Technology & Services"
    if maj in (15, 16, 17, 34, 40, 41, 42, 44, 45, 47, 50, 51, 75, 76):
        return "Industrials"
    return "Other"


def metrics_for(rec, price):
    """Compute valuation metrics, or None if the filing data can't support them."""
    ttm, bs, shares = rec.get("ttm"), rec.get("balance") or {}, rec.get("shares")
    if not ttm or not shares or not price:
        return None
    rev, op, fcf = ttm.get("revenue"), ttm.get("opIncome"), ttm.get("fcf")
    if None in (rev, op, fcf) or rev <= 0 or op <= 0 or fcf <= 0:
        return None
    mc = price * shares
    if mc < MIN_MARKET_CAP:
        return None
    debt, cash = bs.get("debt") or 0, bs.get("cash") or 0
    ev = mc + debt - cash
    ncav = None
    if bs.get("assetsCurrent") is not None and bs.get("liabilities") is not None:
        ncav = bs["assetsCurrent"] - bs["liabilities"]
    prior = rec.get("ttmPrior")
    growth = (rev / prior["revenue"] - 1) if prior and prior.get("revenue") else None
    margin = op / rev
    margin_prior = (prior["opIncome"] / prior["revenue"]) if prior and prior.get("revenue") and prior.get("opIncome") is not None else None
    return {
        "price": round(price, 2), "marketCap": mc, "ev": ev,
        "evEbit": ev / op, "fcfYield": fcf / mc, "ps": mc / rev,
        "netDebtEbit": (debt - cash) / op, "ncavMc": (ncav / mc) if ncav is not None else None,
        "revenueGrowth": growth, "opMargin": margin,
        "opMarginChange": (margin - margin_prior) if margin_prior is not None else None,
        "ocfVsNet": (ttm["ocf"] / ttm["netIncome"]) if ttm.get("ocf") and ttm.get("netIncome") and ttm["netIncome"] > 0 else None,
    }


def percentile_ranks(values, lower_better):
    """values: {ticker: x}. Returns {ticker: 0..100}, 100 = cheapest-looking."""
    items = sorted(values.items(), key=lambda kv: kv[1], reverse=lower_better)
    n = len(items)
    if n == 1:
        return {items[0][0]: 50.0}
    return {t: 100.0 * i / (n - 1) for i, (t, _) in enumerate(items)}


def why_cheap(m, sector_median_ev_ebit):
    flags = []
    if m["revenueGrowth"] is not None and m["revenueGrowth"] < 0:
        flags.append(f"Sales shrank {abs(m['revenueGrowth']) * 100:.0f}% over the past year")
    if m["opMarginChange"] is not None and m["opMarginChange"] <= -0.02:
        flags.append(f"Profit margin fell {abs(m['opMarginChange']) * 100:.1f} points")
    if m["netDebtEbit"] > 3:
        flags.append(f"Heavy debt ({m['netDebtEbit']:.1f}x a year of operating profit)")
    if m["ocfVsNet"] is not None and m["ocfVsNet"] < 0.8:
        flags.append("Reported profit is higher than the cash it brings in")
    if sector_median_ev_ebit and m["evEbit"] < 0.8 * sector_median_ev_ebit:
        flags.append("The whole sector looks out of favor right now")
    if not flags:
        flags.append("No obvious red flag in the numbers. Ask what the market might know that the numbers don't")
    return flags


def run_screen(records, prices, asof):
    """records: {ticker: slim record}; prices: {ticker: {'close': x} or None}."""
    rows = {}
    for t, rec in records.items():
        sector = sector_of(rec.get("sic"))
        if sector == "Financials":
            continue
        p = prices.get(t)
        m = metrics_for(rec, p["close"] if p else None)
        if m:
            rows[t] = {"ticker": t, "name": rec.get("name"), "sector": sector, "metrics": m}

    by_sector = {}
    for r in rows.values():
        by_sector.setdefault(r["sector"], []).append(r["ticker"])
    for sector, tickers in by_sector.items():
        # Rank inside the sector when it's big enough; otherwise against everyone.
        group = tickers if len(tickers) >= 8 else list(rows)
        score_parts = {t: [] for t in tickers}
        for key, w in WEIGHTS.items():
            vals = {t: rows[t]["metrics"][key] for t in group if rows[t]["metrics"][key] is not None}
            if len(vals) < 3:
                continue
            pr = percentile_ranks(vals, key in LOWER_IS_BETTER)
            for t in tickers:
                if t in pr:
                    score_parts[t].append((pr[t], w))
        for t in tickers:
            parts = score_parts[t]
            wsum = sum(w for _, w in parts)
            rows[t]["score"] = round(sum(s * w for s, w in parts) / wsum, 1) if len(parts) >= 3 else None

    ev_by_sector = {s: statistics.median(rows[t]["metrics"]["evEbit"] for t in ts) for s, ts in by_sector.items()}
    ranked = sorted((r for r in rows.values() if r["score"] is not None), key=lambda r: -r["score"])[:TOP_N]
    picks = []
    for i, r in enumerate(ranked, 1):
        m = r["metrics"]
        picks.append({
            "rank": i, "ticker": r["ticker"], "name": r["name"], "sector": r["sector"], "score": r["score"],
            "price": m["price"], "marketCap": round(m["marketCap"]), "ev": round(m["ev"]),
            "evEbit": round(m["evEbit"], 1), "fcfYieldPct": round(m["fcfYield"] * 100, 1),
            "priceToSales": round(m["ps"], 2), "netDebtToEbit": round(m["netDebtEbit"], 1),
            "revenueGrowthPct": None if m["revenueGrowth"] is None else round(m["revenueGrowth"] * 100, 1),
            "opMarginPct": round(m["opMargin"] * 100, 1),
            "whyMightBeCheap": why_cheap(m, ev_by_sector.get(r["sector"])),
        })
    return {"asOf": asof, "screened": len(rows), "method": "sector-relative percentile of EV/EBIT, FCF yield, P/S, net debt/EBIT, NCAV/market cap",
            "disclaimer": "Practice lesson only, not investment advice. Cheap-looking can be cheap for a reason.",
            "picks": picks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--financials", required=True)
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD (must match prices fetched for that date)")
    ap.add_argument("--out", default="data/diamonds.json")
    ap.add_argument("--cache", default="data/price_cache.json")
    args = ap.parse_args()
    with open(args.financials, encoding="utf-8") as f:
        records = json.load(f)["companies"]
    cache = json.load(open(args.cache, encoding="utf-8")) if os.path.exists(args.cache) else {}
    prices = cache.get(args.asof, {})
    have = sum(1 for t in records if prices.get(t))
    print(f"{have}/{len(records)} companies have a price for {args.asof}")
    if have < 0.8 * len(records):
        print("Not enough prices yet. Re-run the price step (it resumes), then screen again.")
        raise SystemExit(0)
    result = run_screen(records, prices, args.asof)
    result["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    print(f"wrote {len(result['picks'])} picks (from {result['screened']} screened) to {args.out}")


if __name__ == "__main__":
    main()
