from __future__ import annotations

import logging
import time
from datetime import date, datetime

from scalper.config import Config, UnderlyingCfg

log = logging.getLogger("scalper.universe")

_TRANSIENT = ("timed out", "timeout", "10060", "connection", "HTTP 500", "HTTP 502", "HTTP 503", "429")


def _expiry_code(d: date) -> str:
    return d.strftime("%d%b%y").upper()


def with_retry(call, attempts: int = 3, wait_sec: float = 2.0):
    """Retry a REST call whose error looks like a network blip; other errors return immediately."""
    res = None
    for i in range(attempts):
        res = call()
        if not isinstance(res, dict) or res.get("status") == "success":
            return res
        if not any(t.lower() in str(res.get("message", "")).lower() for t in _TRANSIENT) or i == attempts - 1:
            return res
        log.warning("Transient error, retrying in %.0fs: %s", wait_sec * (i + 1), str(res.get("message"))[:120])
        time.sleep(wait_sec * (i + 1))
    return res


class Rest:
    """Thin wrapper over the openalgo SDK so the rest of the code never touches response shapes."""

    def __init__(self, client, api_key: str):
        self.c = client
        self.api_key = api_key

    def ping(self) -> tuple[bool, str]:
        res = self.c._post("ping", {"apikey": self.api_key})
        if isinstance(res, dict) and res.get("status") == "success":
            return True, (res.get("data") or {}).get("broker", "")
        return False, (res or {}).get("message", "no response") if isinstance(res, dict) else str(res)

    def analyzer_mode(self) -> str:
        res = self.c.analyzerstatus()
        data = res.get("data", {}) if isinstance(res, dict) else {}
        return str(data.get("mode", "unknown"))

    def expiries(self, u: UnderlyingCfg) -> list[date]:
        res = with_retry(lambda: self.c.expiry(symbol=u.name, exchange=u.fo_exchange, instrumenttype="options"))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"expiry lookup failed for {u.name}: {res}")
        return sorted(datetime.strptime(s, "%d-%b-%y").date() for s in res.get("data", []))

    def holidays(self, year: int) -> list[dict]:
        res = with_retry(lambda: self.c.holidays(year))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"holiday calendar failed for {year}: {res}")
        return res.get("data", [])

    def quote(self, symbol: str, exchange: str) -> dict:
        res = with_retry(lambda: self.c.quotes(symbol=symbol, exchange=exchange))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"quote failed for {exchange}:{symbol}: {res}")
        return res.get("data") or {}

    def multiquotes(self, items: list[dict]) -> dict[str, dict]:
        """{symbol: quote} for [{'symbol', 'exchange'}, ...]; symbols that failed are missing."""
        res = with_retry(lambda: self.c.multiquotes(symbols=items))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"multiquotes failed: {res}")
        return {r["symbol"]: r["data"] for r in res.get("results", []) if r.get("data")}

    def option_chain(self, u: UnderlyingCfg, expiry: date, strike_count: int) -> dict:
        res = with_retry(lambda: self.c.optionchain(
            underlying=u.name, exchange=u.index_exchange, expiry_date=_expiry_code(expiry), strike_count=strike_count
        ))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"option chain failed for {u.name} {expiry}: {res}")
        return res

    def future(self, u: UnderlyingCfg, today: date) -> dict | None:
        """The nearest index future that has not expired: symbol, lot, tick, expiry."""
        res = with_retry(lambda: self.c.search(query=f"{u.name} FUT", exchange=u.fo_exchange))
        if not isinstance(res, dict) or res.get("status") != "success":
            raise RuntimeError(f"futures lookup failed for {u.name}: {res}")
        rows = []
        for x in res.get("data", []):
            if x.get("name") != u.name or not str(x.get("instrumenttype", "")).startswith("FUT"):
                continue
            try:
                exp = datetime.strptime(str(x.get("expiry")), "%d-%b-%y").date()
            except ValueError:
                continue
            if exp >= today:
                rows.append((exp, x))
        if not rows:
            return None
        exp, x = min(rows, key=lambda r: r[0])
        return {"symbol": x["symbol"], "lot": int(x.get("lotsize") or 1), "tick": float(x.get("tick_size") or 0.05),
                "expiry": exp}


def chain_instruments(u: UnderlyingCfg, chain: dict) -> list[dict]:
    out = []
    for row in chain.get("chain", []):
        strike = float(row.get("strike", 0))
        for opt in ("CE", "PE"):
            q = row.get(opt.lower())
            if not q or not q.get("symbol"):
                continue
            out.append(
                {
                    "symbol": q["symbol"],
                    "exchange": u.fo_exchange,
                    "kind": "option",
                    "underlying": u.name,
                    "strike": strike,
                    "opt": opt,
                    "lotsize": int(q.get("lotsize") or 1),
                    "tick_size": float(q.get("tick_size") or 0.05),
                    "seed": {k: q.get(k) for k in ("ltp", "bid", "ask", "open", "high", "low", "oi")},
                }
            )
    return out


def build_meta(rest: Rest, cfg: Config, today: date) -> dict:
    """Pick the nearest expiry per underlying, flag whether today is expiry day, and list the strikes to watch."""
    underlyings = []
    for u in cfg.underlyings:
        if not u.enabled:
            continue
        upcoming = [d for d in rest.expiries(u) if d >= today]
        if not upcoming:
            log.warning("No upcoming expiry for %s", u.name)
            continue
        expiry = upcoming[0]
        chain = rest.option_chain(u, expiry, cfg.universe.strike_count)
        options = chain_instruments(u, chain)
        underlyings.append(
            {
                "name": u.name,
                "index_exchange": u.index_exchange,
                "fo_exchange": u.fo_exchange,
                "expiry": expiry.isoformat(),
                "is_expiry_day": expiry == today,
                "index": {"symbol": u.name, "exchange": u.index_exchange, "kind": "index", "underlying": u.name},
                "options": options,
            }
        )
        log.info(
            "%s: expiry %s (%s), %d option contracts, spot %s",
            u.name, expiry, "EXPIRY DAY" if expiry == today else "not expiry", len(options), chain.get("underlying_ltp"),
        )
    vix = {"symbol": cfg.universe.vix_symbol, "exchange": cfg.universe.vix_exchange, "kind": "vix"}
    return {"day": today.isoformat(), "underlyings": underlyings, "vix": vix}
