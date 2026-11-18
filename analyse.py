import glob
import re
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("out")
OUT.mkdir(exist_ok=True)
UNDERLYINGS = r"(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTY|SENSEX|BANKEX)"
ZERODHA_CHARGE_COLS = ["Brokerage", "Exchange Transaction Charges", "IPFT", "SEBI Charges",
                       "CGST", "SGST", "IGST", "Stamp Duty", "STT"]


def num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.replace(",", "").str.strip(), errors="coerce")


def parse_symbol(sym):
    s = re.sub(r"\s+", " ", str(sym)).strip().upper()
    m = re.match(UNDERLYINGS, s.replace("OPT ", ""))
    und = m.group(1) if m else "OTHER"
    opt = "CE" if s.endswith("CE") or " CE " in f" {s} " else ("PE" if s.endswith("PE") or " PE " in f" {s} " else "FUT")
    return und, opt


# ---------------- Zerodha (FLA936) — fill-level with timestamps ----------------
def load_zerodha():
    rows = []
    for f in sorted(glob.glob("taxpnl-FLA936-*.xlsx")):
        xl = pd.ExcelFile(f)
        sh = [s for s in xl.sheet_names if s.startswith("Tradewise")][0]
        raw = pd.read_excel(f, sheet_name=sh, header=None, dtype=str)
        start = raw.index[raw[1] == "F&O"][0]
        end = raw.index[(raw[1] == "Currency") & (raw.index > start)][0]
        hdr = start + 2
        df = raw.iloc[hdr + 1:end].copy()
        df.columns = raw.iloc[hdr].tolist()
        df = df[df["Symbol"].notna()].dropna(axis=1, how="all")
        for c in ["Quantity", "Buy Value", "Sell Value", "Profit"] + ZERODHA_CHARGE_COLS:
            df[c] = num(df[c]).fillna(0).values
        df["charges"] = df[ZERODHA_CHARGE_COLS].sum(axis=1)
        df["entry_time"] = pd.to_datetime(df["Entry Date"])
        df["exit_time"] = pd.to_datetime(df["Exit Date"])
        df["src"] = f
        rows.append(df)
    z = pd.concat(rows, ignore_index=True)
    z = z.rename(columns={"Symbol": "symbol", "Quantity": "qty", "Buy Value": "buy_value",
                          "Sell Value": "sell_value", "Profit": "gross"})
    z["net"] = z["gross"] - z["charges"]
    return z[["symbol", "entry_time", "exit_time", "qty", "buy_value", "sell_value", "gross", "charges", "net", "src"]]


def zerodha_episodes(z):
    """Merge overlapping fill pairs of the same contract into one position episode (flat -> flat)."""
    z = z.sort_values(["symbol", "entry_time", "exit_time"]).copy()
    ep_id, last_sym, cur_end, ids = -1, None, None, []
    for sym, et, xt in zip(z["symbol"], z["entry_time"], z["exit_time"]):
        if sym != last_sym or et > cur_end:
            ep_id += 1
            cur_end = xt
        else:
            cur_end = max(cur_end, xt)
        last_sym = sym
        ids.append(ep_id)
    z["episode"] = ids
    g = z.groupby("episode")
    ep = g.agg(symbol=("symbol", "first"), entry_time=("entry_time", "min"), last_entry=("entry_time", "max"),
               first_exit=("exit_time", "min"), exit_time=("exit_time", "max"), qty=("qty", "sum"),
               buy_value=("buy_value", "sum"), sell_value=("sell_value", "sum"), gross=("gross", "sum"),
               charges=("charges", "sum"), net=("net", "sum"), fills=("qty", "size"),
               n_entries=("entry_time", "nunique"), n_exits=("exit_time", "nunique"))
    ep["avg_buy"] = ep["buy_value"] / ep["qty"]
    ep["avg_sell"] = ep["sell_value"] / ep["qty"]
    ep["hold_min"] = (ep["exit_time"] - ep["entry_time"]).dt.total_seconds() / 60
    ep["pnl_pct_premium"] = ep["gross"] / ep["buy_value"] * 100
    # Price-level averaging: multiple entry timestamps where later entries were cheaper than the first (adding to a loser)
    first_px = z.groupby("episode").apply(lambda d: d.loc[d["entry_time"] == d["entry_time"].min(), "buy_value"].sum()
                                          / d.loc[d["entry_time"] == d["entry_time"].min(), "qty"].sum(),
                                          include_groups=False)
    ep["first_entry_px"] = first_px
    ep["averaged_down"] = (ep["n_entries"] > 1) & (ep["avg_buy"] < ep["first_entry_px"] * 0.995)
    ep["broker"] = "Zerodha"
    ep["date"] = ep["entry_time"].dt.normalize()
    return ep.reset_index()


# ---------------- Dhan — day-level per contract ----------------
def load_dhan():
    raw = pd.read_excel("dhan_tax_pnl.xlsx", header=None, dtype=str)
    hdr = raw.index[raw[0] == "Sr."][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c) if pd.notna(c) else f"c{i}" for i, c in enumerate(raw.iloc[hdr])]
    df = df[num(df["Sr."]).notna().values]
    out = pd.DataFrame({
        "symbol": df["Security Name"].values,
        "entry_time": pd.to_datetime(df["Buy Date"], format="%d-%m-%Y").values,
        "exit_time": pd.to_datetime(df["Sell Date"], format="%d-%m-%Y").values,
        "qty": num(df["Buy Qty."]).values,
        "avg_buy": num(df["Avg. Buy Price"]).values,
        "avg_sell": num(df["Avg. Sell Price"]).values,
        "buy_value": num(df["Buy Value"]).values,
        "sell_value": num(df["Sell Value"]).values,
        "hold_days": num(df["Holding Period"]).values,
        "gross": num(df["Gross P&L"]).values,
        "charges": num(df["Total Charges"]).values,
        "net": num(df["Net P&L"]).values,
    })
    out["broker"] = "Dhan"
    out["date"] = out["entry_time"]
    out["pnl_pct_premium"] = out["gross"] / out["buy_value"] * 100
    return out


# ---------------- Kotak (YQRCT) — contract-level aggregate per FY ----------------
def load_kotak():
    rows = []
    for f in sorted(glob.glob("Gain_Loss_YQRCT_*.xlsx")):
        raw = pd.read_excel(f, sheet_name="P&L Equity-Tradewise", header=None, dtype=str)
        hdr = raw.index[raw[0] == "Script Name"][0]
        sub = raw.iloc[hdr + 1]
        df = raw.iloc[hdr + 3:].copy()
        df = df[df[0].notna() & df[13].notna()]
        ncol = raw.shape[1]
        net_col, gross_col = ncol - 2, ncol - 1
        tot_col = [i for i, v in sub.items() if isinstance(v, str) and v.startswith("Total") and "T = B - A" not in v][-1]
        rows.append(pd.DataFrame({
            "symbol": df[0].str.replace(r"\s+", " ", regex=True).str.strip().values,
            "qty": num(df[13]).values,
            "avg_buy": num(df[6]).values,
            "avg_sell": num(df[9]).values,
            "buy_value": num(df[7]).values,
            "sell_value": num(df[10]).values,
            "gross": num(df[gross_col]).values,
            "charges": num(df[tot_col]).values,
            "net": num(df[net_col]).values,
            "fy": f[16:24] + "-" + f[25:33],
        }))
    k = pd.concat(rows, ignore_index=True)
    exp = k["symbol"].str.extract(r"\s(\d{2}[A-Za-z]{3}\d{2,4})\s")[0]
    k["expiry"] = pd.to_datetime(exp, format="mixed", dayfirst=True, errors="coerce")
    k["broker"] = "Kotak"
    k["date"] = k["expiry"]
    k["pnl_pct_premium"] = k["gross"] / k["buy_value"] * 100
    return k


def stats(df, label):
    w, l = df[df["net"] > 0], df[df["net"] <= 0]
    avg_w, avg_l = w["net"].mean(), l["net"].mean()
    return {
        "segment": label, "trades": len(df), "win_rate_%": round(len(w) / max(len(df), 1) * 100, 1),
        "avg_win": round(avg_w, 0), "avg_loss": round(avg_l, 0),
        "payoff(avgW/|avgL|)": round(avg_w / abs(avg_l), 2) if avg_l else np.nan,
        "expectancy/trade": round(df["net"].mean(), 0), "max_win": round(df["net"].max(), 0),
        "max_loss": round(df["net"].min(), 0), "gross": round(df["gross"].sum(), 0),
        "charges": round(df["charges"].sum(), 0), "net": round(df["net"].sum(), 0),
        "breakeven_win_rate_%": round(abs(avg_l) / (avg_w + abs(avg_l)) * 100, 1) if avg_l else np.nan,
    }


def tail_share(df):
    losses = df.loc[df["net"] < 0, "net"].sort_values()
    tot = losses.sum()
    r = {}
    for p in (0.05, 0.10, 0.20):
        n = max(int(len(losses) * p), 1)
        r[f"worst {int(p*100)}% losers share of all losses"] = f"{losses.iloc[:n].sum() / tot * 100:.0f}% ({n} trades, {losses.iloc[:n].sum():,.0f})"
    r["sum of all winners"] = f"{df.loc[df['net'] > 0, 'net'].sum():,.0f}"
    r["sum of all losers"] = f"{tot:,.0f}"
    return r


def fy_of(d):
    d = pd.Timestamp(d)
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y % 100:02d}-{(y + 1) % 100:02d}"


def main():
    lines = []
    p = lines.append

    z = load_zerodha()
    ze = zerodha_episodes(z)
    d = load_dhan()
    k = load_kotak()
    for df in (ze, d):
        df["underlying"], df["opt"] = zip(*df["symbol"].map(parse_symbol))
        df["fy"] = df["date"].map(fy_of)
    k["underlying"], k["opt"] = zip(*k["symbol"].map(parse_symbol))

    ze.to_csv(OUT / "zerodha_episodes.csv", index=False)
    z.to_csv(OUT / "zerodha_fills.csv", index=False)
    d.to_csv(OUT / "dhan_trades.csv", index=False)
    k.to_csv(OUT / "kotak_contracts.csv", index=False)

    p("=" * 100)
    p("RECONCILIATION (computed totals)")
    p(f"Zerodha fills: {len(z)}, gross {z['gross'].sum():,.0f}, charges {z['charges'].sum():,.0f}, net {z['net'].sum():,.0f}")
    p(f"Dhan rows: {len(d)}, gross {d['gross'].sum():,.0f}, charges {d['charges'].sum():,.0f}, net {d['net'].sum():,.0f}")
    p(f"Kotak contracts: {len(k)}, gross {k['gross'].sum():,.0f}, charges {k['charges'].sum():,.0f}, net {k['net'].sum():,.0f}")
    tot_net = z["net"].sum() + d["net"].sum() + k["net"].sum()
    tot_ch = z["charges"].sum() + d["charges"].sum() + k["charges"].sum()
    tot_gross = z["gross"].sum() + d["gross"].sum() + k["gross"].sum()
    p(f"ALL BROKERS: gross {tot_gross:,.0f}  charges {tot_ch:,.0f}  NET {tot_net:,.0f}")

    p("\n" + "=" * 100)
    p("HEADLINE STATS PER BROKER / FY")
    st = [stats(ze, "Zerodha ALL (episodes)")] + [stats(g, f"Zerodha {fy}") for fy, g in ze.groupby("fy")]
    st += [stats(d, "Dhan ALL (day/contract)")] + [stats(g, f"Dhan {fy}") for fy, g in d.groupby("fy")]
    st += [stats(k, "Kotak ALL (contract agg)")] + [stats(g, f"Kotak {fy}") for fy, g in k.groupby("fy")]
    p(pd.DataFrame(st).to_string(index=False))

    p("\n" + "=" * 100)
    p("TAIL RISK: how much of total loss comes from the few biggest losers")
    for name, df in (("Zerodha", ze), ("Dhan", d), ("Kotak", k)):
        p(f"-- {name}")
        for kk, vv in tail_share(df).items():
            p(f"   {kk}: {vv}")

    p("\n" + "=" * 100)
    p("ZERODHA DEEP DIVE (timestamped)")
    w, l = ze[ze["net"] > 0], ze[ze["net"] <= 0]
    p(f"Median hold  winners: {w['hold_min'].median():.1f} min   losers: {l['hold_min'].median():.1f} min")
    p(f"Mean hold    winners: {w['hold_min'].mean():.1f} min   losers: {l['hold_min'].mean():.1f} min")
    p(f"Median % move on premium  winners: {w['pnl_pct_premium'].median():.1f}%   losers: {l['pnl_pct_premium'].median():.1f}%")
    p(f"Mean % move on premium    winners: {w['pnl_pct_premium'].mean():.1f}%   losers: {l['pnl_pct_premium'].mean():.1f}%")
    bins = [0, 1, 3, 5, 10, 20, 60, 180, 1e9]
    lbl = ["<1m", "1-3m", "3-5m", "5-10m", "10-20m", "20-60m", "1-3h", ">3h"]
    ze["hold_bucket"] = pd.cut(ze["hold_min"], bins=bins, labels=lbl, include_lowest=True)
    hb = ze.groupby("hold_bucket", observed=True).agg(trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
                                                     avg_net=("net", "mean"), total_net=("net", "sum"))
    p("\nBy holding time:\n" + hb.round(1).to_string())

    ze["hour"] = ze["entry_time"].dt.hour + (ze["entry_time"].dt.minute >= 30) * 0.5
    hh = ze.groupby("hour").agg(trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
                                avg_net=("net", "mean"), total_net=("net", "sum"))
    p("\nBy entry half-hour:\n" + hh.round(1).to_string())

    av = ze.groupby("averaged_down").agg(trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
                                         avg_net=("net", "mean"), total_net=("net", "sum"), worst=("net", "min"))
    p("\nAveraging down (added at lower price after first entry):\n" + av.round(1).to_string())

    p("\nBy underlying / option type:\n" + ze.groupby(["underlying", "opt"]).agg(
        trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
        avg_net=("net", "mean"), total_net=("net", "sum")).round(1).to_string())

    # Position size vs outcome
    ze["size_q"] = pd.qcut(ze["buy_value"], 5, duplicates="drop")
    p("\nBy capital deployed (buy value quintile):\n" + ze.groupby("size_q", observed=True).agg(
        trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
        avg_net=("net", "mean"), total_net=("net", "sum")).round(1).to_string())

    # Daily behaviour
    day = ze.sort_values("entry_time").groupby("date")
    dd = day.agg(trades=("net", "size"), net=("net", "sum"), gross=("gross", "sum"), charges=("charges", "sum"),
                 worst_trade=("net", "min"), best_trade=("net", "max"))
    p(f"\nTrading days: {len(dd)}  green days: {(dd['net'] > 0).mean() * 100:.1f}%  "
      f"avg green day {dd.loc[dd['net'] > 0, 'net'].mean():,.0f}  avg red day {dd.loc[dd['net'] <= 0, 'net'].mean():,.0f}")
    p("Worst 15 days:\n" + dd.sort_values("net").head(15).round(0).to_string())
    p("Best 10 days:\n" + dd.sort_values("net", ascending=False).head(10).round(0).to_string())
    worst_days_share = dd.sort_values("net").head(10)["net"].sum()
    p(f"Worst 10 days total: {worst_days_share:,.0f} vs all-days net {dd['net'].sum():,.0f}")

    # Trades-per-day vs result (overtrading)
    dd["tpd"] = pd.cut(dd["trades"], [0, 2, 5, 10, 20, 1e9], labels=["1-2", "3-5", "6-10", "11-20", ">20"])
    p("\nDay result by number of trades that day:\n" + dd.groupby("tpd", observed=True).agg(
        days=("net", "size"), green_pct=("net", lambda s: (s > 0).mean() * 100),
        avg_day=("net", "mean"), total=("net", "sum")).round(0).to_string())

    # After a loss: what happens to the next trade the same day (revenge / tilt)
    seq = ze.sort_values("entry_time").copy()
    seq["prev_net"] = seq.groupby("date")["net"].shift()
    seq["prev_size"] = seq.groupby("date")["buy_value"].shift()
    seq["gap_min"] = (seq["entry_time"] - seq.groupby("date")["exit_time"].shift()).dt.total_seconds() / 60
    aft = seq.dropna(subset=["prev_net"]).copy()
    aft["after"] = np.where(aft["prev_net"] > 0, "after WIN", "after LOSS")
    aft["size_up"] = aft["buy_value"] > aft["prev_size"] * 1.2
    p("\nNext trade same day after a win vs a loss:\n" + aft.groupby("after").agg(
        trades=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100), avg_net=("net", "mean"),
        total=("net", "sum"), sized_up_pct=("size_up", lambda s: s.mean() * 100),
        median_gap_min=("gap_min", "median")).round(1).to_string())

    # Running daily P&L: after day P&L drops below -X, what did the rest of day do?
    seq["cum_day"] = seq.groupby("date")["net"].cumsum()
    seq["cum_before"] = seq["cum_day"] - seq["net"]
    for thr in (-2000, -5000, -10000):
        sub = seq[seq["cum_before"] <= thr]
        p(f"Trades taken when already down <= {thr:,} on the day: {len(sub)} trades, net {sub['net'].sum():,.0f}, "
          f"win rate {(sub['net'] > 0).mean() * 100 if len(sub) else 0:.0f}%")
    for thr in (2000, 5000):
        sub = seq[seq["cum_before"] >= thr]
        p(f"Trades taken when already up >= {thr:,} on the day: {len(sub)} trades, net {sub['net'].sum():,.0f}, "
          f"win rate {(sub['net'] > 0).mean() * 100 if len(sub) else 0:.0f}%")

    # Counterfactuals (approximate: assumes stop fills at the level; no slippage modelling)
    p("\nCOUNTERFACTUALS on Zerodha episodes (approximate, ignores slippage / missed upside):")
    for pct in (15, 20, 25, 30):
        cap = -(ze["buy_value"] * pct / 100) - ze["charges"]
        adj = np.where(ze["net"] < cap, cap, ze["net"])
        p(f"  Hard stop at -{pct}% of premium: net {adj.sum():,.0f} (actual {ze['net'].sum():,.0f})")
    for cap_abs in (1000, 2000, 3000, 5000):
        adj = np.maximum(ze["net"], -cap_abs)
        p(f"  Max loss per trade capped at {cap_abs:,}: net {adj.sum():,.0f}")
    for dl in (3000, 5000, 10000):
        kept = seq[seq["cum_before"] > -dl]
        p(f"  Daily stop {dl:,} (no new trades once down that much): net {kept['net'].sum():,.0f}, trades {len(kept)}")

    p("\nTop 25 worst Zerodha episodes:")
    cols = ["symbol", "entry_time", "exit_time", "hold_min", "qty", "first_entry_px", "avg_buy", "avg_sell",
            "n_entries", "n_exits", "pnl_pct_premium", "gross", "charges", "net"]
    p(ze.sort_values("net")[cols].head(25).round(2).to_string(index=False))
    p("\nTop 15 best Zerodha episodes:")
    p(ze.sort_values("net", ascending=False)[cols].head(15).round(2).to_string(index=False))

    p("\n" + "=" * 100)
    p("DHAN DEEP DIVE (date-level)")
    p(f"Overnight/multi-day rows: {(d['hold_days'] > 0).sum()}  net from them {d.loc[d['hold_days'] > 0, 'net'].sum():,.0f}")
    p(f"Median % move on premium winners: {d.loc[d['net'] > 0, 'pnl_pct_premium'].median():.1f}%  "
      f"losers: {d.loc[d['net'] <= 0, 'pnl_pct_premium'].median():.1f}%")
    p("By underlying:\n" + d.groupby(["underlying", "opt"]).agg(rows=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
                                                               net=("net", "sum")).round(1).to_string())
    dday = d.groupby("date")["net"].sum()
    p(f"Dhan trading days {len(dday)} green {(dday > 0).mean() * 100:.0f}%  worst days:\n{dday.sort_values().head(10).round(0).to_string()}")
    p("Top 15 worst Dhan rows:\n" + d.sort_values("net")[["symbol", "entry_time", "exit_time", "hold_days", "qty", "avg_buy",
                                                          "avg_sell", "pnl_pct_premium", "gross", "charges", "net"]].head(15).round(2).to_string(index=False))

    p("\n" + "=" * 100)
    p("KOTAK (contract-level aggregate only — no dates/times in report)")
    p("By underlying:\n" + k.groupby(["underlying", "opt"]).agg(contracts=("net", "size"), win_rate=("net", lambda s: (s > 0).mean() * 100),
                                                               net=("net", "sum"), charges=("charges", "sum")).round(0).to_string())
    p("Top 15 worst Kotak contracts:\n" + k.sort_values("net")[["symbol", "fy", "qty", "avg_buy", "avg_sell", "pnl_pct_premium",
                                                               "gross", "charges", "net"]].head(15).round(2).to_string(index=False))

    (OUT / "report.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
