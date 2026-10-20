# Support/resistance model: full change set

Unzip over the project folder (same level as server.py). Files under `static/` go in your `static/` folder.

| File | Change |
|---|---|
| sr_model.py | NEW. Pivot-based support/resistance zones; support-bounce and resistance-breakout long setups. |
| tests/test_sr_model.py | NEW. No-look-ahead, well-formed signals, backtest/holding/recommend wiring. `python tests/test_sr_model.py` |
| technical.py | Trend signals tagged with `model`/`setup`; new `compare_signals()`. Rules unchanged. |
| backtest.py | `run()` takes optional `signal_fn` (default unchanged). |
| holding.py | `propose(..., model=)`: history cross-check replays the matching rule set. |
| recommend.py | `model="trend"|"sr"|"either"`; picks carry `model` and `setup`. |
| server.py | `/api/analyze`: `sr_signal`, `sr_cmp_check`, `sr_hold`, `model_agreement`, and `signal_model` (which setup drives the suggested split and funding outcomes). `/api/recommend`: `model`. `/api/backtest`: `strategy`. `/api/track/evaluate`: entries carry `model`/`setup`. |
| agent_tools.py, agent.py | `sr_levels` tool; `strategy` on backtest_stock; `model` on suggest_stocks; rule set on watchlist entries. |
| static/index.html | S/R box and chart lines, S/R summary column, rule-set selectors (Suggest, Backtest, Sizing and funding), rule-set column and comparison rows on Evaluation, Eval tick adds one entry per rule set with a LONG setup. |
| static/eval-store.js | Entries remember `model`/`setup`; old lists load as trend; one entry per rule set per stock/date/source. |
| static/agent-widget.js | Welcome text and starter chips now cover sectors (momentum, rotation, stocks to review); each reply shows which data it used ("Used: sector rotation"). |
| sectors.py | NEW. Live-price overlay: 1D = latest traded price (LTP) vs previous close, refreshed every 30 s on top of end-of-day history. NSE macro sectors and industry groups with tiered baskets (large, mid, small; editable via `sector_map.json`), equal-weight indices for all, large and mid+small, daily/weekly/monthly series, momentum score and status. Sector comparison chart and `sector_momentum` assistant tool. |
| static/index.html (Settings) | New **Discover NSE tools** panel: lists every tool on the Bhavcopy and CM servers, highlights ones that mention sectors, industries, indices or constituents, and copies the list. |
| sectors.py, server.py, static/index.html | Sector rotation view: relative strength vs benchmark (RS-Ratio and RS-Momentum) with trails and Leading/Improving/Weakening/Lagging quadrants, early-mover flags, for sectors or the industry groups of one sector. `/api/sectors/rotation`. |
| agent_tools.py, agent.py | New `sector_rotation` assistant tool. |
| sectors.py, static/index.html, sector_map.example.json | `sector_map.json` now MERGES with the built-in lists (add `"_mode": "replace"` to use only your file). The Sectors tab shows where the file goes and whether it was found. Example file included. |
| sectors.py, server.py, static/index.html | **Stocks to review**: a mechanical shortlist from sector momentum, rotation and the stocks' own setups, with reasons and cautions; tick stocks and send them to Portfolio analysis. `/api/sectors/picks`. |
| agent_tools.py, agent.py | New `sector_picks` assistant tool (the Stocks to review shortlist). |
| static/TradeTheTrend-User-Manual.pdf, static/index.html | The illustrated user guide (PDF) is part of the app: a **Guide** link in the top bar and a **User guide** panel in Settings. |
| static/index.html | **Table filters** on every table with 4+ rows in every tab: a filter box under each column heading (text, `>5`, `<=-2`, `3..8`, `a, b`, `!x`), click a heading to sort, a row count and Clear filters. Filters survive refreshes. "Select all" now selects only the rows left showing. |
| DEPLOY.md | New section 10c describing the two rule sets. |

No new dependencies. Information only, not investment advice.
