from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    side: str
    entry: float
    stop: float
    tp1: float
    tp2: float
    lots: float
    pnl: float
    r_multiple: float
    exit_reason: str


def _hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_m15(path: Path) -> pd.DataFrame:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        df = pd.read_csv(handle)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.sort_values("time").drop_duplicates("time").set_index("time")
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if not df.index.is_monotonic_increasing:
        raise ValueError("timestamps are not monotonic")
    return df


def add_features(df: pd.DataFrame, asia_start: int, asia_end: int) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=14).mean().shift(1)
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean().shift(1)
    out["ema50"] = out["close"].ewm(span=50, adjust=False).mean().shift(1)
    out["ema200"] = out["close"].ewm(span=200, adjust=False).mean().shift(1)
    out["date"] = out.index.date

    hours = out.index.hour
    asia_mask = (hours >= asia_start) & (hours < asia_end)
    asia = out.loc[asia_mask].groupby("date").agg(asia_high=("high", "max"), asia_low=("low", "min"))
    out = out.join(asia, on="date")
    out[["asia_high", "asia_low"]] = out.groupby("date")[["asia_high", "asia_low"]].ffill()
    out["asia_mid"] = (out["asia_high"] + out["asia_low"]) / 2.0
    out["hour"] = hours
    return out


def _period_pf(trades: pd.DataFrame, key: pd.Series) -> float:
    values: list[float] = []
    for _, group in trades.groupby(key):
        gp = float(group.loc[group.pnl > 0, "pnl"].sum())
        gl = float(-group.loc[group.pnl < 0, "pnl"].sum())
        if gl == 0:
            values.append(float("inf") if gp > 0 else 0.0)
        else:
            values.append(gp / gl)
    return min(values) if values else 0.0


def evaluate_a01(
    data_path: Path,
    cfg: dict[str, Any],
    artifacts_root: Path,
    data_sha256: str,
) -> dict[str, Any]:
    contract = cfg["contract"]
    gates = cfg["gates"]
    params = {
        "atr_mult": 1.2,
        "tp2_r": 2.0,
        "session_start": 7,
        "session_end": 12,
        "require_ema200": True,
    }
    df = add_features(
        load_m15(data_path),
        int(contract.get("asia_start_hour_utc", 0)),
        int(contract.get("asia_end_hour_utc", 7)),
    )

    initial_capital = float(contract["initial_capital"])
    risk_fraction = float(contract["risk_fraction"])
    cost_per_side_per_001 = float(contract["cost_per_side_per_001"])
    tp1_fraction = float(contract.get("tp1_fraction", 0.5))
    equity = initial_capital
    equity_curve = [equity]
    trades: list[Trade] = []

    i = 201
    while i < len(df) - 1:
        row = df.iloc[i]
        nxt = df.iloc[i + 1]
        if not (params["session_start"] <= int(row.hour) < params["session_end"]):
            i += 1
            continue
        needed = [row.atr14, row.asia_high, row.asia_low, row.ema200]
        if any(pd.isna(x) for x in needed):
            i += 1
            continue

        long_signal = row.low < row.asia_low and row.close > row.asia_low and row.close > row.open
        short_signal = row.high > row.asia_high and row.close < row.asia_high and row.close < row.open
        if params["require_ema200"]:
            long_signal = bool(long_signal and row.close > row.ema200)
            short_signal = bool(short_signal and row.close < row.ema200)
        if not long_signal and not short_signal:
            i += 1
            continue

        side = "BUY" if long_signal else "SELL"
        entry = float(nxt.open)
        stop_dist = float(row.atr14) * float(params["atr_mult"])
        if stop_dist <= 0:
            i += 1
            continue
        stop = entry - stop_dist if side == "BUY" else entry + stop_dist
        tp1 = entry + stop_dist if side == "BUY" else entry - stop_dist
        tp2 = entry + stop_dist * params["tp2_r"] if side == "BUY" else entry - stop_dist * params["tp2_r"]

        # XAUUSD approximation: 1 lot = $100 per $1.00 movement.
        risk_budget = equity * risk_fraction
        raw_lots = risk_budget / (stop_dist * 100.0 + cost_per_side_per_001 * 200.0)
        lots = max(0.01, np.floor(raw_lots * 100.0) / 100.0)
        max_loss = stop_dist * 100.0 * lots
        costs_entry = cost_per_side_per_001 * (lots / 0.01)
        realized = -costs_entry
        remaining = 1.0
        be_active = False
        exit_reason = "TIME"
        exit_price = entry
        exit_idx = min(i + 96, len(df) - 1)

        for j in range(i + 1, min(i + 97, len(df))):
            bar = df.iloc[j]
            if side == "BUY":
                hit_sl = bar.low <= (entry if be_active else stop)
                hit_tp1 = remaining == 1.0 and bar.high >= tp1
                hit_tp2 = bar.high >= tp2
            else:
                hit_sl = bar.high >= (entry if be_active else stop)
                hit_tp1 = remaining == 1.0 and bar.low <= tp1
                hit_tp2 = bar.low <= tp2

            # Pessimistic same-bar ordering: SL first.
            if hit_sl:
                sl_price = entry if be_active else stop
                move = (sl_price - entry) if side == "BUY" else (entry - sl_price)
                realized += move * 100.0 * lots * remaining
                realized -= cost_per_side_per_001 * (lots * remaining / 0.01)
                exit_reason = "BE" if be_active else "SL"
                exit_price = sl_price
                exit_idx = j
                remaining = 0.0
                break
            if hit_tp1:
                realized += stop_dist * 100.0 * lots * tp1_fraction
                realized -= cost_per_side_per_001 * (lots * tp1_fraction / 0.01)
                remaining -= tp1_fraction
                be_active = True
            if hit_tp2 and remaining > 0:
                realized += stop_dist * params["tp2_r"] * 100.0 * lots * remaining
                realized -= cost_per_side_per_001 * (lots * remaining / 0.01)
                exit_reason = "TP2"
                exit_price = tp2
                exit_idx = j
                remaining = 0.0
                break

        if remaining > 0:
            last = df.iloc[exit_idx]
            exit_price = float(last.close)
            move = (exit_price - entry) if side == "BUY" else (entry - exit_price)
            realized += move * 100.0 * lots * remaining
            realized -= cost_per_side_per_001 * (lots * remaining / 0.01)

        equity += realized
        equity_curve.append(equity)
        trades.append(
            Trade(
                entry_time=str(df.index[i + 1]),
                exit_time=str(df.index[exit_idx]),
                side=side,
                entry=entry,
                stop=stop,
                tp1=tp1,
                tp2=tp2,
                lots=lots,
                pnl=realized,
                r_multiple=realized / max(max_loss, 1e-9),
                exit_reason=exit_reason,
            )
        )
        i = max(i + 1, exit_idx + 1)

    trades_df = pd.DataFrame([asdict(t) for t in trades])
    if trades_df.empty:
        metrics = {
            "trades": 0,
            "pf": 0.0,
            "pf_year_min": 0.0,
            "pf_half_min": 0.0,
            "max_drawdown": 0.0,
            "trades_per_day": 0.0,
            "pnl_daily": 0.0,
            "net_pnl": 0.0,
            "win_rate": 0.0,
        }
    else:
        trades_df["entry_time"] = pd.to_datetime(trades_df["entry_time"], utc=True)
        gross_profit = float(trades_df.loc[trades_df.pnl > 0, "pnl"].sum())
        gross_loss = float(-trades_df.loc[trades_df.pnl < 0, "pnl"].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        eq = np.asarray(equity_curve, dtype=float)
        peaks = np.maximum.accumulate(eq)
        dd = float(np.max((peaks - eq) / np.maximum(peaks, 1e-9)))
        calendar_days = max((df.index[-1] - df.index[0]).days, 1)
        half_key = trades_df.entry_time.dt.year.astype(str) + "-H" + ((trades_df.entry_time.dt.month > 6).astype(int) + 1).astype(str)
        metrics = {
            "trades": int(len(trades_df)),
            "pf": pf,
            "pf_year_min": _period_pf(trades_df, trades_df.entry_time.dt.year),
            "pf_half_min": _period_pf(trades_df, half_key),
            "max_drawdown": dd,
            "trades_per_day": float(len(trades_df) / calendar_days),
            "pnl_daily": float(trades_df.pnl.sum() / calendar_days),
            "net_pnl": float(trades_df.pnl.sum()),
            "win_rate": float((trades_df.pnl > 0).mean()),
            "ending_equity": float(equity),
        }

    failures = []
    checks = [
        (metrics["pf"], gates["pf_global_min"], "PF_LOW", lambda a, b: a >= b),
        (metrics["pf_year_min"], gates["pf_year_min"], "Y_LOW", lambda a, b: a >= b),
        (metrics["pf_half_min"], gates["pf_half_min"], "H_LOW", lambda a, b: a >= b),
        (metrics["max_drawdown"], gates["max_drawdown_max"], "DD_HIGH", lambda a, b: a <= b),
        (metrics["trades_per_day"], gates["trades_per_day_min"], "FREQ_LOW", lambda a, b: a >= b),
        (metrics["pnl_daily"], gates["pnl_daily_min"], "PNL_LOW", lambda a, b: a >= b),
    ]
    for value, threshold, code, test in checks:
        if not test(float(value), float(threshold)):
            failures.append(code)

    experiment_hash = _hash({"architecture": "A01", "params": params, "data": data_sha256, "engine": 1})
    out_dir = artifacts_root / "experiments" / experiment_hash
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(params, indent=2) + "\n", encoding="utf-8")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    if not trades_df.empty:
        trades_df.to_csv(out_dir / "trades.csv.gz", index=False, compression="gzip")
    pd.DataFrame({"equity": equity_curve}).to_csv(out_dir / "equity.csv.gz", index=False, compression="gzip")

    return {
        "experiment_hash": experiment_hash,
        "architecture": "A01",
        "status": "PASS" if not failures else "FAIL",
        "progress": "EVALUATED",
        "diagnosis": "PASS" if not failures else ",".join(failures),
        "metrics": metrics,
        "evidence": str(out_dir.relative_to(artifacts_root.parent)).replace("\\", "/"),
    }
