# Market data

Place `XAUUSD15.csv.gz` under `data/raw/`.

Expected format: tab-separated rows without a header:

```text
UTC timestamp    open    high    low    close    volume
```

Optional `XAUUSD1.csv.gz` may be supplied for intrabar validation. Without M1 coverage, the engine must retain pessimistic SL-first sequencing.
