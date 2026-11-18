from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

import yaml


@dataclass
class OpenAlgoCfg:
    host: str = "http://127.0.0.1:5000"
    ws_url: str = "ws://127.0.0.1:8765"
    api_key_env: str = "OPENALGO_API_KEY"


@dataclass
class NotifyCfg:
    telegram_username: str = ""
    min_interval_sec: float = 2.0


@dataclass
class UnderlyingCfg:
    name: str = "NIFTY"
    index_exchange: str = "NSE_INDEX"
    fo_exchange: str = "NFO"
    enabled: bool = True


@dataclass
class SessionCfg:
    wake: str = "08:45"
    start: str = "09:15"
    expected_move_at: str = "09:20"
    flatten: str = "15:20"
    end: str = "15:31"
    eval_interval_sec: int = 5


@dataclass
class UniverseCfg:
    strike_count: int = 15
    recenter_minutes: int = 15
    recenter_strikes: int = 4
    expiry_only: bool = True
    vix_symbol: str = "INDIAVIX"
    vix_exchange: str = "NSE_INDEX"


@dataclass
class RiskCfg:
    R: float = 1500.0


@dataclass
class RegimeCfg:
    min_bars: int = 10
    confirm_min: int = 3
    trend_frac: float = 0.6
    trend_sma_bars: int = 20
    range_lookback_min: int = 60
    range_frac: float = 0.25
    straddle_lookback_min: int = 30
    straddle_decay_pct: float = 3.0
    wall_band_pct: float = 1.5
    breakout_after: str = "14:30"
    breakout_lookback_min: int = 45
    breakout_expansion: float = 1.5
    chaos_vix_jump_pct: float = 8.0
    chaos_spread_pct: float = 3.0
    stale_sec: int = 20


@dataclass
class S1Cfg:
    enabled: bool = True
    window: tuple[str, str] = ("09:30", "12:30")
    max_itm_at_open: int = 5
    min_drop_pct: float = 50.0
    min_red_5m: int = 3
    arm_valid_min: int = 25
    min_bounce_pct: float = 3.0
    min_premium: float = 30.0
    max_spread_pct: float = 1.5
    require_index_turn: bool = True
    stop_buffer_pct: float = 2.0
    stop_atr_mult: float = 1.0
    initial_pct: float = 60.0
    cooldown_min: int = 10
    max_alerts_per_day: int = 4


@dataclass
class S2Cfg:
    enabled: bool = True
    window: tuple[str, str] = ("14:30", "15:12")
    max_otm: int = 4
    premium_min: float = 3.0
    premium_max: float = 15.0
    rise_lookback_min: int = 3
    min_rise_pct: float = 30.0
    min_vol_ratio: float = 2.0
    max_spread_pct: float = 6.0
    budget_per_try: float = 1500.0
    max_tries: int = 2
    cooldown_min: int = 10


@dataclass
class S3Cfg:
    enabled: bool = True
    window: tuple[str, str] = ("11:00", "13:30")
    exit: str = "15:15"
    wing_strikes: int = 3
    range_lookback_min: int = 60
    range_mult: float = 0.5
    decay_lookback_min: int = 30
    min_decay_pct: float = 3.0
    leg_stop_pct: float = 50.0
    max_spread_pct: float = 2.0
    max_loss_rs: float = 6000.0
    max_alerts_per_day: int = 1
    paper: bool = True


@dataclass
class HarvestCfg:
    enabled: bool = True
    paper: bool = True
    capital_rs: float = 1_000_000.0
    risk_pct: float = 1.0
    max_open_risk_pct: float = 2.5
    pause_drawdown_pct: float = 6.0
    dte_entry: int = 4
    entry_time: str = "15:22"
    mark_time: str = "15:25"
    deadline: str = "15:29"
    vix_lookback: int = 250
    vix_min_days: int = 120
    vix_bar: str = "15:25"
    max_vix_age_days: int = 10
    wing_mult: float = 2.0
    min_wing: int = 4
    max_wing: int = 10
    max_spread_cost_pct: float = 5.0
    events: list[str] = field(default_factory=list)


@dataclass
class FlowCfg:
    host: str = "127.0.0.1"
    port: int = 8787
    strikes: int = 12
    expiries: int = 2
    depth20: bool = True
    depth20_underlying: str = "NIFTY"
    frame_hz: float = 10.0
    recenter_strikes: int = 3
    buildup_min: int = 5
    buildup_min_dp_pct: float = 2.0
    oi_event_mult: float = 5.0
    oi_event_min_pct: float = 3.0
    burst_mult: float = 4.0
    burst_side_share: float = 0.7
    burst_min_lots: float = 200.0
    max_events_per_min: int = 3
    iv_event_pts: float = 1.0
    wall_mult: float = 5.0
    event_cooldown_min: int = 10
    bounce_lots: int = 5
    futures: bool = True
    tape_col_ms: int = 2000
    tape_cols: int = 450
    absorb_mult: float = 3.0
    absorb_min_lots: float = 150.0
    sweep_levels: int = 3
    sweep_min_lots: float = 50.0
    fut_wall_mult: float = 4.0
    fut_wall_min_lots: float = 100.0
    liq_spread_mult: float = 1.6
    liq_depth_frac: float = 0.6
    record_depth: bool = True
    record_strikes: int = 5
    order_flow_url: str = "http://127.0.0.1:8790"
    order_flow_autostart: bool = True
    breadth: bool = True


@dataclass
class PathsCfg:
    data_dir: str = "data"
    journal: str = "data/journal.sqlite"


def _default_underlyings() -> list[UnderlyingCfg]:
    return [
        UnderlyingCfg("NIFTY", "NSE_INDEX", "NFO"),
        UnderlyingCfg("SENSEX", "BSE_INDEX", "BFO"),
    ]


@dataclass
class Config:
    name: str = "dhan"
    openalgo: OpenAlgoCfg = field(default_factory=OpenAlgoCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)
    underlyings: list[UnderlyingCfg] = field(default_factory=_default_underlyings)
    session: SessionCfg = field(default_factory=SessionCfg)
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    s1: S1Cfg = field(default_factory=S1Cfg)
    s2: S2Cfg = field(default_factory=S2Cfg)
    s3: S3Cfg = field(default_factory=S3Cfg)
    harvest: HarvestCfg = field(default_factory=HarvestCfg)
    flow: FlowCfg = field(default_factory=FlowCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)

    def api_key(self) -> str:
        return os.environ.get(self.openalgo.api_key_env, "").strip()


def _build(cls: type, data: Any, path: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Config section '{path or 'root'}' must be a mapping")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown config keys in '{path or 'root'}': {sorted(unknown)}")
    hints = get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value, hint = data[f.name], hints[f.name]
        origin = getattr(hint, "__origin__", None)
        if dataclasses.is_dataclass(hint):
            value = _build(hint, value, f"{path}.{f.name}".lstrip("."))
        elif origin is list and dataclasses.is_dataclass(hint.__args__[0]):
            value = [_build(hint.__args__[0], v, f"{path}.{f.name}[{i}]".lstrip(".")) for i, v in enumerate(value)]
        elif origin is tuple:
            value = tuple(value)
        kwargs[f.name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    with open(path, encoding="utf-8") as fh:
        return _build(Config, yaml.safe_load(fh), "")


def load_env_file(path: str | Path) -> None:
    """Minimal KEY=VALUE loader; values already in the environment win."""
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
