# LoopsGPT — QuantOS XAUUSD

A repository-backed, token-efficient quantitative research loop for XAUUSD.

## Design

- Hourly atomic research jobs in GitHub Actions.
- Compact versioned ledger and evidence-linked decisions.
- Experiment hashing and deduplication.
- Expected-information-gain queue.
- LIGHT/MID/DEEP adaptive compute.
- Automatic pruning, freezing and periodic audits.
- ChatGPT reserved for high-value synthesis instead of mechanical runs.

## Quick start

```bash
python -m pip install -e '.[dev]'
python quantos.py status
python quantos.py run
pytest
```

Place the M15 dataset at `data/raw/XAUUSD15.csv.gz`. See `data/README.md`.
