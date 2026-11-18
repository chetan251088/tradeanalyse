import calendar
import re

import pandas as pd

MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
WEEKLY_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
SYM = re.compile(r"^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTY|SENSEX|BANKEX)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$")
SYM_M = re.compile(r"^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTY|SENSEX|BANKEX)(\d{2})([A-Z]{3})(\d+)(CE|PE)$")


def monthly_weekday(und, year, month):
    # Approximate exchange monthly-expiry weekday by era (holiday shifts ignored).
    d = pd.Timestamp(year=year, month=month, day=1)
    if und == "SENSEX":
        return 4 if d < pd.Timestamp("2025-01-01") else (1 if d < pd.Timestamp("2025-09-01") else 3)
    if und == "BANKNIFTY" and pd.Timestamp("2024-03-01") <= d < pd.Timestamp("2024-12-01"):
        return 2
    return 3 if d < pd.Timestamp("2025-09-01") else 1


def zerodha_expiry(sym):
    m = SYM.match(sym)
    if m:
        return pd.Timestamp(year=2000 + int(m.group(2)), month=WEEKLY_MONTH[m.group(3)], day=int(m.group(4)))
    m = SYM_M.match(sym)
    if m:
        und, y, mon = m.group(1), 2000 + int(m.group(2)), MONTHS[m.group(3)]
        wd = monthly_weekday(und, y, mon)
        last = pd.Timestamp(year=y, month=mon, day=calendar.monthrange(y, mon)[1])
        return last - pd.Timedelta(days=(last.weekday() - wd) % 7)
    return pd.NaT


def bucket_px(px):
    return pd.cut(px, [0, 10, 30, 100, 250, 1e9], labels=["<10", "10-30", "30-100", "100-250", ">250"])


def summarize(df, by):
    return df.groupby(by, observed=True).agg(
        trades=("net", "size"), win_pct=("net", lambda s: round((s > 0).mean() * 100)),
        avg_win=("net", lambda s: s[s > 0].mean()), avg_loss=("net", lambda s: s[s <= 0].mean()),
        net=("net", "sum"), worst=("net", "min")).round(0)


def main():
    pd.set_option("display.width", 220)
    z = pd.read_csv("out/zerodha_episodes.csv", parse_dates=["entry_time", "exit_time", "date"])
    z["expiry"] = z["symbol"].map(zerodha_expiry)
    z["expiry_day"] = z["date"] == z["expiry"]
    t = z["entry_time"].dt.hour * 60 + z["entry_time"].dt.minute
    z["window"] = pd.cut(t, [0, 9 * 60 + 45, 12 * 60 + 30, 14 * 60 + 30, 24 * 60],
                         labels=["09:15-09:45", "09:45-12:30", "12:30-14:30", "14:30-close"], right=False)
    z["entry_px"] = bucket_px(z["avg_buy"])
    z["era"] = pd.cut(z["date"], [pd.Timestamp("2000-01-01"), pd.Timestamp("2024-04-01"), pd.Timestamp("2030-01-01")],
                      labels=["FY22-FY24", "FY25+"])

    print("ZERODHA — expiry-day vs other days")
    print(summarize(z, ["era", "expiry_day"]).to_string())
    ez = z[z["expiry_day"]]
    print("\nZERODHA expiry-day trades by time window")
    print(summarize(ez, ["era", "window"]).to_string())
    print("\nZERODHA expiry-day trades by entry premium")
    print(summarize(ez, ["era", "entry_px"]).to_string())
    print("\nZERODHA expiry day, 14:30-close, by entry premium (gamma-burst zone)")
    print(summarize(ez[ez["window"] == "14:30-close"], ["entry_px"]).to_string())
    print("\nZERODHA expiry day, 09:45-12:30, premium >=100 (fallen ITM bounce zone)")
    sub = ez[(ez["window"].isin(["09:15-09:45", "09:45-12:30"])) & (ez["avg_buy"] >= 100)]
    print(summarize(sub, ["era", "averaged_down"]).to_string())
    print("\nHold-time winners vs losers on expiry day (minutes, median):",
          round(ez.loc[ez.net > 0, "hold_min"].median(), 1), "vs", round(ez.loc[ez.net <= 0, "hold_min"].median(), 1))
    print("Expiry-day % move on premium winners vs losers (median):",
          round(ez.loc[ez.net > 0, "pnl_pct_premium"].median(), 1), "vs", round(ez.loc[ez.net <= 0, "pnl_pct_premium"].median(), 1))
    big = ez[ez["pnl_pct_premium"] >= 100]
    print(f"Expiry-day trades that captured >=100% on premium: {len(big)}  net {big['net'].sum():,.0f}")

    d = pd.read_csv("out/dhan_trades.csv", parse_dates=["entry_time", "date"])
    d["expiry"] = pd.to_datetime(d["symbol"].str.extract(r"(\d{2} [A-Za-z]{3} \d{4})")[0], format="%d %b %Y")
    d["expiry_day"] = d["date"] == d["expiry"]
    d["entry_px"] = bucket_px(d["avg_buy"])
    print("\nDHAN — expiry-day vs other days")
    print(summarize(d, ["fy", "expiry_day"]).to_string())
    print("\nDHAN expiry-day by entry premium")
    print(summarize(d[d["expiry_day"]], ["entry_px"]).to_string())


if __name__ == "__main__":
    main()
