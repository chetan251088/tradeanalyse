# TradeAnalyse: project handoff (31 Oct to 9 Nov 2026)

This document summarises one long working conversation between the trader (user `chetan`) and a coding agent. It
covers the analysis of the trader's broker reports, what was built, what was tested, what was found, how to run it,
and what is still open. It is written so another agent can pick up the work without the original chat.

The day-to-day reference manual is [README.md](README.md). This file is the story, the decisions and the open items.
Raw notes kept during the work are in the agent's repo memory; their content is folded in here.

> **Security:** `env/dhan.env` and `openalgo-main/.env` hold live secrets (OpenAlgo API key, Dhan keys, Telegram bot
> token). Never paste them into a chat or commit them. No secrets appear in this document.

---

## Contents
1. [The trader and the goal](#1-the-trader-and-the-goal)
2. [Timeline](#2-timeline)
3. [Analysis of the trader's own reports](#3-analysis-of-the-traders-own-reports)
4. [Architecture decisions](#4-architecture-decisions)
5. [What was built](#5-what-was-built)
6. [Market data downloaded](#6-market-data-downloaded)
7. [Research and backtest results](#7-research-and-backtest-results)
8. [The one survivor: Weekend VRP Harvester](#8-the-one-survivor-weekend-vrp-harvester)
9. [FLOW dashboard (JARVIS)](#9-flow-dashboard-jarvis)
10. [Third-party apps wired to our data](#10-third-party-apps-wired-to-our-data)
11. [Operations cheat-sheet](#11-operations-cheat-sheet)
12. [Gotchas and lessons learned](#12-gotchas-and-lessons-learned)
13. [Current state](#13-current-state)
14. [Next steps](#14-next-steps)
15. [Rules for the next agent](#15-rules-for-the-next-agent)
16. [File map](#16-file-map)

---

## 1. The trader and the goal

**Who:**
- Trades Indian **index options**, mostly intraday scalping: NIFTY, BANKNIFTY and SENSEX, plus some FINNIFTY and
  MIDCPNIFTY.
- Brokers: **Zerodha, Dhan and Kotak**. Dhan is the one connected to automation.
- Lives in **Sweden**: IST = CET + 4:30 in winter (CEST + 3:30 in summer).
- Works on a **corporate Windows PC** behind an HTTPS proxy.
- Has a free **Oracle Cloud VPS** with a static IP, reachable over PuTTY/WinSCP.

**Self-described problem (first message):**
- Good at finding trades and taking small profits many times.
- When a trade goes wrong, hesitates to exit, so the loss grows large.
- When a trade is profitable, exits too quickly.
- Wants a system built on his OpenAlgo fork (`openalgo-main/`, from github.com/marketcalls/openalgo) that:
  - automates his edge;
  - enforces guardrails: stops, partial exits, trailing;
  - eventually runs fully automated on the VPS.

**His claimed edges:**
1. **Expiry-day morning "bounce".** When NIFTY/SENSEX falls steadily on 5-minute bars early on an expiry day, deep
   options that collapsed "must bounce", because expiries don't hand free money to sellers. Catch the bounce, average
   once if needed, exit in profit.
2. **Late-expiry "burst".** Near the close of expiry, some OTM options jump 10x in minutes; catch those.
3. Later, **smaller "fake bounces"** of 5 to 20 minutes, taken without waiting for a full recovery.

**Standing preferences and constraints:**
- **Live order placement stays OFF.** Everything is alerts, paper or shadow until there is evidence. The trader
  agreed to this.
- Deterministic rules decide; LLMs (Grok, Kronos) are advisory at most. The trader dropped Kronos to keep things
  simple.
- Start with Dhan; Kotak and Zerodha may follow later as **separate OpenAlgo instances** (one broker per instance).
- Run locally on Windows first, then move to the VPS.

---

## 2. Timeline

| Date (2026) | Work |
|---|---|
| 31 Oct | Parsed and analysed all broker reports. Read OpenAlgo's architecture. Designed the bot. Set up local OpenAlgo on port 5001 with Dhan. |
| 31 Oct to 1 Nov | Built the `scalper` alert scanner (S1/S2/S3) and the Dhan history downloader. Downloaded 5 years of 1-minute index and option-chain data. |
| 1 Nov | Data audit. Realistic fill model. Backtests of S1/S2/S3. Broad research on buy-side and sell-side ideas: no edge, except the **Weekend VRP Harvester**. Wired the harvester as paper. Windows tasks, then VPS deployment kit, Telegram control bot, README. day-trader-skill evaluation and three workspace skills. |
| 2 to 3 Nov | Built the FLOW options dashboard (JARVIS) and its v2 layout. Bounce studies, Bounce Watch panel, mirror test. Order book (DOM) and futures tape panel, depth recorder, DATA update button. |
| 8 Nov | Integrated MrChartist **Order Flow** and **Option Hub** on our own OpenAlgo/Dhan data. Fixed Option Hub "Offline" with Dhan index prices. |
| 9 Nov | Option Hub proxy: back-off and quieter logging for NSE timeouts and 403s. |

---

## 3. Analysis of the trader's own reports

Script: `analyse.py`, which writes `out/report.txt` and CSVs. `expiry_edge.py` adds the time-of-day breakdown.
`mirror_test.py` does the reversal test.

### 3.1 Inputs

| Broker | Files | Detail available |
|---|---|---|
| Zerodha | `taxpnl-FLA936-*.xlsx` ("Tradewise Exits", F&O section), `pnl-FLA936.xlsx` | Fill pairs with entry and exit **timestamps**. Merged into "episodes": open to flat on one contract. |
| Dhan | `dhan_tax_pnl.xlsx`, `PNL_REPORT.xls` | Per day and contract, **no times** |
| Kotak | `Gain_Loss_YQRCT_*.xlsx` | Per contract per financial year, **no dates**. The FY26-27 file splits buy and sell charges. |

Totals reconcile to each broker's own summary.

### 3.2 Headline numbers (Jan 2022 to Oct 2026)

| Broker | Units | Gross | Charges | Net |
|---|---|---|---|---|
| Zerodha | 1,964 episodes (10,658 fills) | -11.64 L | 3.19 L | **-14.83 L** |
| Dhan | 961 day-contracts | -7.38 L | 3.45 L | **-10.83 L** |
| Kotak | 1,158 contracts | -2.40 L | 3.50 L | **-5.90 L** |
| **All** | | **-21.4 L** | **10.1 L** | **-31.6 L** |

### 3.3 Behaviour findings (Zerodha, timestamped)

**Trade selection is fine.**
- Win rate is 56-64% at every broker in every year.
- Days with only 1-2 trades were 86% green (+2.05 L).
- Trades that were never added to: gross +1.92 L, but charges of 2.89 L made them -0.97 L.

**Size is the problem, not stop distance.**
- In option-price terms, losses are about as big as wins: median win +3.1%, median loss -2.9%.
- In rupees they are not: average loss Rs 4,206 vs average win Rs 1,642.
- That needs about a 72% win rate just to break even.

| Pattern | Trades | Net |
|---|---|---|
| Added to a losing position | 73 | **-13.86 L** (25% won) |
| Never added | 1,891 | -0.97 L |
| Position more than 3x normal size | 236 | **-14.40 L** |
| Days with 11+ trades | 50 days | **-15.75 L** (none of the 12 days with 20+ trades was green) |
| Trades taken when already down Rs 10k+ that day | 386 | -9.98 L |

- **Single worst trade:** 11 Jan 2023, BANKNIFTY 41500 CE.
  - Bought at 828, added 4 times, averaging 785 on 3,600 qty, exited at 544.
  - Loss **-8.72 L in about 4 minutes**.
- Median hold: winners 0.6 min, losers 0.9 min.
- On expiry days the median winner was held **18 seconds** for +4.3%. Only 6 trades in four years made 100% or more.

### 3.4 Expiry-day timing (`expiry_edge.py`)

| Entry window | FY22-24 | FY25 onwards |
|---|---|---|
| 09:15-09:45 | 56% won, +11.8k | 57% won, +13.3k |
| **09:45-12:30** | **69% won, +25.4k** | **68% won, about break-even** |
| 12:30-14:30 | 66% won, -52.3k | 57% won, -27.3k |
| 14:30-close | 51% won, -72.6k | 65% won, -13.9k |

- Morning expiry trades on options priced Rs 100 or more, without adding: 69% won.
- The same setups with averaging down: **0 of 6 won**.
- Late-day trades only worked on options under Rs 10; options at Rs 30-100 lost.

### 3.5 Mirror test (what if every trade had been reversed)

- Zerodha's tradewise sheet has **no buy/sell column**. Short trades list the sell as the "entry", so `analyse.py`
  treats everything as long.
- Sides were inferred by matching fill prices to stored bars. About 72% of NIFTY trades in FY22-24 were shorts.
- **Reversing every trade:** -31.6 L becomes **-8.5 L**, after paying the spread again (19.7 L) plus charges.
  - Only with a free spread would it be +11.3 L.
  - One BANKNIFTY trade (-8.7 L) is most of the difference. Without the best row the reversed total is -17 L.
- The index moved the trader's way only 50/50 of the time, so there is **no directional skill either way**.
- At an equal Rs 25k per trade:
  - Dhan FY25 onwards +2.5 L;
  - Kotak +1.2 L (t 1-2, not significant);
  - Zerodha about 0.
- **Conclusion:** the losses come from **size** (top 10% premium trades = -13.7 L of Zerodha's -14.8 L; 159
  add-to-position episodes = -14.5 L) plus **charges** (10.1 L). No reversal rule set was significant.

---

## 4. Architecture decisions

1. **Keep OpenAlgo unmodified.** It is the broker gateway: login, REST, WebSocket ticks, paper ("Analyze") mode.
   - One OpenAlgo instance holds exactly one broker account.
   - Multiple brokers means multiple instances; the repo has `install/install-docker-multi-custom-ssl.sh`.
2. **One bot process per broker account,** with all strategies inside it, so risk limits are shared and strategies
   don't conflict. It runs as our own package (`scalper/`), not inside OpenAlgo.
3. **Only fixed, backtestable rules may place orders.** LLMs (Grok, Kronos) are advisory or shadow only: they are
   slow, give different answers each time, and can be prompt-injected.
4. **Risk is set in rupees before every entry (R).** Quantity, stop and any add are derived from R. Limits can be
   tightened during the day but loosened only the next day.
5. **The same code runs in backtest, replay, paper and live.** Rules use 1-minute and 5-minute bars because Dhan
   history is 1-minute, so backtests match live.
6. **A broker-side stop must always be in place** once live orders exist; not yet relevant, because live is off.
7. **Own WebSocket client** (`scalper/feed.py`), because the OpenAlgo SDK's depth callback drops OI and volume.

---

## 5. What was built

All of it is in this repo (`C:\solution\python\tradeanalyse`). Python 3.14 in `.venv`; OpenAlgo has its own
`openalgo-main\.venv`.

| Area | Main files | Status |
|---|---|---|
| Report analysis | `analyse.py`, `expiry_edge.py`, `mirror_test.py` | Done |
| Alert scanner (no orders) | `scalper/` (`run.py`, `engine.py`, `setups.py`, `feed.py`, `regime.py`, `outcomes.py`, `store.py`, `notify.py`), `config/dhan.yaml` | Built. S1/S2 disabled; S3 is paper. |
| Replay / history | `scalper/history.py`, `--replay`, `--history YYYY-MM-DD` | Done |
| Data downloader | `tools/dhan_download.py`, `tools/dhan_probe.py`, `tools/audit_data.py` | Done; data complete to 30 Oct (DATA button tops up) |
| Execution and cost model | `scalper/execution.py` | Done |
| Backtest and research | `scalper/backtest.py`, `research.py`, `research_multi.py`, `research_runs.py`, `vrp_harvest.py`, `study.py`, `study_bounce.py`, `study_minibounce.py`, `skill_tests.py` | Done |
| Weekend VRP Harvester (paper) | `scalper/harvest.py`, journal in `data/journal.sqlite` | Live as paper inside the scanner |
| Telegram control bot | `scalper/control.py` | Done |
| Index/VIX top-up | `scalper/index_update.py` | Done |
| Windows automation | `tools/install_tasks.ps1`, `tools/run_*.ps1`, `tools/task_env.ps1` | Built; **tasks removed from the PC on 1 Nov** |
| VPS deployment kit | `deploy/vps/setup.sh`, systemd units, README section 12 | Ready; deployment **not confirmed** |
| Workspace skills | `.github/skills/india-strategy-test`, `india-harvest-ops`, `india-event-check` | Done (ported from vendor/day-trader-skill, MIT) |
| FLOW dashboard | `scalper/flow/*`, `web/flow/app.js` | Done; used live by the trader |
| Order Flow (MrChartist) | `order-flow-main/server/providers/openalgo.mjs` + `.env` loading | Done |
| Option Hub (MrChartist) | `tools/run_option_hub.py` + patches in `india-s-best-option-hub-main/proxy-server.mjs` and `src/` | Done |
| Tests | `tests/` (51 pass) | `.venv\Scripts\python.exe -m pytest -q tests` |

### 5.1 Scanner strategies (expiry-day alerts)

| Code | Idea | Result | Config state |
|---|---|---|---|
| S1 | Exhaustion bounce: the trader's morning idea | Negative in all tests | Disabled |
| S2 | Gamma burst: the late-expiry idea | Negative | Disabled |
| S3 | Quiet-decay iron fly: 11:00-13:30, 60-min index range <= 0.5x ATM straddle, straddle down 3% in 30 min, wings 3 strikes, per-leg stop +50%, exit 15:15, max loss Rs 6,000, once a day | First looked positive. **After an honest fill model, no edge.** | `paper: true` (alerts say PAPER) |

### 5.2 Execution and cost model (`scalper/execution.py`)
- **Fills:** 15 s latency. Half-spread of 0.10% on NIFTY and 0.25% on SENSEX (minimum 1 tick). 1 tick slippage.
  Limit orders for targets; stops fill at the gap price.
- **Charges:** Rs 20 brokerage per order, plus STT, transaction charges and GST.
- **R** = the planned rupee risk of the trade.

---

## 6. Market data downloaded

**Index data**
- **Content:** 5 years of 1-minute NIFTY, SENSEX and INDIAVIX bars.
- **Stored in:** `data/market/index/<NAME>/<YYYY>.parquet`.
- **Index files** contain bars from 07:06 to 18:15; the loaders keep 09:15-15:29.

**Option chains**
- **Source:** Dhan `/v2/charts/rollingoption` (expired options).
- **Coverage:** NIFTY 1,232 days (2021-11-02 to 2026-10-30, 261 expiry days); SENSEX 856 days (2023-05-15 to
  2026-10-30, 181 expiry days). About 0.9 GB, roughly 530 KB per chain file.
- **Stored in:** `data/market/options/<NAME>/chain/<day>.parquet`, ATM +-10 strikes. `days.csv` holds the calendar;
  expiry day = the week's minimum 15:25 straddle/spot ratio below 0.6%.

**Data audit** (`tools/audit_data.py`, output `data/market/audit.csv` and `audit.txt`)
- **NIFTY:** clean.
- **SENSEX problems:**
  - 2023: wrong strikes, filler bars, no OI;
  - Jan-Mar 2024: no OI;
  - Apr-Sep 2024: stale second contract;
  - Sep 2025: duplicate contracts.
- **Handling:** `backtest.clean_chain` checks strikes against the real index ATM, drops no-trade bars and dedupes. A
  day gate requires ATM +-2 coverage of at least 90% and OI present.

**Daily top-up**
- The **DATA button** on the FLOW dashboard does it; so does `scalper.index_update` plus
  `dhan_download.py --chains all`, run after the close.
- **Do not run the chain downloader during market hours.** It shares Dhan's per-user limit with live use.

---

## 7. Research and backtest results

The honest summary: **almost nothing has an edge after realistic costs.**

All results use realistic fills and costs. Train = before 2024-07-01, test = after.

| Idea | Result | Verdict |
|---|---|---|
| S1 exhaustion bounce, expiry days | -0.15R/trade (n=1,068, t=-3.8); negative in both halves; gross already negative | Rejected |
| S1 on non-expiry days | -0.12R (n=1,014) | Rejected |
| S2 gamma burst | -0.47R (planned exit) / -0.23R (50% stop) | Rejected |
| S1 premise on the index (`study.py --sweep`) | Target hit first 44% vs stop first 54%, edge about -0.1R; new extreme within 60 min 59% of the time | Premise not supported |
| Buy side after mid-2024: trend-pullback scalps, breakouts, ITM trend ride, long straddle at every entry time, OI pin | -0.18R, -0.26R, ~0, -17 to -29% premium, no pin | No edge |
| Intraday momentum (first 30 min to close) | t=2.0 in train, 0.08 in test | Gone since mid-2024 (likely SEBI's Nov 2024 expiry rules) |
| Quiet-decay iron fly (S3) | First +0.12R train / +0.064R test. **Inflated** by selling the wing at the stop bar's close (bad prints, e.g. SENSEX 2025-05-27 +4.1R, impossible). Honest fill: **-0.21R test, 0 of 54 neighbouring settings positive**. Best variant +0.03R (t 0.2). | No edge; kept as paper |
| S3 on non-expiry days | -0.22R; never reaches its take-profit intraday | Expiry-only, and still no edge |
| Positional: overnight flies, hold-to-expiry flies, 1-sigma condors, long OTM strangles | All negative or insignificant | Rejected |
| Naked ATM straddle sale (volatility premium) | +2-5% of premium, t<2, fat tails | Not usable |
| **Weekend VRP Harvester** | **+0.31R test (t 2.9, n=86)**, +0.06-0.10R train (not significant); 9 of 9 neighbours positive; both indices positive | **Only survivor; running as paper** |
| Collapsed-put bounce (`study_bounce.py`, puts down 60%+ since 09:20 bought at 13:30) | OTM -34 to -41% per trade, win about 10%, t<-4; held to close -80 to -99% | Rejected ("must bounce" is false) |
| Fake-bounce scalp, 5-20 min (`study_minibounce.py`, 6 trigger types, 4 exit types) | All net negative (-2 to -8% per trade); about -1% even at zero cost. 60% see bid +15% within 20 min, but that is volatility, not edge. | Rejected |
| day-trader-skill pack (ORB, daily setups, time-machine odds) | ORB is a coin flip; daily setups flip sign in test; odds uncorrelated with outcome (-0.03) | Rejected; skills ported for workflow only |

Outputs: `data/research/*.txt`, `minibounce_<NAME>.csv`, `out/mirror_report.txt`.

---

## 8. The one survivor: Weekend VRP Harvester

Full rules: README section 4. Code: `scalper/harvest.py`. Backtest: `python -m scalper.vrp_harvest`, output in
`data/research/vrp_harvest.txt`.

**Rules**
- **When:** 15:22 IST, per index, exactly **4 sessions before the weekly expiry**, so the hold spans a weekend.
  - Holidays come from OpenAlgo `TRADING_HOLIDAY` rows with NFO/BFO closed.
  - Never on an expiry day.
- **VIX filter:** India VIX must be at or above the median of its last 250 daily 15:25 values.
- **Position:**
  - Sell the ATM straddle at the bid.
  - Buy wings at the ask, `clamp(round(2 x straddle / step), 4, 10)` strikes out.
  - Hold to expiry day 15:25. **No stop**: the wings cap the loss.
- **Size:** 1% of paper equity per trade. 0 lots means a **shadow** row, tracked in R only.
  - One NIFTY lot risks about Rs 16k; one SENSEX lot about Rs 6k.
  - About Rs 3.7 L per lot is needed at 2% risk.
- **Gates:**
  - one open position per index;
  - total open risk at most 2.5%;
  - pause new entries after a 6% drawdown;
  - 4-leg spread cost at most 5% of the credit;
  - manual `/pause`.
- **Events** (RBI, Budget and so on) are listed in the alert but do not block it, because the backtest didn't filter
  on them.

**Evidence and caveats**
- **Test period:** +0.31R (t 2.9, n=86); stress test +0.29R.
- **Train period:** not significant on its own.
- **Concentration:** the edge sits after mid-2024, and only the 4-session / VIX-on cell works.
- **Equity curve:** Rs 10 L grew to 15.7 L over 2022-2026 (CAGR 10.6%, max drawdown 5.7%).
- **Treat it as a forward test.**

**Commands**
- Report: `.venv\Scripts\python.exe -m scalper.harvest --report`
- Dry run: `.venv\Scripts\python.exe -m scalper.harvest --check-now --env-file env\dhan.env`

**Going-live checklist**
1. At least 8-10 closed paper trades, in line with the backtest.
2. Set the real `capital_rs` and start at 1% risk.
3. Check events before each entry (skill `india-event-check`).

**Unknown:** whether the scanner has actually run on the 15:22 entry days since 1 Nov. The Windows tasks were removed
and the VPS deployment wasn't confirmed. Check `--report`.

---

## 9. FLOW dashboard (JARVIS)

```powershell
.venv\Scripts\python.exe -m scalper.flow --live --env-file env\dhan.env --open      # http://127.0.0.1:8787
.venv\Scripts\python.exe -m scalper.flow --replay 2026-10-27 --speed 10 --start 09:15 --open
```

- **Built because** the trader wanted an order-block.com style options-flow screen on his own data.
- **Rendering:** pure canvas at 60 fps, on a fixed 1680x945 virtual canvas scaled to the window.

**Model** (`scalper/flow/model.py`):
- Black-76 IV and greeks;
- GEX and gamma flip;
- max pain;
- OI build-up classification;
- trade side;
- decay split.

**Engine** (`scalper/flow/engine.py`):
- **Updates:** analysis once a second, frames 10 times a second.
- **Market read:** three votes (writers 15 min, flow 5 min, day OI), giving a bias.
- **JARVIS events:** minute-by-minute tags and heat.
- **History backfill on start:** about 90 s for 200 contracts.
- **Futures:** NIFTY FUT via `add_future`.
- **Read lines:** a liquidity read (BOOK line) and a breadth line, neither scored.

**Sources** (`scalper/flow/sources.py`):
- REST chain plus WebSocket depth 5 for all contracts.
- Depth 20 for NIFTY FUT plus 49 ATM options (Dhan allows 50 depth-20 slots).

**Panels and keys:**
- chart/decay, OI river, read/GEX/smile, heat, meters, feed;
- **B** = Bounce Watch: a paper panel running the mini-bounce triggers, one ticket per side per 20 minutes;
- **T** = futures **Tape**:
  - aggressor CVD;
  - Cont OFI with a z-score;
  - sweeps of 3+ levels;
  - absorption;
  - walls pulled vs filled;
  - 15-min price/CVD divergence;
  - 2-second heat columns.

**Depth recorder** (`scalper/flow/record.py`):
- **Writes to:** `data/flow/depth/<day>_<HHMMSS>.jsonl.gz`.
- **What:** the future on every packet; ATM +-5 options, index and VIX once a second.
- **Purpose:** raw material for a future order-book study.

**HUD chips:**
- **DATA** (`scalper/flow/updater.py`): one click runs `index_update`, then the chain download (skipped
  09:00-15:35 IST), then the Order Flow NSE archive fetch.
- **ORDER FLOW**: green when the Order Flow server is up; hover shows breadth.

**Order Flow integration:** `scalper/flow/breadth.py` polls Order Flow's `/api/market` each minute. In live mode it
auto-starts the Order Flow node server on port 8790.

**Testing:** a second instance can run on `--port 8788` or `8789` so the trader's 8787 isn't disturbed.

---

## 10. Third-party apps wired to our data

Both apps are MrChartist projects licensed for **personal, non-commercial use only**.

### 10.1 Order Flow (`order-flow-main/`, Node 24)

**Our provider** (`server/providers/openalgo.mjs`):
- Loads OpenAlgo `/instruments` for NFO and BFO once.
- Once a minute, one `/multiquotes` sweep over all spots, futures and ATM +-12 strikes:
  - chunks of 3,000 symbols;
  - send only symbol and exchange, because extra fields are rejected;
  - about 17 s for 213 names.
- Futures 1-minute bars from `/history`; the +4.5 h shift is measured on NIFTY.
- Previous close and OI from `data/archive/*.json.gz`.

**Configuration:**
- `.env`: `MF_PROVIDER=openalgo`, `PORT=8790`, `OPENALGO_ENV_FILE=../env/dhan.env`.
- The repo never loaded `.env`, so `process.loadEnvFile` was added in `server/index.mjs`, `checkProvider.mjs` and
  `scripts/dev.mjs`.

**Build:** use `npx vite build`. `npm run build` hangs in `tsc` on this PC.

**Gaps:** BAJAJHLDNG, DALBHARAT and ICICIPRULI are missing in OpenAlgo; FOCIT has no quote.

### 10.2 Option Hub (`india-s-best-option-hub-main/`)

- **Start (always this way):**
  `openalgo-main\.venv\Scripts\python.exe tools\run_option_hub.py`. Add `--proxy-only` for just the data proxy.
  - Reads the Dhan login from OpenAlgo's database.
  - Passes `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` only in the child process environment; never printed or written.
  - Sets `NODE_USE_ENV_PROXY=1` and `NO_PROXY`.
  - Runs `npm run dev`: page on 4001, proxy on 127.0.0.1:4002.
  - **Restart it after each daily OpenAlgo login**, because the token changes.
- **Plain `npm run dev` doesn't work here.** Node then ignores the office proxy, so you get NSE "warm-up timed out
  after 8000 ms", and there are no Dhan credentials.

**Patches made in `proxy-server.mjs`:**
- **Dhan live feed through the office proxy:** uses `https-proxy-agent` when `HTTPS_PROXY` is set; otherwise the
  socket closes with code 1006.
- **New endpoint `/api/dhan-proxy?endpoint=indices`:**
  - one Dhan `/marketfeed/ohlc` call for NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX and INDIA VIX;
  - answers in NSE's row shape;
  - off-session, Dhan reports close = last price and net change 0, so the previous close comes from daily candles,
    cached per day.
- **Dhan quote calls** (`/marketfeed/*`) go through their own queue at 1 request per 1.1 s (Dhan's limit is 1/s).
- **NSE website timeouts and 403s:** the back-off doubles 1, 2, 4, 8, then 15 min, and resets on success. Logging
  is quieter.

**Client changes:**
- `src/lib/marketApi.ts` `fetchIndicesRaw()` tries NSE first and falls back to the Dhan endpoint; also used by
  `fetchAllIndices`.
- `src/hooks/useMarketData.ts` labels the data source correctly.
- The header now reads "Live" with Dhan prices even when NSE is blocked.

**NSE website 403 explained:**
- NSE's Akamai bot check refuses the proxy's scripted Node client.
- The browser on the same network loads nseindia.com fine, so it isn't an IP ban.
- **Decision: do not build a way around the bot check.** Use Dhan/OpenAlgo data or NSE archive files instead.

**Panels still blocked by NSE:** NSE option chain (Dhan's chain works), holidays, VIX history, OI spurts,
most-active, event calendar, large deals, F&O movers, exchange market status (falls back to the IST clock).

**1Cliq one-click trading:**
- In LIVE mode the app deliberately refuses orders ("Live one-click execution is not enabled in this build"): it is
  Phase 1, paper only, and no UI element calls `placeOrder`.
- Advice given: use Paper mode.
- A guarded live layer on OpenAlgo was offered (fixed size, automatic broker-side stop, no adding, daily loss stop).
  The trader deferred it.

**Option Hub tests not passing (outside the code we changed):**
- The Participants date-picker test fails; it depends on today's date.
- The proxyEnvelope snapshot-dir test fails on Windows paths.
- `proxyFixes.test.ts` imports the proxy in about 10 s, so it is flaky under load; run it alone or with
  `--hookTimeout 60000`.

### 10.3 Not covered
`fii-dii-data-main/` is in the workspace, and the trader has run `npm start` there. It wasn't part of this
conversation.

---

## 11. Operations cheat-sheet

### Ports
| Port | What |
|---|---|
| 5001 | OpenAlgo REST and web UI (Dhan redirect is registered on 5001) |
| 8765 | OpenAlgo WebSocket |
| 5555 | OpenAlgo internal message bus (ZeroMQ) |
| 8787 | FLOW dashboard (8788/8789 for test instances) |
| 8790 | Order Flow |
| 4001 / 4002 | Option Hub page / data proxy |

### Daily start order (Windows PC)
1. **OpenAlgo:** run `cd openalgo-main; .\.venv\Scripts\python.exe app.py`, then open http://127.0.0.1:5001.
   - Log in and connect Dhan after 03:00 IST (22:30 CET) and before 09:15 IST.
   - The terminal needs `HTTPS_PROXY` set (and `NO_PROXY=127.0.0.1,localhost`).
2. **FLOW:** `.venv\Scripts\python.exe -m scalper.flow --live --env-file env\dhan.env --open`. This auto-starts Order
   Flow on 8790.
3. **Option Hub (optional):** `openalgo-main\.venv\Scripts\python.exe tools\run_option_hub.py`.
4. **Scanner and harvester (for paper trades):** `.venv\Scripts\python.exe -m scalper --env-file env\dhan.env`, or
   reinstall the tasks with `pwsh -File tools\install_tasks.ps1`, or deploy to the VPS.
5. **After the close:** click DATA on the FLOW dashboard.

### Other commands
| What | Command |
|---|---|
| Connection check | `.venv\Scripts\python.exe -m scalper --check --env-file env\dhan.env` |
| Replay a stored day | `.venv\Scripts\python.exe -m scalper --replay data/ticks/<day>.jsonl.gz` |
| Rebuild a past day from Dhan | `... -m scalper --history YYYY-MM-DD` |
| Tests (our code) | `.venv\Scripts\python.exe -m pytest -q tests` (bare `pytest` also collects openalgo-main) |
| Option Hub tests | `cd india-s-best-option-hub-main; npx vitest run` |
| Telegram bot | `python -m scalper.control` (`/status /report /check /pause /resume /logs /login /restart`) |

### Schedule (IST)
| IST | What happens |
|---|---|
| 03:00 | Broker session expires |
| 08:45 | Scanner wakes |
| 09:15 | Market opens; expiry setups need the login by now |
| 15:22 | Harvest entry check |
| 15:25 | Marks and expiry closes |
| 15:31 | Summary and index top-up |

The broker login can't be automated; `/login` in Telegram sends the link.

---

## 12. Gotchas and lessons learned

**Corporate network**
- Terminals need `HTTPS_PROXY`; without it, Dhan calls time out (WinError 10060). Set
  `NO_PROXY=127.0.0.1,localhost`.
- Node ignores the proxy unless `NODE_USE_ENV_PROXY=1`.
- `uv` needs `--native-tls`; it can't download Python 3.12, so the venvs use 3.14.

**OpenAlgo quirks**
- History timestamps are shifted by the host time zone (+4.5 h on this PC) and include the next day. This is
  corrected in `history.py` and the Order Flow provider.
- Depth-5 and depth-20 for the same symbol arrive on one topic. The 20-level packet has `depth_level=20` and **no LTP**,
  so it must not overwrite the price.
- `prev_oi` isn't forwarded, so the OI change baseline is the first value seen.
- Dhan OI is reported as quantity, not lots.

**Dhan limits**
- Quotes: 1 request/s, up to 1,000 instruments per call. History: 5 requests/s. 100k data requests a day.
- `rollingoption` accepts only 1-2 days per request: use `fromDate = toDate`.
- After about 10k requests at 3.5 requests/s, or about 61k in one day, Dhan throttled the user for hours (DH-904/805).
  The downloader now:
  - paces itself adaptively;
  - stops at 40k requests a day;
  - runs at 2 requests/s by default.
- All apps (FLOW, Order Flow, Option Hub, scanner) **share one Dhan rate limit**.

**Backtesting**
- Never fill a stop at the stop bar's close. Bad prints on fast moves inflated the sell results until the fill model
  was fixed (wing sold at the stop bar's open or last price).
- Zerodha's report has no side column, so shorts look like longs.

**Tooling**
- An async terminal can drop a preceding `cd`. Order Flow's `npm run build` hangs, so use `npx vite build`.
- Telegram: the scanner must use its **own** bot token. Sharing OpenAlgo's gives HTTP 409.
- Don't build workarounds for the NSE website's bot check.

---

## 13. Current state

**Code and data**
- Code is complete and tested: 51 Python tests pass. Option Hub's tests for the changed code pass.
- Research data is complete to 30 Oct 2026; later days depend on DATA top-ups.

**Running and recording**
- **Nothing is running in the background:** all processes were stopped on 9 Nov.
- Live trading is **off** everywhere: the scanner sends alerts only, the harvester is paper, 1Cliq is disabled.
- Harvester paper history is unknown; check `python -m scalper.harvest --report`.
- Depth recordings exist only for the sessions in which FLOW ran live with recording on.

**Deployment**
- The VPS kit is ready; deployment is not confirmed.
- Windows scheduled tasks were removed on 1 Nov.

---

## 14. Next steps

In suggested priority order.

1. **Make the harvester forward test run every day.** It is the only evidence-backed idea.
   - Either deploy to the Oracle VPS (README section 12) or reinstall the Windows tasks.
   - The daily login is the bottleneck: `/login` in Telegram.
   - Goal: 8-10 closed paper trades, then compare with the backtest (+0.31R test, +0.06-0.10R train).
2. **Guardrails for the trader's manual trading.** This is the biggest lever according to the report analysis.
   - Proposed but not built: a risk monitor that enforces:
     - no adding to losers;
     - a maximum size per trade;
     - a daily loss stop;
     - a maximum number of trades per day;
     - optionally, no new expiry-day entries after 12:30.
   - OpenAlgo has a starting point in `services/scalping_risk_monitor_service.py` (stop, target, trailing; no
     partials) and a Telegram `/closeall`.
   - Decide whether it watches all three brokers, which would need separate OpenAlgo instances.
3. **Order-book (DOM) study.** After 2-4 weeks of depth recordings, write `scalper/study_dom.py`: do OFI, CVD or
   absorption predict the next 10 s, 1 min or 5 min, net of costs? Requires FLOW running live during market hours.
4. **Keep data current:** DATA button after each close; don't download during market hours.
5. **Option Hub:**
   - Move the NSE-blocked panels the trader actually uses to Dhan/OpenAlgo/archives. For example, F&O movers from
     OpenAlgo multiquotes, VIX history from Dhan daily candles, holidays from OpenAlgo. **Ask which panels matter.**
   - Pace the other Dhan callers in `server/brokers/dhan*.mjs`; occasional 429/805 responses remain.
6. **1Cliq live (deferred by the trader):** if wanted, add a guarded order layer through OpenAlgo with:
   - fixed size;
   - an automatic broker-side stop;
   - no averaging;
   - a daily stop;
   - Analyze (paper) mode first.

   Under SEBI's retail-algo rules, order APIs need a whitelisted static IP.
7. **More brokers:** Kotak and Zerodha as separate OpenAlgo instances on the VPS, only if needed.
8. **New strategy ideas:** test with the `india-strategy-test` skill on the stored data before anything goes near
   the scanner. Pre-register rules, use a train/test split and honest fills.

---

## 15. Rules for the next agent

1. **No live orders** without the trader's explicit go-ahead and paper evidence first.
2. **Never print or copy secrets** from `env/dhan.env`, `openalgo-main/.env`, OpenAlgo's database or Option Hub
   credentials.
3. **Backtest honestly:** realistic fills (`scalper/execution.py`), costs, a train/test split and neighbour
   robustness. Report negative results plainly. The trader's ideas were tested and rejected; say so if asked again.
4. **Respect Dhan's limits**, which are shared across all apps. No bulk downloads during market hours.
5. **No anti-bot workarounds** (NSE website). Use licensed broker data or official archives.
6. **Keep OpenAlgo unmodified;** integrate via REST/WebSocket.
7. **Run Python tests** with `.venv\Scripts\python.exe -m pytest -q tests`.
8. The trader prefers short, direct answers and wants changes implemented rather than only suggested.

---

## 16. File map

```
analyse.py, expiry_edge.py, mirror_test.py   report analysis -> out/
config/dhan.yaml                             scanner, harvest, flow config (vps.yaml generated on server)
env/dhan.env                                 SECRETS (OpenAlgo key, Telegram); example in env/dhan.env.example
scalper/                                     scanner, harvester, research, control bot
scalper/flow/                                FLOW dashboard backend (model, engine, sources, server, tape,
                                             bounce, record, updater, breadth)
web/flow/app.js                              FLOW dashboard frontend (canvas)
tools/                                       dhan_download, audit_data, dhan_probe, run_option_hub,
                                             Windows task scripts
deploy/vps/                                  Oracle VPS setup script and systemd units
data/market/index|options                    5-year Dhan data (parquet)
data/research/                               research outputs
data/journal.sqlite                          scanner and harvester journal
data/flow/depth/                             order-book recordings
.github/skills/                              india-strategy-test, india-harvest-ops, india-event-check
vendor/day-trader-skill/                     MIT skill pack (evaluated)
openalgo-main/                               OpenAlgo (unmodified, own .venv, port 5001)
order-flow-main/                             MrChartist Order Flow + our openalgo provider (port 8790)
india-s-best-option-hub-main/                MrChartist Option Hub + Dhan patches (ports 4001/4002)
README.md                                    full operating manual (sections 1-18)
```
