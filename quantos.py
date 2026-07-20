from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
CONFIG = ROOT / "config" / "lab.json"
DATA = ROOT / "data" / "raw" / "XAUUSD15.csv.gz"
DATA_MANIFEST = ROOT / "data" / "manifest.json"
DATA_HEADER = "timestamp,open,high,low,close,volume,timeframe"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:length]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def eig(job: dict[str, Any]) -> float:
    return (
        0.35 * float(job.get("uncertainty", 0))
        + 0.30 * float(job.get("expected_improvement", 0))
        + 0.20 * float(job.get("search_reduction", 0))
        + 0.15 * float(job.get("validation_priority", 0))
    )


def mode_for(ledger: dict[str, Any]) -> str:
    run_id = int(ledger.get("run_id", 0)) + 1
    triggers = set(ledger.get("deep_triggers", []))
    allowed = {"CONTRADICTION", "REGIME_SHIFT", "OOS_DECAY", "ARCH_DECISION", "FINAL_SELECTION"}
    if triggers & allowed or int(ledger.get("stall_count", 0)) >= 3:
        return "DEEP"
    if run_id % 24 == 0 or run_id % 6 == 0:
        return "MID"
    return "LIGHT"


def budget(mode: str, leader_score: float | None = None) -> int:
    if mode == "DEEP":
        return 32
    if mode == "MID":
        return 24
    return 16 if leader_score is not None and leader_score > 0 else 8


def profit_factor(pnls: list[float]) -> float | None:
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    if gross_loss == 0:
        return None if gross_profit == 0 else math.inf
    return gross_profit / gross_loss


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


@dataclass
class Result:
    experiment_hash: str
    architecture: str
    status: str
    progress: str
    diagnosis: str
    metrics: dict[str, Any]
    evidence: str
    timestamp: str


def select_job(queue: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    completed = set(index.get("hashes", {}))
    candidates = []
    for job in queue.get("jobs", []):
        signature = stable_hash(job)
        if signature not in completed:
            candidates.append((eig(job), job, signature))
    if not candidates:
        return None
    _, job, signature = max(candidates, key=lambda x: x[0])
    return {**job, "signature": signature}


def gate_diagnosis(metrics: dict[str, Any], gates: dict[str, float]) -> list[str]:
    failures = []
    checks = [
        ("pf", "pf_global_min", "PF_LOW", lambda a, b: a >= b),
        ("pf_year_min", "pf_year_min", "Y_LOW", lambda a, b: a >= b),
        ("pf_half_min", "pf_half_min", "H_LOW", lambda a, b: a >= b),
        ("max_drawdown", "max_drawdown_max", "DD_HIGH", lambda a, b: a <= b),
        ("trades_per_day", "trades_per_day_min", "FREQ_LOW", lambda a, b: a >= b),
        ("pnl_daily", "pnl_daily_min", "PNL_LOW", lambda a, b: a >= b),
    ]
    for metric, gate, code, test in checks:
        value = metrics.get(metric)
        if value is None or not test(float(value), float(gates[gate])):
            failures.append(code)
    return failures


def validate_data() -> tuple[bool, str]:
    if not DATA.exists():
        return False, f"{DATA.relative_to(ROOT)} missing"
    if not DATA_MANIFEST.exists():
        return False, f"{DATA_MANIFEST.relative_to(ROOT)} missing"

    try:
        manifest = load(DATA_MANIFEST)
        datasets = manifest.get("datasets", [])
        m15 = next(item for item in datasets if item.get("timeframe") == "m15")
        expected_hash = str(m15["sha256"])
        actual_hash = file_sha256(DATA)
        if actual_hash != expected_hash:
            return False, f"M15 checksum mismatch expected={expected_hash} actual={actual_hash}"
        if int(m15.get("rows", 0)) <= 0:
            return False, "M15 manifest reports no rows"
        with gzip.open(DATA, "rt", encoding="utf-8", newline="") as handle:
            header = handle.readline().strip()
            first = handle.readline().strip()
        if header != DATA_HEADER:
            return False, f"Unexpected M15 header: {header!r}"
        if not first:
            return False, "M15 dataset contains no data rows"
    except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        return False, f"Data validation failed: {exc}"

    coverage = f"{m15.get('first_timestamp')}..{m15.get('last_timestamp')}"
    return True, f"{DATA.relative_to(ROOT)} sha256={actual_hash} rows={m15['rows']} coverage={coverage}"


def blocked_result(job: dict[str, Any], run_id: int, evidence: str) -> Result:
    payload = {"run_id": run_id, "job": job, "reason": "DATA_BLOCK", "evidence": evidence}
    return Result(
        experiment_hash=stable_hash(payload),
        architecture=job.get("architecture", "NA"),
        status="BLOCKED",
        progress="ANOMALY_FOUND",
        diagnosis="DATA_BLOCK",
        metrics={},
        evidence=evidence,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def run_once() -> Result:
    cfg = load(CONFIG)
    ledger_path = STATE / "ledger.json"
    queue_path = STATE / "queue.json"
    index_path = STATE / "experiment_index.json"
    leaders_path = STATE / "leaderboard.json"
    ledger, queue, index, leaders = map(load, [ledger_path, queue_path, index_path, leaders_path])
    job = select_job(queue, index)
    if job is None:
        ledger["stall_count"] = int(ledger.get("stall_count", 0)) + 1
        ledger["next_action"] = "Generate a new high-EIG job"
        save(ledger_path, ledger)
        return Result(
            stable_hash({"empty": ledger["run_id"]}),
            "NA",
            "NO_PROGRESS",
            "SEARCH_SPACE_REDUCED",
            "QUEUE_EMPTY",
            {},
            "queue exhausted",
            datetime.now(timezone.utc).isoformat(),
        )

    run_id = int(ledger.get("run_id", 0)) + 1
    mode = mode_for(ledger)
    data_ok, data_evidence = validate_data()
    if not data_ok:
        result = blocked_result(job, run_id, data_evidence)
    else:
        ledger["known_failures"] = [
            item for item in ledger.get("known_failures", []) if item != "DATA_BLOCK"
        ]
        # Mechanical backtest engines plug in here. This controller intentionally
        # refuses to invent metrics when the reproducible evaluator is absent.
        result = Result(
            experiment_hash=stable_hash(
                {"job": job, "run": run_id, "data_sha256": file_sha256(DATA)}
            ),
            architecture=job["architecture"],
            status="NO_PROGRESS",
            progress="CONFIG_EVALUATED",
            diagnosis="EVALUATOR_PENDING",
            metrics={},
            evidence=data_evidence,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    ledger.update(
        {
            "version": int(ledger.get("version", 0)) + 1,
            "run_id": run_id,
            "mode": mode,
            "last_experiment": asdict(result),
            "next_action": (
                "Resolve evaluator/data blocker"
                if result.status in {"BLOCKED", "NO_PROGRESS"}
                else "Run next highest-EIG job"
            ),
        }
    )
    if result.status == "BLOCKED":
        ledger["known_failures"] = sorted(
            set(ledger.get("known_failures", []) + ["DATA_BLOCK"])
        )
    index.setdefault("hashes", {})[job["signature"]] = result.experiment_hash
    save(ledger_path, ledger)
    save(index_path, index)
    save(ROOT / "artifacts" / "latest_run.json", asdict(result))
    return result


def status() -> None:
    ledger = load(STATE / "ledger.json")
    leaders = load(STATE / "leaderboard.json")
    print(json.dumps({"ledger": ledger, "leaderboard": leaders}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "status"], nargs="?", default="status")
    args = parser.parse_args()
    if args.command == "run":
        print(json.dumps(asdict(run_once()), indent=2))
    else:
        status()


if __name__ == "__main__":
    main()
