"""Run the positional strategy studies on the panel and write trade lists to data/research/.

  .venv\\Scripts\\python.exe -m scalper.research_runs
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scalper.research_multi import OUT, STEP, Book, Cost, fly_trade, load, split, stats


def realized_vol(book: Book, name: str, n: int = 10) -> dict:
    c = book.days.loc[name]["ix_15:25"].astype(float)
    return np.log(c).diff().rolling(n).std().to_dict()


def trades(book: Book, cost: Cost = Cost()) -> pd.DataFrame:
    """Every (session, horizon, wing, side) combination of the positional studies."""
    ok = book.days["ok"]
    rows = []
    for name in ("NIFTY", "SENSEX"):
        ss = book.sessions[name]
        rv = realized_vol(book, name)
        for i, d in enumerate(ss):
            exp = book.days.loc[(name, d), "expiry"]
            if not ok[(name, d)] or exp < d:
                continue
            dte = sum(1 for x in ss[i + 1:] if x <= exp)
            spot = book.index(name, d, "15:25")
            atm = round(spot / STEP[name]) * STEP[name]
            ce, _ = book.price(name, d, "CE", atm, "15:25")
            pe, _ = book.price(name, d, "PE", atm, "15:25")
            implied = (ce + pe) / spot
            realized = rv.get(d, np.nan) * np.sqrt(max(dte, 1)) * 0.8
            ctx = {"dte": dte, "vrp": implied / realized if realized > 0 else np.nan,
                   "vix": book.days.loc[(name, d), "vix_15:25"], "weekend": False}
            plans = []
            if i + 1 < len(ss) and ss[i + 1] <= exp and ok[(name, ss[i + 1])]:
                nxt = ss[i + 1]
                plans.append(("overnight", nxt, "10:00", {"weekend": (nxt - d).days >= 3}))
            if exp in ss and exp > d and ok[(name, exp)] and dte <= 4:
                plans.append(("to_expiry", exp, "15:25", {}))
            for horizon, d_out, t_out, extra in plans:
                for wing in (4, 8):
                    for side in (-1, 1):
                        t = fly_trade(book, name, d, "15:25", d_out, t_out, wing, cost=cost, side=side)
                        if t:
                            rows.append({**t, **ctx, **extra, "horizon": horizon, "wing_k": wing,
                                         "side": "short" if side < 0 else "long"})
    df = pd.DataFrame(rows)
    df["period"] = np.where(pd.to_datetime(df.day) < pd.Timestamp("2024-07-01"), "train", "test")
    return df


def main() -> int:
    pd.set_option("display.width", 230)
    pd.set_option("display.max_rows", 300)
    book = Book(load())
    df = trades(book)
    df.to_csv(OUT / "positional_trades.csv", index=False)
    print(f"{len(df)} trades; worst net_w {df.net_w.min():.3f} (must be > -1.1)")
    keys = ["horizon", "side", "name", "wing_k", "period"]
    print(pd.DataFrame({k: stats(g.sort_values("day").net_w) for k, g in df.groupby(keys)}).T.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
