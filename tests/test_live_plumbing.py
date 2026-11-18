import json
from datetime import date

from scalper.config import Config
from scalper.feed import MODE_DEPTH, MODE_QUOTE, LiveFeed
from scalper.universe import Rest, build_meta


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))


def test_feed_passes_full_payload_and_resubscribes_after_auth():
    got = []
    feed = LiveFeed("ws://x", "k", got.append)
    ws = FakeWS()
    feed._ws = ws
    feed.subscribe([("NIFTY", "NSE_INDEX")], MODE_QUOTE)
    feed.subscribe([("NIFTY03NOV2624000PE", "NFO")], MODE_DEPTH)
    assert ws.sent == []  # nothing goes out before authentication

    feed._on_open(ws)
    assert ws.sent[0] == {"action": "authenticate", "api_key": "k"}
    feed._on_message(ws, json.dumps({"type": "auth", "status": "success", "broker": "dhan"}))
    subs = {m["mode"]: m["symbols"] for m in ws.sent[1:]}
    assert subs == {
        MODE_QUOTE: [{"symbol": "NIFTY", "exchange": "NSE_INDEX"}],
        MODE_DEPTH: [{"symbol": "NIFTY03NOV2624000PE", "exchange": "NFO"}],
    }

    data = {"ltp": 101.5, "oi": 123456, "volume": 999, "timestamp": 1_700_000_000_000,
            "depth": {"buy": [{"price": 101.4}], "sell": [{"price": 101.6}]}}
    feed._on_message(ws, json.dumps({"type": "market_data", "symbol": "NIFTY03NOV2624000PE",
                                     "exchange": "NFO", "mode": 3, "data": data}))
    assert got == [("tick", 1_700_000_000_000, "NIFTY03NOV2624000PE", "NFO", data)]


class FakeClient:
    def _post(self, endpoint, payload):
        return {"status": "success", "data": {"message": "pong", "broker": "dhan"}}

    def analyzerstatus(self):
        return {"status": "success", "data": {"mode": "analyze", "analyze_mode": True}}

    def expiry(self, symbol, exchange, instrumenttype):
        days = {"NIFTY": ["03-NOV-26", "10-NOV-26"], "SENSEX": ["29-OCT-26", "05-NOV-26"]}
        return {"status": "success", "data": days[symbol]}

    def optionchain(self, underlying, exchange, expiry_date, strike_count):
        def leg(sym, label):
            return {"symbol": sym, "label": label, "ltp": 100, "bid": 99.9, "ask": 100.1, "open": 120,
                    "high": 130, "low": 90, "oi": 5000, "lotsize": 65 if underlying == "NIFTY" else 20,
                    "tick_size": 0.05}
        return {"status": "success", "underlying_ltp": 24010, "atm_strike": 24000.0, "chain": [
            {"strike": 24000.0, "ce": leg(f"{underlying}{expiry_date}24000CE", "ATM"),
             "pe": leg(f"{underlying}{expiry_date}24000PE", "ATM")},
        ]}


def test_build_meta_flags_expiry_day_and_uses_nearest_expiry():
    rest = Rest(FakeClient(), "k")
    assert rest.ping() == (True, "dhan")
    assert rest.analyzer_mode() == "analyze"
    meta = build_meta(rest, Config(), date(2026, 11, 3))
    nifty, sensex = meta["underlyings"]
    assert nifty["is_expiry_day"] is True and nifty["expiry"] == "2026-11-03"
    assert sensex["is_expiry_day"] is False and sensex["expiry"] == "2026-11-05"
    assert nifty["options"][0]["symbol"] == "NIFTY03NOV2624000CE"
    assert nifty["options"][0]["exchange"] == "NFO" and sensex["options"][0]["exchange"] == "BFO"
    assert nifty["options"][0]["lotsize"] == 65 and nifty["options"][0]["seed"]["open"] == 120
