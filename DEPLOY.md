# Deployment

1. Add `data/raw/XAUUSD15.csv.gz`.
2. In repository settings, enable **Actions → Workflow permissions → Read and write permissions**.
3. Run **QuantOS CI**.
4. Run **QuantOS Hourly Micro-Job** manually once.
5. Verify updates to `state/ledger.json` and `artifacts/latest_run.json`.

The workflow commits state changes with `[quantos]` in the commit message to avoid redundant CI loops.
