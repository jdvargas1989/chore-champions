import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sec_facts as S
import screen as SC
import prices as P


def fact(start, end, val, filed="2025-01-01"):
    d = {"end": end, "val": val, "filed": filed}
    if start:
        d["start"] = start
    return d


def facts_of(**tags):
    return {"cik": 1, "facts": {"us-gaap": {t: {"units": {"USD": v}} for t, v in tags.items()}}}


class SecTests(unittest.TestCase):
    def test_q2_q3_q4_derivation(self):
        f = facts_of(Revenues=[
            fact("2024-01-01", "2024-03-31", 100),
            fact("2024-01-01", "2024-06-30", 230),   # H1 -> Q2 = 130
            fact("2024-01-01", "2024-09-30", 380),   # 9M -> Q3 = 150
            fact("2024-01-01", "2024-12-31", 560),   # FY -> Q4 = 180
        ])
        q = S.quarterly_series(f, S.REVENUE)
        self.assertEqual(q, {"2024-03-31": 100, "2024-06-30": 130, "2024-09-30": 150, "2024-12-31": 180})

    def test_q4_fallback_without_nine_months(self):
        f = facts_of(Revenues=[
            fact("2024-01-01", "2024-03-31", 100), fact("2024-04-01", "2024-06-30", 130),
            fact("2024-07-01", "2024-09-30", 150), fact("2024-01-01", "2024-12-31", 560)])
        self.assertEqual(S.quarterly_series(f, S.REVENUE)["2024-12-31"], 180)

    def test_restatement_latest_filed_wins(self):
        f = facts_of(Revenues=[fact("2024-01-01", "2024-03-31", 100, "2024-05-01"),
                               fact("2024-01-01", "2024-03-31", 110, "2025-05-01")])
        self.assertEqual(S.quarterly_series(f, S.REVENUE)["2024-03-31"], 110)

    def _full(self):
        revs, ops, ocf, cap, net = [], [], [], [], []
        for y, base in ((2023, 100), (2024, 120)):
            qs = [("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30"), ("10-01", "12-31")]
            for i, (a, b) in enumerate(qs):
                for lst, m in ((revs, 1), (ops, 0.2), (ocf, 0.25), (cap, 0.05), (net, 0.15)):
                    lst.append(fact(f"{y}-{a}", f"{y}-{b}", base * m + i))
        return facts_of(Revenues=revs, OperatingIncomeLoss=ops, NetCashProvidedByUsedInOperatingActivities=ocf,
                        PaymentsToAcquirePropertyPlantAndEquipment=cap, NetIncomeLoss=net)

    def test_ttm_and_prior(self):
        f = self._full()
        f["facts"]["us-gaap"]["AssetsCurrent"] = {"units": {"USD": [fact(None, "2024-12-31", 500)]}}
        f["facts"]["us-gaap"]["Liabilities"] = {"units": {"USD": [fact(None, "2024-12-31", 300)]}}
        f["facts"]["dei"] = {}
        f["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": [fact("2024-10-01", "2024-12-31", 50), fact("2024-01-01", "2024-12-31", 48)]}}
        r = S.build_record(f, {"sic": "3571"}, "TST", "Test Co")
        self.assertEqual(r["ttm"]["revenue"], 4 * 120 + 6)
        self.assertEqual(r["ttmPrior"]["revenue"], 4 * 100 + 6)
        self.assertAlmostEqual(r["ttm"]["fcf"], (4 * 30 + 6) - (4 * 6 + 6))
        self.assertEqual(r["shares"], 50)  # latest quarter, not differenced
        self.assertEqual(r["balance"]["assetsCurrent"], 500)
        self.assertEqual(r["sic"], 3571)

    def test_ttm_rejects_gappy_quarters(self):
        f = facts_of(Revenues=[fact("2022-01-01", "2022-03-31", 1), fact("2023-01-01", "2023-03-31", 1),
                               fact("2024-01-01", "2024-03-31", 1), fact("2025-01-01", "2025-03-31", 1)])
        self.assertIsNone(S.build_record(f)["ttm"])


def rec(ticker, sic, rev, op, fcf, shares, debt=0, cash=0, ca=0, liab=0):
    return {"ticker": ticker, "name": ticker, "sic": sic, "shares": shares,
            "ttm": {"revenue": rev, "opIncome": op, "fcf": fcf, "ocf": fcf, "netIncome": op * 0.8},
            "ttmPrior": {"revenue": rev, "opIncome": op},
            "balance": {"debt": debt, "cash": cash, "assetsCurrent": ca, "liabilities": liab}}


class ScreenTests(unittest.TestCase):
    def test_cheap_company_ranks_first_and_exclusions(self):
        recs, prices = {}, {}
        for i in range(10):  # ten ordinary, fairly priced tech-ish companies
            t = f"N{i}"
            recs[t] = rec(t, 7372, 1e9, 2e8, 1.5e8, 1e8, ca=3e8, liab=2e8)
            prices[t] = {"close": 40 + i}   # market cap 4.0-4.9B
        recs["CHEAP"] = rec("CHEAP", 7372, 1e9, 2e8, 1.5e8, 1e8, ca=6e8, liab=1e8)
        prices["CHEAP"] = {"close": 12}      # market cap 1.2B -> below $2B floor, so excluded
        recs["CHEAP2"] = rec("CHEAP2", 7372, 1e9, 2e8, 1.5e8, 1e8, ca=6e8, liab=1e8)
        prices["CHEAP2"] = {"close": 21}     # 2.1B market cap, much cheaper than peers
        recs["BANK"] = rec("BANK", 6021, 1e9, 2e8, 1.5e8, 1e8)
        prices["BANK"] = {"close": 1}
        recs["BURN"] = rec("BURN", 7372, 1e9, 2e8, -1e7, 1e8)
        prices["BURN"] = {"close": 1}
        out = SC.run_screen(recs, prices, "2026-09-30")
        tickers = [p["ticker"] for p in out["picks"]]
        self.assertEqual(tickers[0], "CHEAP2")
        for bad in ("CHEAP", "BANK", "BURN"):
            self.assertNotIn(bad, tickers)
        self.assertTrue(out["picks"][0]["whyMightBeCheap"])

    def test_percentiles(self):
        r = SC.percentile_ranks({"a": 1, "b": 2, "c": 3}, lower_better=True)
        self.assertEqual((r["a"], r["c"]), (100.0, 0.0))


class PriceTests(unittest.TestCase):
    class Fake:
        name = "fake"
        def __init__(self, limit=None):
            self.n, self.limit = 0, limit
        def close_on_or_before(self, t, d):
            self.n += 1
            if self.limit and self.n > self.limit:
                raise P.RateLimited("x")
            return (d, 10.0) if t != "NONE" else None

    def test_resumable(self):
        cache = {}
        n, left = P.fetch_prices(["A", "B", "C"], "2026-09-30", self.Fake(limit=2), cache)
        self.assertEqual((n, left), (2, 1))
        n, left = P.fetch_prices(["A", "B", "C", "NONE"], "2026-09-30", self.Fake(), cache)
        self.assertEqual(left, 0)
        self.assertIsNone(cache["2026-09-30"]["NONE"])

    def test_dates(self):
        import datetime as dt
        self.assertEqual(P.last_quarter_end(dt.date(2026, 10, 15)), dt.date(2026, 9, 30))
        self.assertEqual(P.last_quarter_end(dt.date(2026, 1, 5)), dt.date(2025, 12, 31))
        self.assertEqual(P.last_friday(dt.date(2026, 10, 15)), dt.date(2026, 10, 9))


if __name__ == "__main__":
    unittest.main()
