"""Quarterly financials from SEC EDGAR "companyfacts" (free, no API key).

The SEC publishes every XBRL figure a company has reported. This module turns that
raw feed into clean quarterly rows plus trailing-twelve-month (TTM) totals.

Key idea: 10-Qs report Q1, Q2, Q3 (income items) and year-to-date totals (cash-flow
items), and the 10-K reports the full year. Q4 is never reported on its own, so we
derive it: Q4 = full year - nine months. Same trick for Q2/Q3 when only YTD exists.

Set SEC_USER_AGENT (e.g. "AppName contact@example.com"). The SEC asks automated
callers to identify themselves.
"""
import datetime as dt
import json
import os
import time

# ---- which XBRL tags mean what (first tag in each list has priority) ----------------
REVENUE = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
           "SalesRevenueGoodsNet"]
COGS = ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]
OPEX = ["OperatingExpenses"]
OPINC = ["OperatingIncomeLoss"]
NETINC = ["NetIncomeLoss", "ProfitLoss"]
OCF = ["NetCashProvidedByUsedInOperatingActivities"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"]
DA = ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
      "DepreciationAmortizationAndAccretionNet"]
SHARES_DILUTED = ["WeightedAverageNumberOfDilutedSharesOutstanding"]

ASSETS_CUR = ["AssetsCurrent"]
LIABS = ["Liabilities"]
LIABS_AND_EQ = ["LiabilitiesAndStockholdersEquity"]
EQUITY = ["StockholdersEquity",
          "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
CASH = ["CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
ST_INVEST = ["MarketableSecuritiesCurrent", "ShortTermInvestments"]
DEBT_LONG = ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"]
DEBT_SHORT = ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings"]

_last_call = 0.0


# ---- network (only used in the scheduled job; tests never touch it) ----------------
def _get_json(url, retries=3):
    import requests
    global _last_call
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError("Set SEC_USER_AGENT, e.g. 'ChoreChampions you@example.com'")
    for attempt in range(retries):
        wait = 0.15 - (time.time() - _last_call)  # stay well under 10 requests/second
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 503):
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
    raise RuntimeError(f"SEC request kept failing: {url}")


def ticker_map():
    """ticker -> {cik, name}. SEC writes share classes with a dash (BRK-B)."""
    raw = _get_json("https://www.sec.gov/files/company_tickers.json")
    return {v["ticker"].upper(): {"cik": int(v["cik_str"]), "name": v["title"]} for v in raw.values()}


def fetch_company(cik):
    cik10 = f"{int(cik):010d}"
    facts = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json")
    subs = _get_json(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    return facts, subs


# ---- pure logic (unit-tested) -------------------------------------------------------
def _d(s):
    return dt.date.fromisoformat(s)


def _days(f):
    return (_d(f["end"]) - _d(f["start"])).days


def _kind(days):
    if 80 <= days <= 105:
        return "Q"
    if 170 <= days <= 200:
        return "H1"
    if 260 <= days <= 290:
        return "9M"
    if 350 <= days <= 380:
        return "FY"
    return None


def _unit_facts(facts, tag, unit="USD", taxonomy="us-gaap"):
    node = facts.get("facts", {}).get(taxonomy, {}).get(tag)
    if not node:
        return []
    return node.get("units", {}).get(unit, [])


def quarterly_series(facts, tags, unit="USD"):
    """Return {quarter_end_iso: value} for a flow metric (revenue, cash flow, ...)."""
    out = {}
    for tag in tags:  # earlier tags win; later tags only fill gaps
        rows = [f for f in _unit_facts(facts, tag, unit) if "start" in f and "end" in f]
        latest = {}
        for f in rows:  # restated numbers: the most recently filed one wins
            key = (f["start"], f["end"])
            if key not in latest or f.get("filed", "") > latest[key].get("filed", ""):
                latest[key] = f
        by_start = {}
        direct = {}
        direct_rows = []  # (start, end, value) of true 3-month facts
        for (s, e), f in latest.items():
            k = _kind(_days(f))
            if k == "Q":
                direct[e] = f["val"]
                direct_rows.append((s, e, f["val"]))
            if k:
                by_start.setdefault(s, {})[k] = f
        derived = dict(direct)
        for s, parts in by_start.items():
            q1 = parts.get("Q")  # a 3-month fact starting exactly at fiscal-year start = Q1
            h1, nine, fy = parts.get("H1"), parts.get("9M"), parts.get("FY")
            if q1 and h1 and h1["end"] not in derived:
                derived[h1["end"]] = h1["val"] - q1["val"]
            if h1 and nine and nine["end"] not in derived:
                derived[nine["end"]] = nine["val"] - h1["val"]
            if nine and fy and fy["end"] not in derived:
                derived[fy["end"]] = fy["val"] - nine["val"]
            elif fy and not nine and fy["end"] not in derived:
                # No nine-month total: subtract the three quarters we do have inside this year.
                inside = [v for (qs, qe, v) in direct_rows if qs >= s and qe < fy["end"]]
                if len(inside) == 3:
                    derived[fy["end"]] = fy["val"] - sum(inside)
        for e, v in derived.items():
            out.setdefault(e, v)
    return out


def latest_period_value(facts, tag, unit="shares"):
    """Most recent 3-month (or, if newer, full-year) value. For averages like share counts,
    which must NOT be subtracted from year-to-date totals."""
    best = None
    for f in _unit_facts(facts, tag, unit):
        if "start" not in f or "end" not in f:
            continue
        k = _kind(_days(f))
        if k not in ("Q", "FY"):
            continue
        rank = (f["end"], 1 if k == "Q" else 0, f.get("filed", ""))
        if best is None or rank > best[0]:
            best = (rank, f["val"])
    return best[1] if best else None


def instant_latest(facts, tags, unit="USD", taxonomy="us-gaap", on_or_before=None):
    """Latest point-in-time value (balance sheet items). Returns (end, value) or None."""
    for tag in tags:
        best = None
        for f in _unit_facts(facts, tag, unit, taxonomy):
            if "start" in f:
                continue
            if on_or_before and f["end"] > on_or_before:
                continue
            if best is None or (f["end"], f.get("filed", "")) > (best["end"], best.get("filed", "")):
                best = f
        if best:
            return best["end"], best["val"]
    return None


def _round_ends(ends):
    return sorted(ends)


def build_record(facts, subs=None, ticker=None, name=None, slim=False):
    """Turn raw companyfacts into one tidy record."""
    rev = quarterly_series(facts, REVENUE)
    cogs = quarterly_series(facts, COGS)
    opex = quarterly_series(facts, OPEX)
    opinc = quarterly_series(facts, OPINC)
    net = quarterly_series(facts, NETINC)
    ocf = quarterly_series(facts, OCF)
    capex = quarterly_series(facts, CAPEX)
    da = quarterly_series(facts, DA)

    ends = _round_ends(rev.keys())[-9:]
    quarters = []
    for e in ends:
        r, c, o, oi = rev.get(e), cogs.get(e), opex.get(e), opinc.get(e)
        if o is None and r is not None and c is not None and oi is not None:
            o = r - c - oi  # operating expenses = gross profit - operating income
        quarters.append({
            "end": e, "revenue": r, "cogs": c,
            "grossProfit": (r - c) if (r is not None and c is not None) else None,
            "opex": o, "opIncome": oi, "netIncome": net.get(e),
            "ocf": ocf.get(e), "capex": capex.get(e), "da": da.get(e),
        })

    def ttm(rows):
        if len(rows) < 4:
            return None
        last4 = rows[-4:]
        span = (_d(last4[-1]["end"]) - _d(last4[0]["end"])).days
        if not (230 <= span <= 300):  # four consecutive quarters end ~9 months apart (first to last)
            return None

        def tot(k):
            vals = [q[k] for q in last4]
            return sum(vals) if all(v is not None for v in vals) else None
        out = {k: tot(k) for k in ("revenue", "cogs", "opIncome", "netIncome", "ocf", "capex", "da")}
        out["asOf"] = last4[-1]["end"]
        out["fcf"] = (out["ocf"] - abs(out["capex"] or 0)) if out["ocf"] is not None else None
        return out

    cur = ttm(quarters)
    prior = ttm(quarters[:-4]) if len(quarters) >= 8 else None

    as_of = quarters[-1]["end"] if quarters else None
    bs = {}
    if as_of:
        def inst(tags, unit="USD"):
            hit = instant_latest(facts, tags, unit, on_or_before=as_of)
            return hit[1] if hit else None
        eq = inst(EQUITY)
        liabs = inst(LIABS)
        if liabs is None:
            tle = inst(LIABS_AND_EQ)
            if tle is not None and eq is not None:
                liabs = tle - eq  # Liabilities = (Liabilities + Equity) - Equity
        debt_long_hit = None
        for t in DEBT_LONG:
            h = instant_latest(facts, [t], on_or_before=as_of)
            if h:
                debt_long_hit = (t, h[1])
                break
        debt_short = inst(DEBT_SHORT)
        debt = None
        if debt_long_hit:
            # "LongTermDebt" already includes the current portion; the Noncurrent tag does not.
            debt = debt_long_hit[1] + (debt_short or 0 if debt_long_hit[0] == "LongTermDebtNoncurrent" else 0)
        elif debt_short is not None:
            debt = debt_short
        bs = {
            "asOf": as_of, "assetsCurrent": inst(ASSETS_CUR), "liabilities": liabs, "equity": eq,
            "cash": (inst(CASH) or 0) + (inst(ST_INVEST) or 0) if inst(CASH) is not None else None,
            "debt": debt,
        }

    share_count = None
    for tag in SHARES_DILUTED:
        share_count = latest_period_value(facts, tag, "shares")
        if share_count:
            break
    sic = subs.get("sic") if subs else None
    record = {
        "ticker": ticker, "name": name or (subs or {}).get("name") or facts.get("entityName"),
        "cik": facts.get("cik"), "sic": int(sic) if sic not in (None, "") else None,
        "sicDescription": (subs or {}).get("sicDescription"),
        "shares": share_count, "balance": bs, "ttm": cur, "ttmPrior": prior,
        "latestQuarterEnd": as_of,
    }
    if not slim:
        record["quarters"] = quarters
    return record
