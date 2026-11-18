"""What if every trade had been mirrored? Replays the broker history several ways and looks for rules that fit.

  .venv\\Scripts\\python.exe mirror_test.py        -> out/mirror_report.txt, out/mirror_pairs.csv

A. SELL INSTEAD OF BUY (all brokers): short the same contract at your entry, buy back at your exit. Exact from your
   own fills: mirror gross = -your gross, minus crossing the spread on both legs again; charges get STT on the new
   sell leg. Also how much of it comes from a handful of trades and the margin the biggest shorts would have needed.
B. FLIP DIRECTION on stored 1-minute chains (Zerodha NIFTY/SENSEX fill pairs on the nearest weekly, ATM+-10):
   your trade, the same option sold, the opposite option bought (same distance from spot on the other side) and
   the opposite option sold - all priced on the same bars (linear inside the minute, bid/ask, charges), so they
   compare like for like. Plus whether the index went your way while you held.
C. YOUR OWN ENTRIES WITH RULES (Zerodha, exact timestamps): no averaging down, fixed premium per trade, expiry day,
   time window, and on B's bars a hard stop / time stop.
Eras: FY22-24 (where the rules come from) vs FY25+ (the check).
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from expiry_edge import SYM, SYM_M, zerodha_expiry
from scalper.backtest import EXCHANGES, ROOT, STEP, TICK, _index, clean_chain, expiry_calendar, next_expiry
from scalper.execution import charges as fees

OUT = Path("out")
HS = {"NIFTY": .001, "SENSEX": .0025, "BANKNIFTY": .001, "FINNIFTY": .0015, "MIDCPNIFTY": .0025}
EXCH = {"SENSEX": "BFO", "BANKEX": "BFO"}
MARGIN_PCT = 0.09
SPLIT = pd.Timestamp("2024-04-01")
N = 375
FIXED_RS = 25_000.0
CAP_RS = 2_500.0


def _hs(und, px):
    return np.maximum(TICK, np.asarray(px, float) * pd.Series(und).map(HS).fillna(.002).to_numpy())


def _stt(dates: pd.Series) -> np.ndarray:
    d = dates.dt.strftime("%Y-%m-%d").fillna("2030-01-01")
    return np.select([d < "2023-04-01", d < "2024-10-01"], [0.0005, 0.000625], 0.001)


def _era(dates: pd.Series) -> pd.Series:
    return pd.Series(np.where(dates < SPLIT, "FY22-24", "FY25+"), index=dates.index)


def _parse(sym: str) -> tuple[str, float, str]:
    m = SYM.match(sym) or SYM_M.match(sym)
    return (m.group(1), float(m.group(m.lastindex - 1)), m.group(m.lastindex)) if m else ("", np.nan, "")


def _tbl(df: pd.DataFrame) -> str:
    return df.to_string(float_format=lambda x: f"{x:,.0f}")


# ---------------------------------------------------------------- A
def mirror(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    b, s = df.buy_value / df.qty, df.sell_value / df.qty
    df["extra"] = 2 * (_hs(df.underlying, b) + _hs(df.underlying, s)) * df.qty
    df["m_charges"] = df.charges + _stt(df.date) * (df.buy_value - df.sell_value)
    df["m_free"] = -df.gross - df.m_charges
    df["m_net"] = df.m_free - df.extra
    return df


def section_a(z: pd.DataFrame, d: pd.DataFrame, k: pd.DataFrame) -> list[str]:
    out = ["=" * 110, "A. SELL INSTEAD OF BUY - same contract, same entry and exit times, every broker", "=" * 110]
    rows = []
    allm = []
    for name, df in (("Zerodha (episodes)", z), ("Dhan (day/contract)", d), ("Kotak (contract/FY)", k)):
        m = mirror(df)
        m["era"] = _era(m.date)
        allm.append(m.assign(broker=name))
        for era, g in [("all", m), *m.groupby("era")]:
            top = g.m_net.sort_values(ascending=False)
            rows.append({"broker": name, "era": era, "trades": len(g), "your net": g.net.sum(),
                         "mirror, no extra spread": g.m_free.sum(), "mirror net": g.m_net.sum(),
                         "mirror win %": 100 * (g.m_net > 0).mean(), "mirror median trade": g.m_net.median(),
                         "mirror minus its best 10": top.iloc[10:].sum(), "mirror worst": g.m_net.min()})
    out.append(_tbl(pd.DataFrame(rows).set_index(["broker", "era"])))
    m = pd.concat(allm, ignore_index=True)
    s = m.m_net.sort_values(ascending=False).reset_index(drop=True)
    out.append(f"\nAll brokers: your net {m.net.sum():,.0f} -> mirror net {s.sum():,.0f} "
               f"(extra spread {m.extra.sum():,.0f}, charges {m.m_charges.sum():,.0f})")
    for n in (1, 5, 10, 25, 50):
        out.append(f"   mirror without its best {n:3d} rows ({n / len(s):.1%} of {len(s)}): {s.iloc[n:].sum():,.0f}")
    out.append(f"   mirror profit made by the best 1% of rows: {s.iloc[:len(s) // 100].sum():,.0f}; "
               f"the other 99% together: {s.iloc[len(s) // 100:].sum():,.0f}")
    ze = mirror(z)
    ze["hold"] = pd.cut(ze.hold_min, [-1, 1, 3, 5, 10, 60, 1e9], labels=["<1m", "1-3m", "3-5m", "5-10m", "10-60m", ">1h"])
    out.append("\nZerodha by holding time (episodes):")
    out.append(_tbl(ze.groupby("hold", observed=True).agg(trades=("net", "size"), your_net=("net", "sum"),
                                                          your_win=("net", lambda x: 100 * (x > 0).mean()),
                                                          mirror_net=("m_net", "sum"),
                                                          mirror_win=("m_net", lambda x: 100 * (x > 0).mean()))))
    ze["strike"] = ze.symbol.map(lambda x: _parse(x)[1])
    ze["margin"] = ze.qty * ze.strike * MARGIN_PCT
    big = ze.sort_values("m_net", ascending=False).head(10)
    out.append(f"\nMargin a naked short needs (~{MARGIN_PCT:.0%} of strike x qty): median {ze.margin.median():,.0f}, "
               f"90th pct {ze.margin.quantile(.9):,.0f}, max {ze.margin.max():,.0f}; "
               f"{(ze.margin > 10e5).sum()} episodes above Rs 10 lakh")
    out.append("The 10 best mirrored Zerodha episodes (= your 10 worst) and the margin each short needed:")
    out.append(big[["symbol", "entry_time", "qty", "avg_buy", "avg_sell", "n_entries", "net", "m_net", "margin"]]
               .to_string(index=False, float_format=lambda x: f"{x:,.0f}"))
    return out


# ---------------------------------------------------------------- B
class Day:
    def __init__(self, name: str, day, ix: pd.DataFrame):
        f = ROOT / "options" / name / "chain" / f"{day}.parquet"
        self.ok = False
        if not f.exists():
            return
        ch, q = clean_chain(name, pd.read_parquet(f), ix[ix.day == day])
        if q["atm_cov"] < 0.9:
            return
        ch = ch.assign(i=((ch.ts + 19800) % 86400) // 60 - 555)
        ch = ch[(ch.i >= 0) & (ch.i < N)]
        self.spot = ch.groupby("i").spot.first().reindex(range(N)).ffill().to_numpy(float)
        self.bars = {}
        for (k, opt), g in ch.groupby(["strike", "opt"]):
            g = g.set_index("i").reindex(range(N))
            self.bars[(float(k), opt)] = (g.open.to_numpy(float), g.high.to_numpy(float), g.low.to_numpy(float),
                                          g.close.to_numpy(float))
        self.ok = True

    def px(self, key, i: int, f: float) -> float:
        b = self.bars.get(key)
        if b is None or not 0 <= i < N or np.isnan(b[0][i]) or np.isnan(b[3][i]):
            return np.nan
        return float(b[0][i] + (b[3][i] - b[0][i]) * f)


def section_b(zf: pd.DataFrame) -> tuple[list[str], pd.DataFrame]:
    rows = []
    for name in ("NIFTY", "SENSEX"):
        cal, ix, step, exch = expiry_calendar(name), _index(name), STEP[name], EXCHANGES[name][1]
        hs = lambda p: max(TICK, p * HS[name])
        sub = zf[zf.underlying == name]
        for day, g in sub.groupby(sub.entry_time.dt.date):
            if not cal or day < cal[0]:
                continue
            exp = next_expiry(cal, day)
            g = g[g.expiry.dt.date == exp]
            if g.empty:
                continue
            D = Day(name, day, ix)
            if not D.ok:
                continue
            for r in g.itertuples():
                te, tx = r.entry_time, r.exit_time
                ie, ixx = te.hour * 60 + te.minute - 555, tx.hour * 60 + tx.minute - 555
                fe, fx = te.second / 60, tx.second / 60
                if tx.date() != day or not (0 <= ie <= ixx < N):
                    continue
                se, sx = D.spot[ie], D.spot[ixx]
                kk = round((2 * se - r.strike) / step) * step
                opp = "PE" if r.opt == "CE" else "CE"
                pe, px = D.px((r.strike, r.opt), ie, fe), D.px((r.strike, r.opt), ixx, fx)
                qe, qx = D.px((kk, opp), ie, fe), D.px((kk, opp), ixx, fx)
                if np.isnan([pe, px, qe, qx, se, sx]).any() or min(pe, px, qe, qx) <= 0:
                    continue
                q = r.qty

                def pnl(buy_px, sell_px):
                    return (sell_px - buy_px) * q - fees(exch, day, buy_px * q, sell_px * q, 2)

                def trade(e, x, side):
                    return pnl(e + hs(e), x - hs(x)) if side > 0 else pnl(x + hs(x), e - hs(e))

                ab, asl = r.buy_value / q, r.sell_value / q
                # Zerodha lists the opening fill as "entry" for shorts too: the side is whichever matches the bars
                sd = 1 if abs(pe - ab) + abs(px - asl) <= abs(pe - asl) + abs(px - ab) else -1
                view = (1 if r.opt == "CE" else -1) * sd
                row = {"day": day, "name": name, "symbol": r.symbol, "entry_time": te, "hold_min": (tx - te).total_seconds() / 60,
                       "qty": q, "opt": r.opt, "side": "long" if sd > 0 else "short", "first": r.first,
                       "expiry_day": day == exp, "actual": r.net,
                       "you": trade(pe, px, sd), "reverse": trade(pe, px, -sd),
                       "other option, same side": trade(qe, qx, sd), "other option, other side": trade(qe, qx, -sd),
                       "index pts your way": (sx - se) * view}
                o, _, lo, _ = D.bars[(r.strike, r.opt)]
                entry = pe + hs(pe)
                for s in (0.10, 0.20):
                    out_px = px - hs(px)
                    for i in range(ie + 1, ixx + 1):
                        if not np.isnan(lo[i]) and lo[i] - hs(lo[i]) <= entry * (1 - s):
                            out_px = min(o[i] - hs(o[i]), entry * (1 - s))
                            break
                    row[f"stop {s:.0%}"] = pnl(entry, out_px) if sd > 0 else np.nan
                for t in (1, 3):
                    p = D.px((r.strike, r.opt), ie + t, fe) if row["hold_min"] > t else np.nan
                    row[f"exit after {t}m"] = (np.nan if sd < 0 else row["you"] if np.isnan(p) else pnl(entry, p - hs(p)))
                rows.append(row)
    b = pd.DataFrame(rows)
    out = ["", "=" * 110, "B. FLIP DIRECTION - Zerodha NIFTY/SENSEX fill pairs re-priced on stored 1-minute chains", "=" * 110]
    if b.empty:
        return out + ["no pairs matched stored chains"], b
    b["era"] = _era(pd.to_datetime(b.day))
    b["hold"] = pd.cut(b.hold_min, [-1, 2, 5, 15, 1e9], labels=["<2m", "2-5m", "5-15m", ">15m"])
    out.append("Zerodha's report has no buy/sell column and lists the opening fill as 'entry' for shorts too; matching"
               " your fill prices to the bars, these pairs were SHORTS: "
               + ", ".join(f"{n} {e} {100 * (g.side == 'short').mean():.0f}%" for (n, e), g in b.groupby(["name", "era"])))
    out.append("Model check (1-minute bars cannot see inside a minute): correlation of modelled vs your real P&L by hold")
    out.append("   " + " | ".join(f"{h} {g.actual.corr(g.you):.2f} (n {len(g)})" for h, g in b.groupby("hold", observed=True)))
    s = b[b.hold_min >= 2]
    cols = ["actual", "you", "reverse", "other option, same side", "other option, other side"]
    g = s.groupby(["name", "era", "side"])
    t = g[cols].sum()
    t.insert(0, "pairs", g.size())
    t["index went your way %"] = g["index pts your way"].apply(lambda x: 100 * (x > 0).mean())
    out.append("\nPairs held >= 2 minutes, rupees after spread and charges. 'actual' = your real P&L, 'you' = the model of it,"
               "\n'reverse' = other side of the same option, 'other option, same side' = e.g. buy the PE instead of the CE"
               "\n(opposite view), 'other option, other side' = e.g. sell the PE instead of buying the CE (same view):")
    out.append(_tbl(t))
    out.append("Totals: " + " | ".join(f"{c} {v:,.0f}" for c, v in s[cols].sum().items()))
    out.append(f"Index moved your way while you held (>= 2m): {100 * (s['index pts your way'] > 0).mean():.0f}% "
               f"(against {100 * (s['index pts your way'] < 0).mean():.0f}%), avg {s['index pts your way'].mean():+.1f} pts")
    return out, b


# ---------------------------------------------------------------- C
def _fixed(ret: pd.Series, und: pd.Series, dates: pd.Series) -> list[float]:
    return [r * FIXED_RS - fees(EXCH.get(u, "NFO"), (d if pd.notna(d) else SPLIT).date(), FIXED_RS, FIXED_RS * (1 + r), 2)
            for r, u, d in zip(ret, und, dates)]


def _rule_table(rules) -> pd.DataFrame:
    rows = []
    for label, df, col in rules:
        for era, g in df.groupby("era"):
            x = g[col]
            rows.append({"rule": label, "era": era, "trades": len(g), "win %": 100 * (x > 0).mean(), "net": x.sum(),
                         "avg/trade": x.mean(), "t": x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 else np.nan,
                         "minus best 10": x.sort_values().iloc[:-10].sum(), "worst": x.min()})
    return pd.DataFrame(rows).set_index(["rule", "era"])


def section_c(z: pd.DataFrame, zf: pd.DataFrame, b: pd.DataFrame, d: pd.DataFrame, k: pd.DataFrame) -> list[str]:
    out = ["", "=" * 110, "C. YOUR OWN ENTRIES WITH RULES (Zerodha)", "=" * 110]
    first = zf[zf["first"]].groupby("episode").agg(gross=("gross", "sum"), buy_value=("buy_value", "sum"),
                                                   sell_value=("sell_value", "sum"), charges=("charges", "sum"),
                                                   net=("net", "sum"))
    ep = z.set_index("episode").join(first.add_suffix("_first"))
    ep["ret"] = ep.gross_first / ((ep.buy_value_first + ep.sell_value_first) / 2)
    ep["fixed"] = _fixed(ep.ret, ep.underlying, ep.date)
    t = ep.entry_time.dt.hour * 60 + ep.entry_time.dt.minute
    ep["morning"] = t < 12 * 60 + 30
    ep["expiry_day"] = ep.date == ep.symbol.map(zerodha_expiry)
    ep["era"] = _era(ep.date)
    ep = ep.sort_values("entry_time")
    day_pnl = ep.groupby("date").fixed.transform(lambda x: x.cumsum().shift(fill_value=0))
    day_losses = ep.groupby("date").fixed.transform(lambda x: (x <= 0).cumsum().shift(fill_value=0))
    ep["capped"] = (day_pnl > -2 * CAP_RS) & (day_losses < 3)
    rules = [("R0 as traded", ep, "net"),
             ("R1 no averaging down (first entry only)", ep, "net_first"),
             (f"R2 R1 + same Rs {FIXED_RS:,.0f} premium every trade", ep, "fixed"),
             ("R3 R2 + entries before 12:30", ep[ep.morning], "fixed"),
             ("R4 R2 + expiry day only", ep[ep.expiry_day], "fixed"),
             ("R5 R2 + expiry day + before 12:30", ep[ep.expiry_day & ep.morning], "fixed"),
             ("R6 R2 + NOT expiry day", ep[~ep.expiry_day], "fixed"),
             (f"R7 R3 + day stop (-Rs {2 * CAP_RS:,.0f} or 3 losers)", ep[ep.morning & ep.capped], "fixed")]
    out.append(_tbl(_rule_table(rules)))
    out.append(f"\nSame check on the other brokers (each row sized to Rs {FIXED_RS:,.0f} premium; Dhan = one contract on one day,"
               " Kotak = one contract over a year, so only a rough equal-size view):")
    other = []
    for name, df, col in (("Dhan as traded", d, "net"), ("Dhan same size", d, "fixed"), ("Kotak as traded", k, "net"),
                          ("Kotak same size", k, "fixed")):
        df = df[df.buy_value > 0].copy()
        df["fixed"] = _fixed(df.gross / df.buy_value, df.underlying, df.date)
        df["era"] = _era(df.date)
        other.append((name, df, col))
    out.append(_tbl(_rule_table(other)))
    av = ep[ep.n_entries > 1]
    out.append(f"\nEpisodes where you added to the position (more than one entry time): {len(av)} ({len(av) / len(ep):.0%}), "
               f"net {av.net.sum():,.0f}; their first entries alone: {av.net_first.sum():,.0f}")
    big = ep.buy_value > ep.buy_value.quantile(.9)
    out.append(f"Biggest 10% of trades by premium: net {ep[big].net.sum():,.0f}, win {100 * (ep[big].net > 0).mean():.0f}%; "
               f"the other 90%: net {ep[~big].net.sum():,.0f}, win {100 * (ep[~big].net > 0).mean():.0f}%")
    if not b.empty:
        cols = ["you", "stop 10%", "stop 20%", "exit after 1m", "exit after 3m"]
        out.append("\nExit rules on your LONG NIFTY/SENSEX pairs held >= 2 minutes (model rupees after costs):")
        g = b[(b.hold_min >= 2) & (b.side == "long")].groupby("era")
        t = g[cols].sum()
        t.insert(0, "pairs", g.size())
        out.append(_tbl(t))
    return out


def main() -> int:
    z = pd.read_csv(OUT / "zerodha_episodes.csv", parse_dates=["entry_time", "exit_time", "date"])
    zf = pd.read_csv(OUT / "zerodha_fills.csv", parse_dates=["entry_time", "exit_time"])
    d = pd.read_csv(OUT / "dhan_trades.csv", parse_dates=["date"])
    k = pd.read_csv(OUT / "kotak_contracts.csv", parse_dates=["date"])
    zf = zf.sort_values(["symbol", "entry_time", "exit_time"]).reset_index(drop=True)
    ep, last, end, ids = -1, None, None, []
    for sym, et, xt in zip(zf.symbol, zf.entry_time, zf.exit_time):
        if sym != last or et > end:
            ep, end = ep + 1, xt
        else:
            end = max(end, xt)
        last = sym
        ids.append(ep)
    zf["episode"] = ids
    zf["first"] = zf.entry_time == zf.groupby("episode").entry_time.transform("min")
    zf[["underlying", "strike", "opt"]] = pd.DataFrame(zf.symbol.map(_parse).tolist(), index=zf.index)
    zf["expiry"] = zf.symbol.map(zerodha_expiry)
    assert (z.symbol.to_numpy() == zf.groupby("episode").symbol.first().to_numpy()).all()

    out = section_a(z, d, k)
    ob, b = section_b(zf)
    out += ob + section_c(z, zf, b, d, k)
    text = "\n".join(out)
    (OUT / "mirror_report.txt").write_text(text, encoding="utf-8")
    if not b.empty:
        b.to_csv(OUT / "mirror_pairs.csv", index=False)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
