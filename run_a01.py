from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from evaluator import evaluate_a01

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
ARTIFACTS = ROOT / "artifacts"
DATA = ROOT / "data" / "raw" / "XAUUSD15.csv.gz"
MANIFEST = ROOT / "data" / "manifest.json"
CONFIG = ROOT / "config" / "lab.json"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    cfg = load(CONFIG)
    manifest = load(MANIFEST)
    m15 = next(item for item in manifest["datasets"] if item["timeframe"] == "m15")
    actual = sha256(DATA)
    if actual != m15["sha256"]:
        raise RuntimeError(f"dataset checksum mismatch expected={m15['sha256']} actual={actual}")

    ledger_path = STATE / "ledger.json"
    leaderboard_path = STATE / "leaderboard.json"
    ledger = load(ledger_path)
    leaderboard = load(leaderboard_path)

    result = evaluate_a01(DATA, cfg, ARTIFACTS, actual)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    run_id = int(ledger.get("run_id", 0)) + 1

    ledger.update(
        {
            "version": int(ledger.get("version", 0)) + 1,
            "run_id": run_id,
            "mode": "DEEP",
            "last_experiment": result,
            "stall_count": 0,
            "next_action": (
                "Validate A01 robustness and parameter neighborhood"
                if result["status"] == "PASS"
                else "Diagnose A01 failure and generate one bounded refinement"
            ),
        }
    )
    ledger["known_failures"] = [
        item for item in ledger.get("known_failures", []) if item != "EVALUATOR_PENDING"
    ]
    pointers = list(ledger.get("artifact_pointers", []))
    if result["evidence"] not in pointers:
        pointers.append(result["evidence"])
    ledger["artifact_pointers"] = pointers[-20:]

    leaders = list(leaderboard.get("leaders", []))
    candidate = {
        "architecture": "A01",
        "experiment_hash": result["experiment_hash"],
        "status": result["status"],
        "metrics": result["metrics"],
        "artifact": result["evidence"],
        "run_id": run_id,
    }
    leaders = [x for x in leaders if x.get("architecture") != "A01"] + [candidate]
    leaders.sort(
        key=lambda x: (
            x.get("status") == "PASS",
            float(x.get("metrics", {}).get("pf", 0.0)),
            float(x.get("metrics", {}).get("net_pnl", 0.0)),
        ),
        reverse=True,
    )
    leaderboard["leaders"] = leaders[:20]
    leaderboard["updated_run"] = run_id

    save(ledger_path, ledger)
    save(leaderboard_path, leaderboard)
    save(ARTIFACTS / "latest_run.json", result)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
