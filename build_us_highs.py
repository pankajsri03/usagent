#!/usr/bin/env python3
"""Standalone Nasdaq-sourced US 52-week-high and gainer screener."""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

ROOT = Path(__file__).resolve().parent
API = "https://api.nasdaq.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
           "Referer": "https://www.nasdaq.com/"}
LOCAL = threading.local()
LOG = logging.getLogger("us_highs")

# US market-cap tiers (USD). We keep mid-cap and larger only -- no small/micro/nano.
MEGA_CAP_USD = 200_000_000_000
LARGE_CAP_USD = 10_000_000_000
MID_CAP_USD = 2_000_000_000
MIN_PRICE_USD = 5.0                 # exclude penny stocks
MIN_AVG_DOLLAR_VOLUME_USD = 5_000_000  # 20-session average dollar volume liquidity floor
MIN_GAINER_RETURN_PCT = 5.0         # Gainers also require a 5-session return of at least this much
VOL_BREAKOUT_MULT = 1.5             # latest-session volume >= this x 20-session average = breakout


def cap_tier(market_cap: float) -> str:
    if market_cap >= MEGA_CAP_USD:
        return "MEGA"
    if market_cap >= LARGE_CAP_USD:
        return "LARGE"
    if market_cap >= MID_CAP_USD:
        return "MID"
    return "BELOW_MID"


def parse_market_cap(value) -> float:
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        cap = float(text)
    except (ValueError, TypeError):
        return 0.0
    return cap if math.isfinite(cap) and cap > 0 else 0.0


def get_json(route: str, params: dict) -> dict:
    if not hasattr(LOCAL, "session"):
        LOCAL.session = requests.Session()
        LOCAL.session.headers.update(HEADERS)
    response = LOCAL.session.get(f"{API}/{route}", params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"Nasdaq returned no data: {payload.get('status')}")
    return data


def fetch_universe(min_market_cap: float = MID_CAP_USD, min_price: float = MIN_PRICE_USD) -> list[dict]:
    stocks = {}
    for exchange in ("nasdaq", "nyse", "amex"):
        data = get_json("screener/stocks", {"exchange": exchange, "limit": 10000, "offset": 0})
        rows = data["table"]["rows"]
        if len(rows) != int(data["totalrecords"]):
            raise ValueError(f"Incomplete {exchange} stock universe")
        for row in rows:
            name = row["name"]
            symbol = row["symbol"].strip()
            excluded = re.search(r"\b(warrants?|rights?|units?|preferred|notes?|debentures?|ETF|ETN|fund|trust)\b", name, re.I)
            if excluded or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
                continue
            market_cap = parse_market_cap(row.get("marketCap"))
            try:
                price = number(row["lastsale"])
            except (ValueError, KeyError, TypeError):
                price = 0.0
            if market_cap < min_market_cap or price < min_price:
                continue
            stocks[symbol] = {"SYMBOL": symbol, "COMPANY": name, "EXCHANGE": exchange.upper(),
                              "MARKET_CAP_USD": market_cap, "CAP_TIER": cap_tier(market_cap)}
    if not stocks:
        raise ValueError("Nasdaq returned an empty stock universe after cap/price filters")
    return list(stocks.values())


def number(value: str) -> float:
    result = float(str(value).replace("$", "").replace(",", ""))
    if not math.isfinite(result):
        raise ValueError("Non-finite price or volume")
    return result


def fetch_history(symbol: str, end: date, assetclass: str = "stocks") -> pd.DataFrame:
    start = end - timedelta(days=390)
    cache = ROOT / "cache" / f"{symbol}_{end.isoformat()}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        data = get_json(f"quote/{symbol}/historical", {
            "assetclass": assetclass, "fromdate": start.isoformat(),
            "todate": end.isoformat(), "limit": 5000,
        })
        rows = (data.get("tradesTable") or {}).get("rows") or []
        if not rows or len(rows) != int(data["totalRecords"]):
            raise ValueError("Missing or truncated daily history")
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(rows))
        temporary.replace(cache)
    frame = pd.DataFrame({
        "DATE": [datetime.strptime(row["date"], "%m/%d/%Y") for row in rows],
        "HIGH": [number(row["high"]) for row in rows],
        "CLOSE": [number(row["close"]) for row in rows],
        "VOLUME": [number(row["volume"]) if row["volume"] not in ("N/A", "--", "", None) else float("nan") for row in rows],
    }).set_index("DATE").sort_index()
    if frame.index.has_duplicates or (frame[["HIGH", "CLOSE"]] <= 0).any().any():
        raise ValueError("Duplicate dates or invalid prices")
    return frame.loc[str(start):str(end)]


def evaluate(history: pd.DataFrame, sessions: pd.DatetimeIndex,
             min_dollar_volume: float = MIN_AVG_DOLLAR_VOLUME_USD) -> dict:
    if len(sessions) != 5 or not sessions.is_unique or not sessions.is_monotonic_increasing:
        raise ValueError("Exactly five ordered market sessions required")
    if not sessions.isin(history.index).all():
        raise ValueError("Missing one or more of the last five market sessions")
    if history.index.min() > sessions[0] - pd.Timedelta(days=364):
        raise ValueError("Insufficient history for a full 52-week comparison")
    volumes = history["VOLUME"].reindex(sessions)
    if volumes.isna().any() or (volumes <= 0).any():
        raise ValueError("No trading volume in one of the last five sessions")
    recent = history.loc[:sessions[-1]].tail(20)
    dollar_volume = (recent["CLOSE"] * recent["VOLUME"]).dropna()
    avg_dollar_volume = float(dollar_volume.mean()) if not dollar_volume.empty else 0.0
    if avg_dollar_volume < min_dollar_volume:
        raise ValueError("Average dollar volume below liquidity floor")
    recent_volume = recent["VOLUME"].dropna()
    avg_volume_20 = float(recent_volume.mean()) if not recent_volume.empty else 0.0
    last_volume = int(history.loc[sessions[-1], "VOLUME"])
    vol_ratio = (last_volume / avg_volume_20) if avg_volume_20 > 0 else 0.0
    prior = history["HIGH"].rolling("364D", closed="left").max()
    hits = history["HIGH"].gt(prior).reindex(sessions)
    prev_close = history["CLOSE"].shift(1)
    gains = history["CLOSE"].gt(prev_close).reindex(sessions)
    base_close = float(prev_close.loc[sessions[0]])
    result = {"HIGH_HITS_5D": int(hits.sum()),
              "GAIN_DAYS_5D": int(gains.sum()),
              "RETURN_5D_PCT": round((float(history.loc[sessions[-1], "CLOSE"]) / base_close - 1) * 100, 2)
              if base_close > 0 else None,
              "LAST_CLOSE_USD": float(history.loc[sessions[-1], "CLOSE"]),
              "LAST_VOLUME": last_volume,
              "AVG_VOL_20D": round(avg_volume_20),
              "VOL_RATIO": round(vol_ratio, 2),
              "VOL_BREAKOUT": "Yes" if vol_ratio >= VOL_BREAKOUT_MULT else "No",
              "AVG_DOLLAR_VOL_20D_USD": round(avg_dollar_volume)}
    for session in sessions:
        label = session.date().isoformat()
        result[f"HIT_{label}"] = "Y" if hits.loc[session] else "N"
        result[f"HIGH_{label}"] = float(history.loc[session, "HIGH"])
        result[f"PRIOR_52W_HIGH_{label}"] = float(prior.loc[session])
    return result


def write_workbook(high_rows: list[dict], gain_rows: list[dict], coverage: list[dict],
                   sessions: pd.DatetimeIndex, end: date, limited: bool) -> Path:
    output = ROOT / "output"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"us_52w_high_3of5_{sessions[-1].date()}{'_limited' if limited else ''}.xlsx"
    high_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "HIGH_HITS_5D",
                    "RETURN_5D_PCT", "LAST_CLOSE_USD", "LAST_VOLUME", "AVG_DOLLAR_VOL_20D_USD"]
    gain_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "GAIN_DAYS_5D",
                    "RETURN_5D_PCT", "LAST_CLOSE_USD", "LAST_VOLUME", "AVG_DOLLAR_VOL_20D_USD"]
    high_df = pd.DataFrame(high_rows)
    high_df = (pd.DataFrame(columns=high_columns) if high_df.empty else
               high_df.sort_values(["HIGH_HITS_5D", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                                   ascending=[False, False, False, True])[high_columns])
    gain_df = pd.DataFrame(gain_rows)
    gain_df = (pd.DataFrame(columns=gain_columns) if gain_df.empty else
               gain_df.sort_values(["GAIN_DAYS_5D", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                                   ascending=[False, False, False, True])[gain_columns])
    # Common = stocks in BOTH lists: fresh 52W high on >= 3/5 AND up on >= 3/5.
    common_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "HIGH_HITS_5D",
                      "GAIN_DAYS_5D", "RETURN_5D_PCT", "LAST_CLOSE_USD", "LAST_VOLUME", "AVG_DOLLAR_VOL_20D_USD"]
    gain_symbols = {row["SYMBOL"] for row in gain_rows}
    common_rows = [row for row in high_rows if row["SYMBOL"] in gain_symbols]
    common_df = pd.DataFrame(common_rows)
    common_df = (pd.DataFrame(columns=common_columns) if common_df.empty else
                 common_df.sort_values(["HIGH_HITS_5D", "GAIN_DAYS_5D", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                                       ascending=[False, False, False, False, True])[common_columns])
    # Common + Volume Breakout = the AND set, kept only when the latest session is a volume breakout.
    common_breakout_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "HIGH_HITS_5D",
                               "GAIN_DAYS_5D", "RETURN_5D_PCT", "VOL_RATIO", "LAST_VOLUME", "AVG_VOL_20D",
                               "LAST_CLOSE_USD", "AVG_DOLLAR_VOL_20D_USD"]
    common_breakout_rows = [row for row in common_rows if row.get("VOL_BREAKOUT") == "Yes"]
    common_breakout_df = pd.DataFrame(common_breakout_rows)
    common_breakout_df = (pd.DataFrame(columns=common_breakout_columns) if common_breakout_df.empty else
                          common_breakout_df.sort_values(["VOL_RATIO", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                                                         ascending=[False, False, False, True])[common_breakout_columns])
    # OR = union of the two lists (no volume filter). OR + Vol Breakout adds the breakout gate.
    or_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "SIGNAL", "HIGH_HITS_5D",
                  "GAIN_DAYS_5D", "RETURN_5D_PCT", "VOL_RATIO", "VOL_BREAKOUT", "LAST_CLOSE_USD",
                  "LAST_VOLUME", "AVG_DOLLAR_VOL_20D_USD"]
    high_symbols = {row["SYMBOL"] for row in high_rows}
    union = {}
    for row in high_rows + gain_rows:
        union.setdefault(row["SYMBOL"], row)
    or_rows = []
    for symbol, row in union.items():
        is_high = symbol in high_symbols
        is_gain = symbol in gain_symbols
        signal = "BOTH" if is_high and is_gain else ("52WH" if is_high else "GAINER")
        or_rows.append({**row, "SIGNAL": signal})
    or_df = pd.DataFrame(or_rows)
    or_df = (pd.DataFrame(columns=or_columns) if or_df.empty else
             or_df.sort_values(["HIGH_HITS_5D", "GAIN_DAYS_5D", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                               ascending=[False, False, False, False, True])[or_columns])
    # OR + Volume Breakout = union of the two lists, kept only when the latest session is a volume breakout.
    breakout_columns = ["SYMBOL", "COMPANY", "EXCHANGE", "CAP_TIER", "MARKET_CAP_USD", "SIGNAL", "HIGH_HITS_5D",
                        "GAIN_DAYS_5D", "RETURN_5D_PCT", "VOL_RATIO", "LAST_VOLUME", "AVG_VOL_20D",
                        "LAST_CLOSE_USD", "AVG_DOLLAR_VOL_20D_USD"]
    breakout_rows = [row for row in or_rows if row.get("VOL_BREAKOUT") == "Yes"]
    breakout_df = pd.DataFrame(breakout_rows)
    breakout_df = (pd.DataFrame(columns=breakout_columns) if breakout_df.empty else
                   breakout_df.sort_values(["VOL_RATIO", "RETURN_5D_PCT", "MARKET_CAP_USD", "SYMBOL"],
                                           ascending=[False, False, False, True])[breakout_columns])
    successful = sum(row["STATUS"] == "SCANNED" for row in coverage)
    metadata = [
        ("Source", "Nasdaq public stock screener and daily historical prices"),
        ("Generated UTC", datetime.now(ZoneInfo("UTC")).isoformat()),
        ("Requested cutoff", end.isoformat()),
        ("Market sessions", ", ".join(session.date().isoformat() for session in sessions)),
        ("52W High 3 of 5 sheet", "Fresh 52-week high (daily HIGH strictly above prior 364-day max) on >= 3 of the last 5 sessions"),
        ("Gainers 3 of 5 sheet", "Closed up versus the prior session (positive daily return) on >= 3 of the last 5 sessions AND 5-session return >= 5%"),
        ("Common sheet", "Stocks in BOTH lists: fresh 52-week high on >= 3 of 5 AND up on >= 3 of 5"),
        ("OR sheet", "Union of the 52W-high and gainer lists (either signal), no volume filter"),
        ("OR + Vol Breakout sheet", "Union of the 52W-high and gainer lists, kept only when latest-session volume >= 1.5x the 20-session average"),
        ("Common + Vol Breakout sheet", "The Common (AND) set, kept only when latest-session volume >= 1.5x the 20-session average"),
        ("Completed sessions", "Today's bar is excluded; cutoff defaults to yesterday in New York"),
        ("Universe", "NASDAQ, NYSE, NYSE American stocks/ADRs; name-filtered to exclude funds, trusts, preferreds, warrants, rights, units and debt"),
        ("Cap filter", "Mega, large and mid-cap only (>= $2B). Small, micro and nano caps excluded; tiers: MEGA >= $200B, LARGE >= $10B, MID >= $2B"),
        ("Price filter", "Last close >= $5 (no penny stocks)"),
        ("Liquidity filter", "20-session average dollar volume >= $5,000,000"),
        ("Scope", "LIMITED symbol selection" if limited else "Full filtered Nasdaq stock universe"),
        ("Coverage", f"{successful}/{len(coverage)} evaluated; see Coverage for all exclusions/errors"),
        ("52W High makers (>= 3 of 5)", len(high_rows)),
        ("Gainers (>= 3 of 5)", len(gain_rows)),
        ("Common (both >= 3 of 5)", len(common_rows)),
        ("OR (either signal)", len(or_rows)),
        ("OR + Vol Breakout", len(breakout_rows)),
        ("Common + Vol Breakout", len(common_breakout_rows)),
        ("Completeness", "PARTIAL - some symbols excluded or failed" if successful < len(coverage) else "All selected symbols evaluated"),
        ("Price basis", "Nasdaq-provided OHLC; no additional split/dividend adjustment; corporate actions can affect signals"),
        ("History", "Full 52-week calendar span required; missing last-five sessions excluded"),
        ("Caution", "Screening results, not investment advice. No Indian-market filters applied."),
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        high_df.to_excel(writer, sheet_name="52W High 3 of 5", index=False)
        gain_df.to_excel(writer, sheet_name="Gainers 3 of 5", index=False)
        common_df.to_excel(writer, sheet_name="Common", index=False)
        or_df.to_excel(writer, sheet_name="OR", index=False)
        breakout_df.to_excel(writer, sheet_name="OR + Vol Breakout", index=False)
        common_breakout_df.to_excel(writer, sheet_name="Common + Vol Breakout", index=False)
        pd.DataFrame(coverage).to_excel(writer, sheet_name="Coverage", index=False)
        pd.DataFrame(metadata, columns=["SETTING", "VALUE"]).to_excel(writer, sheet_name="Run Info", index=False)
        for worksheet in writer.book:
            worksheet.freeze_panes = "D2" if worksheet.title != "Run Info" else "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cell in worksheet[1]:
                cell.fill = PatternFill("solid", fgColor="164B43")
                cell.font = Font(color="FFFFFF", bold=True)
            for cells in worksheet.columns:
                width = max(len(str(cell.value or "")) for cell in cells)
                worksheet.column_dimensions[cells[0].column_letter].width = min(65, max(12, width + 2))
            if worksheet.max_row > 1:
                table = Table(displayName="US" + worksheet.title.replace(" ", ""), ref=worksheet.dimensions)
                table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium4", showRowStripes=True)
                worksheet.add_table(table)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, help="Last completed US session cutoff (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", help="Optional limited scan, e.g. AAPL MSFT NVDA")
    parser.add_argument("--min-market-cap", type=float, default=MID_CAP_USD, help="Minimum market cap in USD (default mid-cap floor $2B)")
    parser.add_argument("--min-price", type=float, default=MIN_PRICE_USD, help="Minimum last close in USD (default $5)")
    parser.add_argument("--min-dollar-volume", type=float, default=MIN_AVG_DOLLAR_VOLUME_USD, help="Minimum 20-session average dollar volume (default $5M)")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    end = args.as_of or today - timedelta(days=1)
    if end >= today:
        parser.error("Use a date before today in New York to exclude unfinished daily bars")
    universe = fetch_universe(args.min_market_cap, args.min_price)
    if args.symbols:
        requested = {symbol.upper() for symbol in args.symbols}
        universe = [stock for stock in universe if stock["SYMBOL"] in requested]
        missing = requested - {stock["SYMBOL"] for stock in universe}
        if missing:
            parser.error(f"Symbols absent from eligible Nasdaq universe: {sorted(missing)}")
    benchmark = fetch_history("SPY", end, "etf")
    sessions = benchmark.index[-5:]
    if len(sessions) != 5 or (end - sessions[-1].date()).days > 4:
        raise ValueError("Benchmark sessions are missing or stale; refusing to generate report")
    LOG.info("Scanning %d stocks for %s through %s", len(universe), sessions[0].date(), sessions[-1].date())
    high_rows, gain_rows, coverage = [], [], []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_history, stock["SYMBOL"], end): stock for stock in universe}
        for completed, future in enumerate(as_completed(futures), 1):
            stock = futures[future]
            try:
                result = evaluate(future.result(), sessions, args.min_dollar_volume)
                coverage.append({**stock, "STATUS": "SCANNED",
                                 "DETAIL": f"{result['HIGH_HITS_5D']} high hits, {result['GAIN_DAYS_5D']} up days"})
                enriched = {**stock, **result}
                if result["HIGH_HITS_5D"] >= 3:
                    high_rows.append(enriched)
                if result["GAIN_DAYS_5D"] >= 3 and (result["RETURN_5D_PCT"] or 0) >= MIN_GAINER_RETURN_PCT:
                    gain_rows.append(enriched)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                coverage.append({**stock, "STATUS": "EXCLUDED_OR_FAILED", "DETAIL": str(exc)[:500]})
            if completed % 100 == 0 or completed == len(universe):
                LOG.info("Processed %d/%d; %d high, %d gainers", completed, len(universe), len(high_rows), len(gain_rows))
    if not any(row["STATUS"] == "SCANNED" for row in coverage):
        raise RuntimeError("No symbols evaluated successfully; no workbook written")
    coverage.sort(key=lambda row: row["SYMBOL"])
    path = write_workbook(high_rows, gain_rows, coverage, sessions, end, bool(args.symbols))
    LOG.info("Wrote %s; %d high (>=3/5); %d gainers (>=3/5); %d exclusions/errors", path,
             len(high_rows), len(gain_rows), sum(row["STATUS"] != "SCANNED" for row in coverage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())