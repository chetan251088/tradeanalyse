"""Multi-day (positional) option research on daily snapshots of every downloaded session.

  .venv\\Scripts\\python.exe -m scalper.research_multi build     # one-off: data/research/panel.parquet

The panel holds, for every session and contract (nearest weekly expiry, ATM+-10 strikes, cleaned like
scalper.backtest), the last traded price at fixed times of day, plus the index and India VIX at those
times. Positions are priced from it at entry and exit times on different days (same contract), with the
half-spread model, one tick of slippage per leg and per side, and the scalper.execution charges.
Everything is defined-risk (wings bought), so the worst case per trade is known when it is opened.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from scalper.backtest import (
    EXCHANGES, HALF_SPREAD, LOT, ROOT, STEP, _index, clean_chain, expiry_calendar, next_expiry,
)
from scalper.execution import charges

OUT = Path("data/research")
PANEL = OUT / "panel.parquet"
TIMES = ["09:20", "10:00", "12:00", "14:00", "15:00", "15:25"]
TICK = 0.05
TRAIN_END = date(2024, 7, 1)


def _at_times(df: pd.DataFrame, hm: pd.Series, keys: list[str]) -> pd.DataFrame:
    """Last close at or before each snapshot time (within 5 minutes), plus the 09:15 open."""
    out = []
    first = df[hm == "09:15"].groupby(keys).open.first().rename("09:15o")
    out.append(first)
    for t in TIMES:
        lo = (pd.Timestamp(f"2000-01-01 {t}") - pd.Timedelta(minutes=5)).strftime("%H:%M")
        sel = df[(hm >= lo) & (hm <= t)].sort_values("ts")
        out.append(sel.groupby(keys).close.last().rename(t))
    return pd.concat(out, axis=1)


def build() -> pd.DataFrame:
    vix = _index("INDIAVIX")
    vix_hm = pd.to_datetime(vix.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
    vix_snap = _at_times(vix.rename(columns={"timestamp": "ts"}), vix_hm, ["day"])
    rows = []
    for name in ("NIFTY", "SENSEX"):
        index = _index(name)
        by_day = dict(tuple(index.groupby("day")))
        calendar = expiry_calendar(name)
        for f in sorted((ROOT / "options" / name / "chain").glob("*.parquet")):
            d = date.fromisoformat(f.stem)
            ix = by_day.get(d)
            if ix is None or len(ix) < 300:
                continue
            good, q = clean_chain(name, pd.read_parquet(f), ix)
            if good.empty:
                continue
            ihm = pd.to_datetime(ix.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
            isnap = _at_times(ix.rename(columns={"timestamp": "ts"}).assign(k=0), ihm, ["k"]).iloc[0]
            ghm = pd.to_datetime(good.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")
            snap = _at_times(good, ghm, ["opt", "strike"]).reset_index()
            snap = snap.assign(name=name, day=d, expiry=next_expiry(calendar, d), ok=q["atm_cov"] >= 0.9 and q["oi_cov"] >= 0.5)
            for t in ["09:15o", *TIMES]:
                snap[f"ix_{t}"] = isnap[t]
                snap[f"vix_{t}"] = vix_snap.loc[d, t] if d in vix_snap.index else np.nan
            snap["ix_close"] = float(ix.close.iloc[-1])
            rows.append(snap)
        print(f"{name}: {sum(1 for r in rows if r.name.iloc[0] == name)} sessions")
    panel = pd.concat(rows, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(PANEL, index=False)
    return panel


def load() -> pd.DataFrame:
    return pd.read_parquet(PANEL)


# ------------------------------------------------------------------ pricing helpers ----------
@dataclass(frozen=True)
class Cost:
    spread_mult: float = 1.0
    slip_ticks: int = 1

    def hs(self, name: str, px: float) -> float:
        return max(TICK, px * HALF_SPREAD[name] * self.spread_mult)


class Book:
    """Price lookup: (name, day, opt, strike) -> last traded price at a snapshot time.

    A strike that did not trade near the snapshot is marked off that moment's volatility smile: implied
    vols of the out-of-the-money strikes that did trade, interpolated in strike (flat beyond the ends).
    """

    def __init__(self, panel: pd.DataFrame):
        self.p = panel.set_index(["name", "day", "opt", "strike"]).sort_index()
        days = panel.groupby(["name", "day"]).first()
        self.days = days
        self.sessions = {n: sorted(g.index.get_level_values("day")) for n, g in days.groupby(level="name")}
        self._smiles: dict = {}

    def index(self, name: str, day: date, t: str) -> float:
        return float(self.days.loc[(name, day), f"ix_{t}"])

    def tau(self, name: str, day: date, t: str) -> float:
        expiry = self.days.loc[(name, day), "expiry"]
        hh, mm = (9, 15) if t == "09:15o" else map(int, t.split(":"))
        start = pd.Timestamp(day) + pd.Timedelta(hours=hh, minutes=mm)
        end = pd.Timestamp(expiry) + pd.Timedelta(hours=15, minutes=30)
        return max((end - start).total_seconds(), 60.0) / (365 * 86400)

    def smile(self, name: str, day: date, t: str) -> tuple[np.ndarray, np.ndarray]:
        key = (name, day, t)
        if key not in self._smiles:
            from scalper.greeks import implied_vol

            rows = self.p.loc[(name, day)][t].dropna().reset_index()
            spot, tau = self.index(name, day, t), self.tau(name, day, t)
            otm = rows[((rows.opt == "CE") & (rows.strike >= spot)) | ((rows.opt == "PE") & (rows.strike <= spot))]
            iv = implied_vol(otm[t].to_numpy(float), np.full(len(otm), spot), otm.strike.to_numpy(float),
                             np.full(len(otm), tau), (otm.opt == "CE").to_numpy())
            ok = np.isfinite(iv)
            ks, vs = otm.strike.to_numpy(float)[ok], iv[ok]
            order = np.argsort(ks)
            self._smiles[key] = (ks[order], vs[order])
        return self._smiles[key]

    def price(self, name: str, day: date, opt: str, strike: float, t: str) -> tuple[float, bool]:
        try:
            v = float(self.p.loc[(name, day, opt, strike), t])
        except KeyError:
            v = np.nan
        if not np.isnan(v):
            return v, True
        from scalper.greeks import _price

        s = self.index(name, day, t)
        intrinsic = max(s - strike, 0.0) if opt == "CE" else max(strike - s, 0.0)
        ks, vs = self.smile(name, day, t)
        if len(ks) < 3:
            return max(intrinsic, TICK), False
        iv = float(np.interp(strike, ks, vs))
        model = float(_price(np.array(s), np.array(strike), np.array(self.tau(name, day, t)), np.array(iv),
                             np.array(opt == "CE")))
        return max(model, intrinsic, TICK), False


def fly_trade(book: Book, name: str, d_in: date, t_in: str, d_out: date, t_out: str, wing: int,
              offset: int = 0, cost: Cost = Cost(), R: float = 10_000.0, side: int = -1) -> dict | None:
    """Short (side=-1) or long (side=+1) iron fly / condor: shorts at ATM+-offset, wings `wing` strikes further out.

    The structure's mid value is clamped to its no-arbitrage range [0, width] (last trades of different legs
    can be seconds apart); spreads and slippage are then charged per leg on entry and exit.
    Results are per unit of width (net_w) and per unit of max loss at entry (net_r).
    """
    step = STEP[name]
    spot = book.index(name, d_in, t_in)
    if np.isnan(spot):
        return None
    atm = round(spot / step) * step
    legs = [("CE", atm + offset * step, -1), ("PE", atm - offset * step, -1),
            ("CE", atm + (offset + wing) * step, 1), ("PE", atm - (offset + wing) * step, 1)]
    slip = cost.slip_ticks * TICK
    p_in, p_out, found = [], [], True
    for opt, k, _ in legs:
        a, ok_in = book.price(name, d_in, opt, k, t_in)
        if not ok_in:
            return None
        b, ok_out = book.price(name, d_out, opt, k, t_out)
        p_in.append(a)
        p_out.append(b)
        found &= ok_out
    width = wing * step
    value = lambda px: min(max(px[0] + px[1] - px[2] - px[3], 0.0), width)  # noqa: E731  short straddle - wings
    v_in, v_out = value(p_in), value(p_out)
    friction = sum(cost.hs(name, x) + slip for x in p_in) + sum(cost.hs(name, x) + slip for x in p_out)
    pnl = side * (v_out - v_in) - friction  # short: v_in - v_out; long: v_out - v_in
    risk = (width - v_in) if side < 0 else v_in
    if risk <= 0.02 * width:
        return None
    qty = R / width
    direction = [s * -side for _, _, s in legs]  # +1 bought at entry, -1 sold at entry
    buys = sum(x for d, x in zip(direction, p_in) if d > 0) + sum(x for d, x in zip(direction, p_out) if d < 0)
    sells = sum(p_in) + sum(p_out) - buys
    cst = charges(EXCHANGES[name][1], d_in, qty * buys, qty * sells, 8)
    lot = LOT[name]
    lot_cst = charges(EXCHANGES[name][1], d_in, lot * buys, lot * sells, 8)
    return {"name": name, "day": d_in, "exit_day": d_out, "t_in": t_in, "t_out": t_out, "spot": spot, "atm": atm,
            "credit": round(v_in, 2), "exit_value": round(v_out, 2), "width": width, "risk": round(risk, 2),
            "credit_w": round(v_in / width, 3), "pnl_pts": round(pnl, 2),
            "edge_w": side * (v_out - v_in) / width, "fric_w": friction / width, "charge_w": cst / R,
            "net_w": (qty * pnl - cst) / R, "net_r": (qty * pnl - cst) / (qty * risk),
            "cost_w": (qty * friction + cst) / R, "rs_per_lot": round(lot * pnl - lot_cst),
            "risk_per_lot": round(lot * risk), "exit_found": found}


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if r.empty:
        return {"n": 0}
    eq = r.cumsum()
    loss = -r[r < 0].sum()
    return {"n": len(r), "win%": round((r > 0).mean() * 100), "avg": round(r.mean(), 4),
            "total": round(r.sum(), 2), "PF": round(r[r > 0].sum() / loss, 2) if loss > 0 else np.inf,
            "t": round(r.mean() / (r.std(ddof=1) / np.sqrt(len(r))), 2) if len(r) > 2 else np.nan,
            "maxDD": round(float((eq.cummax().clip(lower=0) - eq).max()), 2), "worst": round(r.min(), 3)}


def split(df: pd.DataFrame, col: str = "net_w") -> pd.DataFrame:
    df = df.sort_values(["day", "name"])
    df = df.assign(period=np.where(pd.to_datetime(df.day) < pd.Timestamp(TRAIN_END), "train", "test"))
    return pd.DataFrame({p: stats(g[col]) for p, g in df.groupby("period")}).T


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("which", choices=["build"])
    ap.parse_args()
    panel = build()
    print(f"panel: {len(panel)} rows, {panel.groupby(['name', 'day']).ngroups} sessions -> {PANEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
