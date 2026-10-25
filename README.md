# US 52-Week High / Gainer Screener

A standalone screener for US-listed stocks (NASDAQ, NYSE, NYSE American),
independent of the Indian-market project. It uses Nasdaq's public stock
screener and daily historical-price API — no credentials required.

## Setup

```sh
./install_agent.sh      # creates .venv and installs pandas, openpyxl, requests
```

## Run

```sh
./run_us_agent.sh                       # poll the latest completed session
./run_us_agent.sh --as-of 2026-10-23    # historical cutoff
./run_us_agent.sh --symbols AAPL MSFT NVDA   # limited scan (writes a _limited file)
.venv/bin/python -m unittest test_us_highs -v
```

Output is written to `output/us_52w_high_3of5_<session>.xlsx`.

## Universe filters (mega / large / mid only)

- NASDAQ, NYSE, NYSE American common stocks and ADRs. Name filtering excludes
  funds, trusts, preferreds, warrants, rights, units, and debt.
- **Market cap >= $2B** — mid-cap and larger only; small, micro and nano caps
  excluded. Tiers: `MEGA >= $200B`, `LARGE >= $10B`, `MID >= $2B`.
- **Last close >= $5** — no penny stocks.
- **20-session average dollar volume >= $5M** — no illiquid names.

## Signal rules

- **52-week high**: a day's intraday HIGH strictly exceeds the maximum daily
  HIGH of the preceding 364 calendar days. Ties do not count. A full 52-week
  history and all five recent sessions (from SPY's trading calendar) are
  required; today's unfinished bar is always excluded.
- **Gainer**: closed up versus the prior session (positive daily return) on
  >= 3 of the last 5 sessions **and** a 5-session return >= 5%.
- **Volume breakout**: latest-session volume >= 1.5x the 20-session average.

## Sheets

- **52W High 3 of 5** — fresh 52-week high on >= 3 of the last 5 sessions.
- **Gainers 3 of 5** — up on >= 3 of 5 sessions and 5-day return >= 5%.
- **Common** — stocks in both of the above (AND).
- **OR** — union of the 52W-high and gainer lists (either signal).
- **OR + Vol Breakout** — the OR set filtered to volume breakouts.
- **Common + Vol Breakout** — the Common (AND) set filtered to volume breakouts.
- **Coverage** — every scanned symbol and all exclusions/failures.
- **Run Info** — rules, filters, counts, and completeness.

## Files

- `cache/` — Nasdaq historical responses, keyed by symbol and cutoff.
- `output/` — generated workbooks.

Nasdaq-provided OHLC is used without corporate-action adjustment; splits and
other corporate actions can affect signals. Results are for screening, not
investment advice.