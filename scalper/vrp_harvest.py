"""VRP Harvester: sell the weekly ATM straddle when volatility is rich, with far wings as crash insurance.

  .venv\\Scripts\\python.exe -m scalper.vrp_harvest

Pre-registered rules (no parameter was fitted on the test period):
  * one position per index per weekly expiry, opened at 15:25 `dte` sessions before expiry, held to expiry 15:25
  * only when India VIX at entry >= its own median of the previous 250 sessions (volatility is rich)
  * sell the ATM call + put, buy wings 2x the implied move (straddle price) away, at least 4 strikes
  * size: worst case (wing width - credit) x quantity <= risk_pct of current capital, whole lots only
Pricing, spreads, slippage and charges as in scalper.research_multi (untraded strikes marked off the smile).
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from scalper.backtest import EXCHANGES, LOT
from scalper.execution import charges
from scalper.research_multi import OUT, STEP, TICK, Book, Cost, load, stats

LOT_HISTORY = {  # approximate exchange lot sizes by contract date
    "NIFTY": [("2021-01-01", 50), ("2024-04-26", 25), ("2024-11-20", 75), ("2025-12-30", 65)],
    "SENSEX": [("2021-01-01", 10), ("2024-11-20", 20)],
}


def lot_size(name: str, day) -> int:
    size = LOT[name]
    for start, n in LOT_HISTORY[name]:
        if str(day) >= start:
            size = n
    return size


def trades(book: Book, dte_entry: int, wing_mult: float = 2.0, vix_filter: bool = True,
           cost: Cost = Cost(), vix_q: float = 0.5) -> pd.DataFrame:
    ok = book.days["ok"]
    vix = book.days["vix_15:25"].astype(float)
    rows = []
    for name in ("NIFTY", "SENSEX"):
        ss = book.sessions[name]
        vix_med = vix.loc[name].rolling(250, min_periods=120).quantile(vix_q).shift(1)
        step = STEP[name]
        for i, d in enumerate(ss):
            exp = book.days.loc[(name, d), "expiry"]
            if exp not in ss or exp <= d or not ok[(name, d)] or not ok[(name, exp)]:
                continue
            if sum(1 for x in ss[i + 1:] if x <= exp) != dte_entry:
                continue
            v, med = vix.loc[(name, d)], vix_med.get(d, np.nan)
            rich = bool(v >= med) if not np.isnan(med) else False
            if vix_filter and not rich:
                continue
            s0 = book.index(name, d, "15:25")
            k = round(s0 / step) * step
            px = lambda dd, o, kk: book.price(name, dd, o, kk, "15:25")  # noqa: E731
            (ce, f1), (pe, f2) = px(d, "CE", k), px(d, "PE", k)
            if not (f1 and f2):
                continue
            prem = ce + pe
            w = min(max(4, round(wing_mult * prem / step)), 10)
            legs = [("CE", k, -1), ("PE", k, -1), ("CE", k + w * step, 1), ("PE", k - w * step, 1)]
            p_in = [px(d, o, kk)[0] for o, kk, _ in legs]
            p_out = [px(exp, o, kk)[0] for o, kk, _ in legs]
            width = w * step
            val = lambda p: min(max(p[0] + p[1] - p[2] - p[3], 0.0), width)  # noqa: E731
            v_in, v_out = val(p_in), val(p_out)
            fric = sum(cost.hs(name, x) + cost.slip_ticks * TICK for x in p_in + p_out)
            pnl_unit = v_in - v_out - fric
            buys = p_in[2] + p_in[3] + p_out[0] + p_out[1]
            sells = p_in[0] + p_in[1] + p_out[2] + p_out[3]
            rows.append({"name": name, "day": d, "expiry": exp, "spot": s0, "atm": k, "vix": v, "vix_med": med,
                         "prem": prem, "wing_k": w, "width": width, "credit": v_in, "exit_value": v_out,
                         "pnl_unit": pnl_unit, "max_loss_unit": width - v_in + fric, "buys": buys, "sells": sells,
                         "move_pct": abs(book.index(name, exp, "15:25") / s0 - 1) * 100})
    df = pd.DataFrame(rows)
    if not df.empty:
        df["period"] = np.where(pd.to_datetime(df.day) < pd.Timestamp("2024-07-01"), "train", "test")
        df["ret_risk"] = df.pnl_unit / df.max_loss_unit
    return df


def simulate(df: pd.DataFrame, capital: float = 500_000.0, risk_pct: float = 2.0) -> pd.DataFrame:
    """Whole-lot sizing on the capital at entry; positions on the same expiry week share that capital."""
    eq, rows = capital, []
    for _, t in df.sort_values(["day", "name"]).iterrows():
        lot = lot_size(t["name"], t.day)
        per_lot = t.max_loss_unit * lot
        lots = int(eq * risk_pct / 100 // per_lot)
        if lots == 0:
            rows.append({**t, "lots": 0, "pnl_rs": 0.0, "equity": eq})
            continue
        qty = lots * lot
        cst = charges(EXCHANGES[t["name"]][1], t.day, qty * t.buys, qty * t.sells, 8)
        pnl = qty * (t.pnl_unit + 0.0) - cst
        eq += pnl
        rows.append({**t, "lots": lots, "pnl_rs": pnl, "equity": eq})
    return pd.DataFrame(rows)


def report(sim: pd.DataFrame, capital: float) -> dict:
    eq = sim.equity
    dd = (eq.cummax() - eq) / eq.cummax()
    years = (pd.Timestamp(sim.day.iloc[-1]) - pd.Timestamp(sim.day.iloc[0])).days / 365.25
    traded = sim[sim.lots > 0]
    return {"trades": len(traded), "final_rs": round(eq.iloc[-1]), "CAGR%": round(((eq.iloc[-1] / capital) ** (1 / years) - 1) * 100, 1),
            "maxDD%": round(dd.max() * 100, 1), "win%": round((traded.pnl_rs > 0).mean() * 100),
            "worst_trade_rs": round(traded.pnl_rs.min()), "worst_trade_%eq": round((traded.pnl_rs / (traded.equity - traded.pnl_rs)).min() * 100, 2)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capital", type=float, default=500_000)
    ap.add_argument("--risk-pct", type=float, default=2.0)
    args = ap.parse_args()
    pd.set_option("display.width", 230)
    book = Book(load())
    for stress in (Cost(), Cost(2.0, 2)):
        tag = "base costs" if stress.spread_mult == 1 else "stress: 2x spreads, 2 ticks slippage"
        print(f"\n######## {tag}")
        for dte in (1, 2, 3, 4):
            for filt in (True, False):
                df = trades(book, dte, vix_filter=filt, cost=stress)
                if df.empty:
                    continue
                per = pd.DataFrame({p: stats(g.sort_values("day").ret_risk) for p, g in df.groupby("period")}).T
                sim = simulate(df, args.capital, args.risk_pct)
                rep = report(sim, args.capital)
                print(f"\n== entry {dte} session(s) before expiry, VIX filter {'ON' if filt else 'off'} | per-trade return on max loss:")
                print(per[["n", "win%", "avg", "t", "worst"]].to_string())
                print(f"   Rs {args.capital:,.0f} at {args.risk_pct}% max loss per trade -> {rep}")
                if stress.spread_mult == 1 and filt:
                    sim.to_csv(OUT / f"vrp_dte{dte}.csv", index=False)
                    by_year = sim.groupby(pd.to_datetime(sim.day).dt.year).agg(trades=("lots", lambda x: (x > 0).sum()), pnl_rs=("pnl_rs", "sum"))
                    print("   by year: " + ", ".join(f"{y}: {int(r.trades)} trades Rs {r.pnl_rs:,.0f}" for y, r in by_year.iterrows()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
