# Reproducible market data

QuantOS no longer requires a manual binary upload.

The hourly workflow runs `scripts/bootstrap_data.mjs`, which downloads XAUUSD bid candles from Dukascopy in UTC through the pinned `dukascopy-node@1.46.4` client. It restores the previous daily cache, downloads only missing candles, validates the series, and produces:

- `data/raw/XAUUSD15.csv.gz`
- `data/raw/XAUUSD1.csv.gz`
- `data/manifest.json`

The compressed raw files live in the GitHub Actions cache rather than Git history. On the first run of each UTC day, the workflow also publishes them as a downloadable Actions artifact retained for 14 days.

## Canonical schema

```text
timestamp,open,high,low,close,volume,timeframe
```

`timestamp` is Unix time in milliseconds. Prices are positive decimal values, rows are strictly ordered and deduplicated, and `timeframe` is `15` or `1`.

The manifest records the source version, UTC coverage, row counts, compressed sizes and SHA-256 checksums. QuantOS must fail closed when data or manifest validation fails; it must never fabricate metrics.
