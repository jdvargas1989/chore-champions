"""Refresh data/sp500.json (the screening universe) from Wikipedia's constituents table.

Runs inside the GitHub job (needs pandas + lxml). If Wikipedia's layout ever changes and this
breaks, edit data/sp500.json by hand: {"companies": [{"ticker": "AAPL", "name": "Apple"}, ...]}
"""
import io
import json
import sys

URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def main():
    import pandas as pd
    import requests
    html = requests.get(URL, headers={"User-Agent": "ChoreChampionsDataJob/1.0"}, timeout=30).text
    table = pd.read_html(io.StringIO(html))[0]
    sym_col = next(c for c in table.columns if str(c).lower().startswith("symbol"))
    name_col = next(c for c in table.columns if str(c).lower().startswith("security"))
    companies = [{"ticker": str(s).strip().replace(".", "-"), "name": str(n).strip()}
                 for s, n in zip(table[sym_col], table[name_col])]
    if len(companies) < 400:
        sys.exit(f"only {len(companies)} rows parsed; refusing to overwrite the list")
    with open("data/sp500.json", "w", encoding="utf-8") as f:
        json.dump({"source": "Wikipedia", "companies": companies}, f, separators=(",", ":"))
    print(f"wrote {len(companies)} companies to data/sp500.json")


if __name__ == "__main__":
    main()
