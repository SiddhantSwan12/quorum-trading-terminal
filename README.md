# Quorum, a trading terminal on TradingAgents

Deployment configuration and current status are documented in [DEPLOYMENT.md](DEPLOYMENT.md).

## Company research additions

- **Company page → Documents → Annual reports:** automatically discovers published report links, grouped by year with newest first. Indian `.NS`/`.BO` companies use Screener's public company index and link directly to BSE/NSE originals. US companies use SEC EDGAR annual filings (10-K, 20-F, 40-F and amendments); configure `SEC_USER_AGENT` with a contact email. `GET /api/companies/{symbol}/reports` caches successful indexes for 24 hours and retains saved links with a warning during source outages. Coverage depends on the public provider; no report URLs or missing years are fabricated. Opening a link does not import its contents for agent analysis.
- **Your attached documents:** upload a text PDF (up to 20 MB/600 pages), or import a direct public HTTPS PDF link. Attach its title, type, reporting period and publication date. Search text and open the original at an exact PDF page. Check the reports to include in the next TradingAgents run; the run confirmation names that selected excerpts go to the configured LLM. Supplied passages and verified page references remain attached to the saved run. Future-dated/undated reports cannot enter a dated analysis. Removing a library entry archives it; originals remain available to earlier citations.
- **Persistent alerts:** existing browser alerts migrate to SQLite on connection. The backend checks prices, results dates and completed agent ratings without an open browser. Keep the Python process running: monitoring stops with it, while conditions and fired history survive restarts. Price checks use delayed Yahoo quotes with trade timestamps and wait when the latest trade is over 30 minutes old. The Alerts screen shows connection, source errors and last checks. Email/webhook delivery is not included.
- **Financials → Ratios:** annual ROE/ROCE, leverage, liquidity, working-capital cycles and cash conversion, computed from available statements. Missing inputs stay unavailable. Indian P&L can request NSE standalone or consolidated series explicitly; that selector applies only to income statements, does not replace the blended history, and never substitutes the other basis. FII/DII breakdown is still unavailable.
- **Research lab (`#/lab`):** a long-or-cash SMA crossover backtest with next-session-open execution, fees, slippage, date windows, equity/benchmark, drawdown and a hypothetical fill ledger. Two/four-chart layouts share interval/range/style/indicators and link pan/zoom across overlapping time ranges. Crosshairs and price scales remain independent. Backtests do not submit orders or change the paper account.

Install updated backend requirements before using documents: `python -m pip install -r server/requirements.txt`. Restart an already running Python backend to load the new routes. No additional API key is needed for local document upload/search; a confirmed agent run still uses the existing LLM provider.

Report limitations: scanned pages require a separately OCR-processed PDF; discovery of issuer URLs is not automatic. Agent Reach can find public source links for import, but is not an application runtime dependency. Document search is page-text keyword ranking and agents receive a bounded set of excerpts, not an exhaustive reading of every attached PDF. Citation links verify the supplied source/page identity; they do not automatically prove a generated claim.

Feature checks: `python -m unittest discover -s server -p "test_*.py" -v` and `node --test terminal/src/lib/backtest.test.js`. Browser QA scripts and results are in `.taste-skill/`.

A dark data-terminal for stock traders who keep it open all day, covering **US (NYSE, Nasdaq)**
and **Indian (NSE, BSE)** listings, with the
[TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent pipeline wired into
every symbol's page.

```
Trading_agent/
├─ terminal/        the web app: React 18 + Vite 6 + Tailwind 4, no chart library
├─ server/          optional FastAPI bridge: Yahoo Finance bars and live TradingAgents runs
└─ TradingAgents/   the upstream framework, cloned as-is (python package)
```

## Run the terminal

```bash
cd terminal
npm install
npm run dev          # http://localhost:5173
```

Without the server it runs on a **simulated feed**: a seeded 260-session history per symbol, a
replay of the last session when a market is closed and live-looking ticks when it is open, in each
exchange's own hours (09:30–16:00 New York, 09:15–15:30 Mumbai). Prices are plausible samples, not
real quotes. The paper account starts empty: every fill and every agent run is your own.

## Connect real prices and real agent runs

```bash
pip install -r server/requirements.txt
pip install -e TradingAgents            # TradingAgents needs Python 3.11+
cp server/.env.example server/.env      # one LLM key, and TRADINGAGENTS_LLM_PROVIDER to match it
python -m uvicorn server.app:app --port 8000 --reload
```

`server/.env` picks the provider and models (`TRADINGAGENTS_LLM_PROVIDER=anthropic` with
`claude-opus-5-5` / `claude-sonnet-5`, or `openai` with its defaults); a key already set in the
Windows environment is used without copying it into the file. Keep `npm run dev` running; Vite
proxies `/api` to the server. `GET /api/health` reports what the server found.
On OpenAI's no-payment tier each model allows 50 requests a day, and a run makes about 16 calls (12 on
the quick model, 4 on the deep one), so a day holds about three runs. The per-minute token caps differ:
gpt-6-luna allows 60,000 tokens a minute, gpt-6-sol 10,000, and an analyst's call with its price
data runs past 10,000, so the quick model must be luna; a request larger than the cap fails outright
and every retry of it still counts against the day. `TRADINGAGENTS_CHECKPOINT_ENABLED=true` (set here)
lets a run that stops part-way resume at its last finished step. With the server up:

- daily, 5-minute and 30-minute bars come from Yahoo Finance for every symbol, including `.NS`
  and `.BO` tickers, plus the USD/INR rate;
- **Run TradingAgents** on a symbol's page starts the full pipeline (four analysts, the bull and
  bear debate, the trader, the risk debate, the portfolio manager) and streams each report into the
  page as the agent files it. One debate round each by default; raise it in `server/.env`.
- the order book stays simulated: no free feed carries depth.

Yahoo rate-limits a machine that calls it too fast. If that happens the top bar says "Simulated;
live feed retrying" and the terminal reconnects by itself once the block clears (usually minutes).

## Screens

| Screen | What it answers | Route |
| --- | --- | --- |
| Overview | What moved the account today, and the ticket to act on it | `#/` |
| Watchlist | Every symbol in both markets, sortable, with the agents' latest rating | `#/watchlist` |
| Analytics | How the account has done against the S&P 500 and the Nifty 50 | `#/analytics` |
| Symbol | The agents' verdict first, then price, position, trade plan, the twelve reports and the statements | `#/symbol/NVDA` |

The overview's watchlist, chart, order book and order ticket all follow the selected symbol.

**The chart** is TradingView's [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache-2.0) themed from the terminal's tokens, with the toolbars of the big platforms: wheel or
pinch to zoom, drag to pan, a crosshair with the bar's open, high, low, close, change and volume in
the corner. Above it: the interval (1m, 5m, 15m, 30m, 1h, 1D, 1W, 1M, each from Yahoo at the
period that fits it), the type (candles, hollow candles, bars, line, area, baseline, Heikin Ashi),
indicators (SMA 20/50/200, EMA 9/21, Bollinger, VWAP on intraday, RSI and MACD in panes of their
own), Compare (another symbol, both read in percent), Alert (a price alert drawn as a line),
snapshot and fullscreen. On the left, the drawing rail: cursor (cross or arrow), lines (trend,
ray, extended, horizontal, horizontal ray, vertical), Fibonacci retracement, long and short
position boxes with the stop, the target at two to one and the percentages, rectangle and brush,
text and icons, and a measure box (price change, bars and time between two points, gone at the
next click); then magnet (snap to the bar's open, high, low or close), keep drawing, hide
drawings, undo the last and remove all. Escape cancels a shape in progress. Drawings are kept
per symbol and follow the chart across intervals. Below: the range presets 1D to All, the
exchange's time zone and a log scale. The sell and buy buttons on the chart are the delayed bid
and ask; a click prefills the order ticket. Candles are green when the close is above the open and
red when below. Without the server the chart has today's five-minute bars and the daily bars only.
Clicking a level in the book sets a limit on the ticket. `/` focuses the symbol search.

**Any listed company.** With the server up, the search box resolves every NSE, BSE, NYSE and
Nasdaq listing by name or ticker (Avantel, Tata Motors, Nvidia…), not only the built-in list. An
opened company gets the full page and stays in the list; **Watch** adds it to the overview watchlist.

**Data sources, all free.** Prices, intraday bars, FX, search and the latest statements come from
Yahoo Finance (unofficial; the server paces itself and backs off for five minutes on a rate
limit). For US companies, **SEC EDGAR** adds every quarter and year ever filed (official; set
`SEC_USER_AGENT=Quorum terminal you@example.com` in `server/.env`, as SEC's fair-access policy
asks). For Indian companies, **NSE's own filings** add quarterly results back to about 2019 and
the promoter / public shareholding by quarter (unofficial, read with a browser-like client, best
effort: it fails quietly when NSE refuses). Every period seen is kept in `server/data/quorum.db`,
so the history only grows. The order book shows Yahoo's delayed best bid and ask, one level; paper
fills use the last trade.

**Fundamentals**, Screener-style, on every company page: a ratio strip (market cap, P/E, book
value, P/B, dividend yield, ROCE, ROE, debt/equity, EV/EBITDA, EPS), machine-generated checks,
and the statements as tables: quarterly results, annual profit and loss, balance sheet, cash
flow, shareholding, and a peer table from the companies in your list. Figures are rupee crore for
Indian companies and dollar millions for US ones. Source: Yahoo Finance, so 5 quarters and about
5 years of history, and holding split into promoters, institutions and public only. The same
figures are handed to every TradingAgents run as context, so the agents quote what the page shows.

**Kronos projection**, on every company page and on the 1M and 1Y charts: [Kronos](https://github.com/shiyu-coder/Kronos)
(MIT), a K-line foundation model, run locally on the symbol's last 400 daily bars. The server samples
30 paths for the next ten sessions and keeps the 10th, 50th and 90th percentile close per day; the
chart draws the median dashed inside the band, the panel gives the numbers, and the same line is
handed to the agents. One projection per symbol per completed bar, kept in `server/data/quorum.db`.
Needs `pip install torch einops safetensors` (a CUDA build of torch uses the GPU, about three seconds
a symbol; the CPU build works, slower); the 400 MB weights download from Hugging Face on first use.
The sampled paths agree with each other more than daily volatility would suggest, so read the band as
the model's agreement, not as odds.

**Screener** (`#/screen`): the Nifty 500 and the S&P 500 behind a bar of filter chips, each
opening its bounds under it: price, day change, market cap, P/E, P/B, PEG, EV/EBITDA, dividend
yield, payout, ROCE, ROE, margins, debt/equity, sales and profit growth over one and three years
and the last quarter, 52-week change and position, beta, the sell-side consensus and target, the
next results date, promoter and institutional holding, and the checks a company fails; plus
market, your watchlist, sector and a text search. Column sets (Overview, Performance, Technicals,
Extended hours, Forecasts, Valuation, Dividends, Profitability, Income statement, Balance sheet,
Cash flow, Per share, Agents) change the table, not the filter, and "+ Column" adds any field to
a set. "Describe" turns a screen written in words into chip bounds with the quick model, one
request of the daily quota. Refresh re-runs the screen; Maximize fills the window. Presets and
saved screens as before. Pre- and post-market figures exist for US names only. The server fills fundamentals for the
whole universe in the background (about a company every 2.5 seconds, once a day, kept in
`server/data/quorum.db`), so the first fill takes under an hour and later starts are instant.

**Sizing and the pre-trade gate** on the order ticket: a buy is sized from the risk budget (1% of
net liquidation by default) and the stop (two ATRs under the price by default), and must pass the
gate: risk within budget, position and sector under their caps, no buys after a 2% day loss or
three losing closed trades in a row, cash available. Limits are yours to set on the ticket.

**Alerts** (`#/alerts`): price above or below a level, results due within three days, or the
agents changing their rating. Checked every second in the browser; a toast and, if allowed, a
desktop notification. The page also lists the next results date for every holding.

**Journal** (`#/journal`): every closed trade with its realised result and a lesson, every open
position with its thesis, a weekly review, best and worst, and the latest lessons.

## Add symbols

Edit `terminal/src/data/universe.js`. A row needs the Yahoo ticker (`RELIANCE.NS`, `ASIANPAINT.BO`,
`AAPL`), a name, a sector and, for the simulated feed, a reference price, a daily volatility and an
average volume. The order of the list is the default watchlist per market.

## Design

Built with the data-terminal style from the taste-skill app sheet (seed 1195622558): cards darker
than the ground, every selection the same 1px accent line, every figure in IBM Plex Mono with
tabular digits, signs in the text. Colour is reserved for the "what moved today" columns, the
headline change, and the two sides of the book. Panels carry no shadow: they are darker than the
ground, and that is their depth.

The agents have a register of their own. The verdict at the top of a company page and the twelve
reports are set in Source Serif 4, so the desk's written judgment reads apart from the terminal's
chrome (Public Sans) and its tape (IBM Plex Mono). The bull and the bear, and the three risk
analysts, are read side by side, the way they argued.

TradingAgents is a research framework; nothing here is financial, investment or trading advice.
Paper orders never leave the browser.
