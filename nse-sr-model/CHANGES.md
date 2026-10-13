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
| static/agent-widget.js | New starter chip for support and resistance. |
| DEPLOY.md | New section 10c describing the two rule sets. |

No new dependencies. Information only, not investment advice.
