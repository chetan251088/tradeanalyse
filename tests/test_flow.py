import math
import queue
from datetime import date, datetime, timedelta

import numpy as np

from scalper.clock import IST
from scalper.config import Config
from scalper.flow import model as M
from scalper.flow.engine import FlowEngine
from scalper.flow.server import FlowServer
from scalper.flow.sources import LiveSource

EXP = date(2026, 11, 3)


def ms(h, m, s=0):
    return int(datetime(2026, 11, 2, h, m, s, tzinfo=IST).timestamp() * 1000)


def test_b76_iv_roundtrip_and_greeks():
    f, k, t = 24000.0, np.array([23800.0, 24000.0, 24200.0]), 2 / 365
    sig = np.array([0.14, 0.12, 0.13])
    for is_call in (True, False):
        px = M.b76_price(f, k, t, sig, is_call)
        assert np.allclose(M.b76_iv(px, f, k, t, is_call), sig, atol=1e-4)
    gc, gp = M.b76_greeks(f, k, t, sig, True), M.b76_greeks(f, k, t, sig, False)
    assert np.allclose(gp["delta"], gc["delta"] - 1)
    assert (gc["gamma"] > 0).all() and (gc["theta"] < 0).all() and (gc["vega"] > 0).all()
    assert np.isnan(M.b76_iv(np.array([150.0]), f, np.array([23800.0]), t, True))[0]  # below intrinsic


def test_forward_maxpain_flip_buildup_side():
    assert M.synthetic_forward([23900, 24000, 24100], [160, 100, 55], [60, 95, 150], 24000) == 24005
    assert M.max_pain([100, 110, 120], [0, 500, 0], [0, 500, 0]) == 110
    assert M.flip_level([1, 2, 3, 4], [-2, -1, 1, 2], 2.4) == 2.5
    assert M.flip_level([1, 2], [1, 2], 1.5) is None
    assert [M.buildup(1, 1), M.buildup(-1, 1), M.buildup(1, -1), M.buildup(-1, -1)] == [M.LONG, M.WRITE, M.COVER, M.UNWIND]
    assert M.trade_side(10.5, 10.0, 10.5, 10.2, 0) == 1
    assert M.trade_side(10.0, 10.0, 10.5, 10.2, 0) == -1
    assert M.trade_side(10.3, 0, 0, 10.2, 0) == 1  # no quote -> tick rule


def test_decay_split_adds_up():
    d = M.decay_split(24000, 0.13, 3 / 365, 24100, 0.12, 2 / 365, 24000)
    assert math.isclose(d["theta"] + d["move"] + d["vol"], d["model"])
    assert d["theta"] < 0 and d["move"] > 0 and d["vol"] < 0


def _engine():
    cfg = Config()
    eng = FlowEngine(cfg.flow, "live")
    eng.add_book("NIFTY", "NIFTY", "NSE_INDEX", 50)
    eng.set_vix("INDIAVIX", "NSE_INDEX")
    rows = []
    for k in range(23800, 24250, 50):
        for cp in ("CE", "PE"):
            rows.append({"symbol": f"N{k}{cp}", "exchange": "NFO", "strike": k, "is_call": cp == "CE", "lot": 65,
                         "oi": 100_000, "volume": 1_000})
    eng.add_chain("NIFTY", EXP, rows)
    return eng


def _price(k, is_call, f=24000.0):
    return float(M.b76_price(f, k, 1.2 / 365, 0.13, is_call))


def _feed(eng, ts, f=24000.0, vol=1_000, oi=100_000):
    eng.on_tick(ts, "NIFTY", "NSE_INDEX", {"ltp": f, "close": 23950})
    eng.on_tick(ts, "INDIAVIX", "NSE_INDEX", {"ltp": 14.2, "close": 14.0})
    for k in range(23800, 24250, 50):
        for cp in ("CE", "PE"):
            px = _price(k, cp == "CE", f)
            eng.on_tick(ts, f"N{k}{cp}", "NFO", {
                "ltp": px + 0.05, "volume": vol, "oi": oi,
                "depth": {"buy": [{"price": px - 0.05, "quantity": 650}], "sell": [{"price": px + 0.05, "quantity": 325}]}})


def test_engine_analytics_frame_and_flows():
    eng = _engine()
    _feed(eng, ms(10, 0))
    _feed(eng, ms(10, 0, 30), vol=2_000)  # 1,000 more traded at the offer -> buyers
    eng.analyze()
    f = eng.frame("NIFTY", 0, 24000)
    h = f["hud"]
    assert len(f["rows"]) == 9 and h["spot"] == 24000 and abs(h["fwd"] - 24000) < 1
    assert 12.0 < h["atm_iv"] < 14.5 and h["max_pain"] is not None and h["lot"] == 65
    ix = {name: i for i, name in enumerate(eng.history("NIFTY")["fields"])}
    atm = next(r for r in f["rows"] if r[0] == 24000)
    assert atm[ix["ce_fbuy"]] == 1000 and atm[ix["ce_fsell"]] == 0 and atm[ix["ce_imb"]] > 0
    assert f["depth"]["ce"]["levels"] == 5 and f["depth"]["ce"]["bids"][0][1] == 650
    eng.reset_flows()
    assert eng.frame("NIFTY", 0)["rows"][4][ix["ce_fbuy"]] == 0


def test_depth20_packet_does_not_overwrite_prices():
    eng = _engine()
    _feed(eng, ms(10, 0))
    ltp = eng.route[("N24000CE", "NFO")][3].ltp
    levels = [{"price": 100 + i, "quantity": 65 if i else 65 * 200, "orders": 1} for i in range(20)]
    eng.on_tick(ms(10, 0, 5), "N24000CE", "NFO", {"depth_level": 20, "depth": {"buy": levels, "sell": levels}})
    o = eng.route[("N24000CE", "NFO")][3]
    assert o.ltp == ltp and o.depth20 is not None and o.wall is not None
    assert any("resting" in e["text"] for e in eng.books["NIFTY"].events)
    eng.analyze()
    assert eng.frame("NIFTY", 0, 24000)["depth"]["ce"]["levels"] == 20


def test_minute_roll_tags_heat_and_writing_event():
    eng = _engine()
    for i in range(7):
        oi = 100_000 if i < 2 else 100_000 + 60_000 * (i - 1)
        _feed(eng, ms(10, i), f=24000 - 30 * i, vol=1_000 + 500 * i, oi=oi)
    eng.advance(ms(10, 8))
    b = eng.books["NIFTY"]
    assert len(b.heat) >= 6 and b.minute_seq >= 6
    o = eng.route[("N24200CE", "NFO")][3]
    assert o.tag == M.WRITE and o.tag_doi > 0
    hist = eng.history("NIFTY")
    assert hist["candles"] and hist["iv"] and isinstance(hist["events"], list)


def to_hm(ts):
    from scalper.clock import to_dt

    return to_dt(ts).strftime("%H:%M")


def test_backfill_rebuilds_heat_anchor_and_read():
    eng = _engine()
    b = eng.books["NIFTY"]
    for m in range(9 * 60 + 15, 10 * 60):
        b.candles.append([ms(m // 60, m % 60), 24000, 24000, 24000, 24000])
    bars = {}
    for k in range(23800, 24250, 50):
        for cp in ("CE", "PE"):
            rows, cum = [], 0.0
            for i, m in enumerate(range(9 * 60 + 15, 10 * 60)):
                cum += 1000
                px = _price(k, cp == "CE") * (1 - 0.002 * i)
                oi = 100_000 + (2_000 * i if cp == "PE" and k < 24000 else 0)
                rows.append((ms(m // 60, m % 60), px, oi, cum))
            bars[(f"N{k}{cp}", "NFO")] = rows
    eng.now = ms(10, 0)
    eng.backfill(bars)
    eng.rebuild()
    o = eng.route[("N23900PE", "NFO")][3]
    assert o.oi0 == 100_000 and len(o.mins) == 45
    assert len(b.heat) == 45 and any(dp > 0 for _, col in b.heat for _, dc, dp in col)
    assert b.anchor and to_hm(b.anchor["ts"]) == "09:20" and b.decay_series and b.iv_series
    _feed(eng, ms(10, 0, 30), oi=100_000)
    eng.analyze()
    rd = eng.frame("NIFTY")["hud"]["read"]
    assert rd["bias"] and {"SUPPORT", "RESIST", "WRITERS", "DAY OI"} <= {x["l"] for x in rd["lines"]}


class FakeRest:
    def ping(self):
        return True, "dhan"

    def expiries(self, u):
        today = date.today()
        return [today, today + timedelta(days=7)]

    def option_chain(self, u, exp, n):
        step = 50 if u.name == "NIFTY" else 100
        base = 24000 if u.name == "NIFTY" else 80000
        chain = [{"strike": base + i * step, "ce": {"symbol": f"{u.name}{exp:%d%m}{base + i * step}CE", "lotsize": 65},
                  "pe": {"symbol": f"{u.name}{exp:%d%m}{base + i * step}PE", "lotsize": 65}} for i in range(-n, n + 1)]
        return {"underlying_ltp": base, "chain": chain}

    def future(self, u, today):
        return {"symbol": f"{u.name}24NOV26FUT", "lot": 65 if u.name == "NIFTY" else 20, "tick": 0.1, "expiry": today}


class FakeFeed:
    def __init__(self):
        self.subs = []

    def start(self):
        pass

    def wait_ready(self, t):
        return True

    def subscribe(self, instruments, mode, depth=5):
        self.subs.append((mode, depth, list(instruments)))


def test_live_source_subscribes_depth5_everywhere_and_depth20_on_nifty_only(tmp_path):
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path)
    eng = FlowEngine(cfg.flow, "live")
    src = LiveSource.__new__(LiveSource)
    src.cfg, src.engine, src.fcfg = cfg, eng, cfg.flow
    src.rest, src.feed, src.ucfg, src.d20, src.next_recenter = FakeRest(), FakeFeed(), {}, set(), 0
    src.q, src.shift, src.loaded, src.futs, src.rec = queue.SimpleQueue(), 0, 0, [], None
    src._candles = lambda u, day: None
    src._backfill = lambda day: None
    src.start()
    d5 = [i for mode, depth, ins in src.feed.subs if mode == 3 and depth == 5 for i in ins]
    d20 = [i for mode, depth, ins in src.feed.subs if depth == 20 for i in ins]
    assert len(d5) == 2 * 2 * 2 * 25 + 2  # 2 indices x 2 expiries x CE/PE x 25 strikes + 2 futures
    assert ("NIFTY24NOV26FUT", "NFO") in d5 and ("SENSEX24NOV26FUT", "BFO") in d5
    assert len(d20) == 50 and all(x == "NFO" for _, x in d20)  # Dhan's cap: the future + 49 options
    assert ("NIFTY24NOV26FUT", "NFO") in d20
    assert sum(s.startswith(f"NIFTY{date.today():%d%m}") for s, _ in d20) == 49
    assert eng.books["NIFTY"].tape.lot == 65 and eng.books["SENSEX"].tape.lot == 20
    assert src.rec.want[("NIFTY24NOV26FUT", "NFO")] == 0 and src.rec.want[("NIFTY", "NSE_INDEX")] == 1000
    assert sum(1 for k, gap in src.rec.want.items() if gap and "CE" in k[0]) == 2 * 11  # ATM+-5 per index
    src.rec.close()
    assert len(list((tmp_path / "flow" / "depth").glob("*.jsonl.gz"))) == 1


def test_bounce_watch_tickets_grades_and_events(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scalper.clock import MINUTE_MS, at
    from scalper.flow import bounce

    monkeypatch.setattr(bounce, "RESEARCH", tmp_path)
    t0 = at(EXP, "09:15")
    closes = [100.0] * 6 + [100 - i * 40 / 25 for i in range(1, 26)] + [60 - i * 2.0 for i in range(1, 11)] + [40.0] * 10
    rows, prev = [], 100.0
    for i, c in enumerate(closes):
        rows.append((t0 + i * MINUTE_MS, c, 1e5, 0.0, 0.0, 1000.0 * i, prev, max(prev, c), min(prev, c)))
        prev = c
    opts = {(24000.0, False): SimpleNamespace(mins=rows, exchange="NFO", lot=65, ask=0.0, bid=0.0, ltp=40.0)}
    candles = [[t0 + i * MINUTE_MS, 24100.0, 24100.0, 24100.0, 24100.0] for i in range(len(closes))]
    last = rows[-1][0]

    quiet = bounce.BounceWatch(5)
    assert quiet.scan("NIFTY", 50.0, opts, candles, 24100.0, last, live_book=False) == []
    bw = bounce.BounceWatch(5)
    ev = bw.scan("NIFTY", 50.0, opts, candles, 24100.0, last, live_book=True)
    tk = bw.books["NIFTY"].tickets
    assert quiet.books["NIFTY"].tickets[0].m == tk[0].m == t0 + 35 * MINUTE_MS  # first flush at <= half the 09:20 price
    assert "flush" in tk[0].trig and math.isclose(tk[0].entry, 50.1)
    ret, why, _ = tk[0].res[bounce.MAIN]
    assert why == "stop" and ret < -0.10
    assert [e[0].split(":")[0] for e in ev][:2] == ["bounce", "bounce-r"]
    v = bw.view("NIFTY", opts, True)
    assert v["n"] == len(tk) and v["tickets"][-1]["res"][bounce.MAIN][1] == "stop" and v["bt"][0][1] is None


def _pk(bids, asks, ltp=None, vol=None, level=None):
    d = {"depth": {"buy": [{"price": p, "quantity": q} for p, q in bids],
                   "sell": [{"price": p, "quantity": q} for p, q in asks]}}
    if ltp is not None:
        d["ltp"], d["volume"] = ltp, vol
    if level:
        d["depth_level"] = level
    return d


def _ladder(top, step, n, q=650, wall=None):
    return [(round(top + i * step, 2), wall[1] if wall and abs(top + i * step - wall[0]) < 1e-9 else q) for i in range(n)]


def test_tape_aggressors_sweep_ofi_and_absorption():
    from scalper.flow.tape import Tape

    t = Tape("NIFTY", "NIFTYFUT", "NFO", 65, 0.1, Config().flow)
    bids, asks = _ladder(24000.0, -0.5, 5), _ladder(24000.5, 0.5, 5)
    assert t.on_tick(1_000, _pk(bids, asks, 24000.0, 100_000)) == []
    ev = t.on_tick(2_000, _pk(_ladder(24001.5, -0.5, 5), _ladder(24002.5, 0.5, 5), 24002.0, 100_000 + 60 * 65))
    assert t.cvd == 60 * 65 and t.ofi > 0
    assert ev and ev[0][0].startswith("sweep:") and "4 levels lifted" in ev[0][2]
    hits = []
    for i in range(1, 5):  # 50 lots sold into a 10-lot 24001.5 bid every 2 s, the bid keeps refilling
        hits += t.on_tick(2_000 + 2_000 * i, _pk(_ladder(24001.5, -0.5, 5), _ladder(24002.5, 0.5, 5), 24001.5,
                                                  100_000 + 60 * 65 + i * 50 * 65))
    assert len(hits) == 1 and hits[0][0].startswith("absorb:NIFTY:bid") and "passive BUYER" in hits[0][2]
    t.sample(10_000)
    v = t.view(10_000, 23990.0)
    assert v["basis"] == 11.5 and v["cvd"] == 60 - 200 and v["levels"] == 5 and t.cols_after(0)[0][0] == 1


def test_tape_walls_pulled_vs_filled():
    from scalper.flow.tape import Tape

    t = Tape("NIFTY", "NIFTYFUT", "NFO", 65, 0.1, Config().flow)
    t.on_tick(0, _pk(_ladder(24000.0, -0.5, 5), _ladder(24000.5, 0.5, 5), 24000.0, 50_000))
    bids = _ladder(24000.0, -0.5, 20)
    t.on_tick(1_000, _pk(bids, _ladder(24000.5, 0.5, 20, wall=(24003.0, 200 * 65)), level=20))
    assert ("offer", 24003.0) in t.walls
    ev = t.on_tick(5_000, _pk(bids, _ladder(24000.5, 0.5, 20), level=20))
    assert ev and "PULLED" in ev[0][2] and t.fates[-1][4] == "pulled"
    t.on_tick(6_000, _pk(_ladder(24000.0, -0.5, 20, wall=(23998.0, 300 * 65)), _ladder(24000.5, 0.5, 20), level=20))
    t.on_tick(9_000, _pk(_ladder(24000.0, -0.5, 5), _ladder(24000.5, 0.5, 5), 23998.0, 50_000 + 300 * 65))
    ev = t.on_tick(10_000, _pk(_ladder(23997.5, -0.5, 20), _ladder(23998.0, 0.5, 20), level=20))
    assert ev and "taken out" in ev[0][2] and t.fates[-1][4] == "filled"


def test_engine_routes_future_into_frame_cols_history_events_and_read():
    eng = _engine()
    eng.add_future("NIFTY", "NFUT", "NFO", 65, 0.1)
    t0 = ms(10, 0)
    for i in range(5):
        _feed(eng, t0 + i * 2_000)
        eng.on_tick(t0 + i * 2_000, "NFUT", "NFO", _pk(_ladder(24040.0, -0.5, 5), _ladder(24040.5, 0.5, 5),
                                                       24040.5, 10_000 + i * 65 * 10))
        eng.analyze()
    eng.on_tick(t0 + 11_000, "NFUT", "NFO", _pk(_ladder(24042.0, -0.5, 5), _ladder(24043.0, 0.5, 5), 24042.5,
                                                 10_000 + 4 * 650 + 80 * 65))
    f = eng.frame("NIFTY")
    assert f["tape"]["sym"] == "NFUT" and f["tape"]["basis"] == 42.5 and f["hud"]["liq"]["warm"] is False
    assert len(eng.tape_cols("NIFTY", 0)) >= 3 and eng.tape_cols("NIFTY", 10**9) == []
    assert eng.history("NIFTY")["tape"]
    assert any(e["text"].startswith("NIFTY FUT BUY sweep") for e in eng.books["NIFTY"].events)
    assert "BOOK" in {x["l"] for x in eng.books["NIFTY"].read["lines"]}


def test_depth_recorder_throttles_and_survives_a_truncated_file(tmp_path):
    from scalper.flow.record import DepthRecorder, read_depth

    p = tmp_path / "d.jsonl.gz"
    rec = DepthRecorder(p)
    rec.want = {("F", "NFO"): 0, ("O", "NFO"): 1000}
    rec.meta({"day": "2026-11-03"})
    book = _pk([(10.0, 65)], [(10.1, 130)], 10.05, 1000)
    for ts, sym, d in [(1_000, "F", book), (1_100, "F", book), (1_000, "O", book), (1_500, "O", book),
                       (1_600, "O", {**book, "depth_level": 20}), (2_100, "O", book), (2_200, "X", book)]:
        rec.add(ts, sym, "NFO", d)
    rec.close()
    rows = list(read_depth(p))
    assert rows[0] == {"meta": {"day": "2026-11-03"}}
    assert [(r["s"], r["t"], r["l"]) for r in rows[1:]] == [("F", 1000, 5), ("F", 1100, 5), ("O", 1000, 5),
                                                              ("O", 1600, 20), ("O", 2100, 5)]
    assert rows[1]["b"] == [[10.0, 65, None]] and rows[1]["p"] == 10.05
    raw = p.read_bytes()
    p.write_bytes(raw[: len(raw) - 12])
    assert len(list(read_depth(p))) <= len(rows)


def test_data_updater_sessions_steps_and_notes(tmp_path, monkeypatch):
    from scalper.flow import updater as U

    tue = date(2026, 11, 3)
    at_ = lambda d, hm: int(datetime(d.year, d.month, d.day, *map(int, hm.split(":")), tzinfo=IST).timestamp() * 1000)
    assert U.last_session(at_(tue, "15:00")) == date(2026, 11, 2) and U.last_session(at_(tue, "16:00")) == tue
    assert U.last_session(at_(date(2026, 11, 2), "10:00")) == date(2026, 10, 30)
    assert U.last_session(at_(date(2026, 11, 7), "12:00")) == date(2026, 11, 6)
    assert U.market_hours(at_(tue, "10:00")) and not U.market_hours(at_(tue, "15:40"))
    for name in ("NIFTY", "SENSEX"):
        (tmp_path / "data/market/options" / name / "chain").mkdir(parents=True)
        (tmp_path / "data/market/options" / name / "chain" / "2026-10-30.parquet").write_bytes(b"")
    assert U.freshness(tmp_path) == {"index": None, "NIFTY": "2026-10-30", "SENSEX": "2026-10-30"}

    monkeypatch.setattr(U, "ROOT", tmp_path)
    monkeypatch.setattr(U, "market_hours", lambda now: True)
    up = U.DataUpdater("config/dhan.yaml", "env/dhan.env", "chetan")
    ran = []

    def fake_exec(cmd, log, cwd=None):
        ran.append(cmd)
        up.lines.append("Index top-up done")
        return 0

    up._exec = fake_exec
    assert up.start() and not up.start() or up.state == "done"
    up.thread.join(5)
    assert up.state == "done" and len(ran) == 1 and ran[0][2] == "scalper.index_update" and "--env-file" in ran[0]
    notes = up.drain()
    assert "chains skipped: market open" in notes[0][1] and notes[-1][1].startswith("Data updated")
    assert up.view()["stale"] is True  # index still missing in tmp root

    up._exec = lambda cmd, log, cwd=None: (up.lines.append("No Dhan session"), 1)[1]
    up.start()
    up.thread.join(5)
    assert up.state == "failed" and "No Dhan session" in up.drain()[-1][1]

    class Up:
        n = 0

        def start(self):
            Up.n += 1

    srv = FlowServer(FlowEngine(Config().flow, "replay"), None, 10, updater=Up())
    srv.on_message(None, {"t": "update"})
    assert Up.n == 1


def test_breadth_from_order_flow_market_file_read_line_and_narrow_rally_event():
    import socket

    from scalper.flow.breadth import OrderFlowServer, load_universe, summarize

    sectors, n50 = load_universe()
    assert len(n50) == 50 and "RELIANCE" in n50 and len(sectors) > 150 and sectors["HDFCBANK"] == "Private Banks"
    n50s = sorted(n50)
    others = sorted(s for s in sectors if s not in n50)[:20]
    series = [{"s": "NIFTY", "sp": 24100.0, "pc": 24000.0}]
    series += [{"s": s, "sp": 101.0 if i < 10 else 100.2 if i < 20 else 98.0, "pc": 100.0} for i, s in enumerate(n50s)]
    series += [{"s": s, "sp": 102.0, "pc": 100.0} for s in others]
    br = summarize({"t": ms(10, 0), "date": "2026-11-02", "series": series}, sectors, n50)
    assert br["n50"] == [10, 10, 30] and br["fno"] == [30, 10, 30] and br["n"] == 70 and br["nifty"] == 0.42
    assert br["lead"][1] >= br["lag"][1]
    assert summarize({"series": series[:5]}, sectors, n50) is None

    eng = _engine()
    eng.breadth = br
    b = eng.books["NIFTY"]
    _feed(eng, ms(10, 0), f=24100.0)
    b.prev_close = 24000.0
    eng.analyze()
    assert any(x["l"] == "BREADTH" and x["t"].startswith("F&O 30↑ 30↓ (10 flat) · N50 10↑ 30↓") for x in b.read["lines"])
    _feed(eng, ms(10, 1), f=24100.0)
    _feed(eng, ms(10, 2), f=24100.0)
    assert any("narrow rally" in e["text"] for e in b.events)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert "already running" in OrderFlowServer(f"http://127.0.0.1:{s.getsockname()[1]}").start()
    srv = FlowServer(FlowEngine(Config().flow, "live"), None, 10, order_flow_url="http://127.0.0.1:8790")
    assert srv.hello()["order_flow"] == "http://127.0.0.1:8790"


def test_server_serves_page_and_blocks_traversal():
    srv = FlowServer(FlowEngine(Config().flow, "replay"), None, 10)

    class Conn:
        def respond(self, status, text):
            return ("respond", status)

    class Req:
        def __init__(self, path):
            self.path = path

    ok = srv.process_request(Conn(), Req("/"))
    assert ok.status_code == 200 and b"<canvas" in ok.body and ok.headers["Content-Type"].startswith("text/html")
    assert srv.process_request(Conn(), Req("/app.js")).headers["Content-Type"] == "text/javascript"
    assert srv.process_request(Conn(), Req("/../../config/dhan.yaml"))[0] == "respond"
    assert srv.process_request(Conn(), Req("/ws")) is None
