#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { getHistoricalRates } from 'dukascopy-node';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const RAW = path.join(ROOT, 'data', 'raw');
const CACHE = path.join(ROOT, '.cache', 'dukascopy');
const MANIFEST = path.join(ROOT, 'data', 'manifest.json');
const SOURCE_VERSION = 'dukascopy-node@1.46.4';
const CSV_HEADER = 'timestamp,open,high,low,close,volume,timeframe';

const SPECS = [
  {
    timeframe: 'm15',
    minutes: 15,
    file: 'XAUUSD15.csv.gz',
    start: process.env.QUANTOS_M15_START || '2022-04-26T00:00:00.000Z',
    chunkDays: 180,
  },
  {
    timeframe: 'm1',
    minutes: 1,
    file: 'XAUUSD1.csv.gz',
    start: process.env.QUANTOS_M1_START || '2026-04-07T00:00:00.000Z',
    chunkDays: 14,
  },
];

function utcDayStart(date = new Date()) {
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function ensureDirs() {
  fs.mkdirSync(RAW, { recursive: true });
  fs.mkdirSync(CACHE, { recursive: true });
  fs.mkdirSync(path.dirname(MANIFEST), { recursive: true });
}

function parseExisting(filePath) {
  if (!fs.existsSync(filePath)) return new Map();
  const text = zlib.gunzipSync(fs.readFileSync(filePath)).toString('utf8').trim();
  if (!text) return new Map();
  const lines = text.split(/\r?\n/);
  const firstDataLine = lines[0].startsWith('timestamp,') ? 1 : 0;
  const rows = new Map();
  for (let i = firstDataLine; i < lines.length; i += 1) {
    const parts = lines[i].split(',');
    if (parts.length < 7) continue;
    const timestamp = Number(parts[0]);
    if (!Number.isFinite(timestamp)) continue;
    rows.set(timestamp, {
      timestamp,
      open: Number(parts[1]),
      high: Number(parts[2]),
      low: Number(parts[3]),
      close: Number(parts[4]),
      volume: Number(parts[5]),
      timeframe: Number(parts[6]),
    });
  }
  return rows;
}

function validateRow(row, expectedMinutes) {
  const values = [row.timestamp, row.open, row.high, row.low, row.close];
  if (!values.every(Number.isFinite)) throw new Error(`Non-finite row: ${JSON.stringify(row)}`);
  if (row.timestamp < 1_000_000_000_000) throw new Error(`Timestamp is not milliseconds: ${row.timestamp}`);
  if (row.high < Math.max(row.open, row.close, row.low)) throw new Error(`Invalid high: ${JSON.stringify(row)}`);
  if (row.low > Math.min(row.open, row.close, row.high)) throw new Error(`Invalid low: ${JSON.stringify(row)}`);
  if (row.open <= 0 || row.high <= 0 || row.low <= 0 || row.close <= 0) throw new Error(`Non-positive price: ${JSON.stringify(row)}`);
  if (row.timeframe !== expectedMinutes) throw new Error(`Wrong timeframe: ${JSON.stringify(row)}`);
}

function serialise(rows) {
  const ordered = [...rows.values()].sort((a, b) => a.timestamp - b.timestamp);
  const lines = [CSV_HEADER];
  for (const row of ordered) {
    lines.push([
      row.timestamp,
      row.open,
      row.high,
      row.low,
      row.close,
      Number.isFinite(row.volume) ? row.volume : 0,
      row.timeframe,
    ].join(','));
  }
  return { ordered, text: `${lines.join('\n')}\n` };
}

async function downloadChunk(spec, from, to) {
  return getHistoricalRates({
    instrument: 'xauusd',
    dates: { from, to },
    timeframe: spec.timeframe,
    priceType: 'bid',
    utcOffset: 0,
    volumes: true,
    ignoreFlats: true,
    format: 'array',
    batchSize: spec.timeframe === 'm1' ? 7 : 30,
    pauseBetweenBatchesMs: 100,
    useCache: true,
    cacheFolderPath: CACHE,
    retryCount: 5,
    retryOnEmpty: true,
    failAfterRetryCount: true,
    pauseBetweenRetriesMs: 1500,
  });
}

async function updateSpec(spec, endExclusive) {
  const filePath = path.join(RAW, spec.file);
  const rows = parseExisting(filePath);
  const intervalMs = spec.minutes * 60_000;
  const configuredStart = new Date(spec.start);
  if (Number.isNaN(configuredStart.getTime())) throw new Error(`Invalid start date: ${spec.start}`);

  let cursor = configuredStart;
  if (rows.size > 0) {
    const last = Math.max(...rows.keys());
    cursor = new Date(Math.max(configuredStart.getTime(), last + intervalMs));
  }

  let downloaded = 0;
  while (cursor < endExclusive) {
    const chunkEnd = new Date(Math.min(
      endExclusive.getTime(),
      cursor.getTime() + spec.chunkDays * 86_400_000,
    ));
    const chunk = await downloadChunk(spec, cursor, chunkEnd);
    for (const item of chunk) {
      const [timestamp, open, high, low, close, volume = 0] = item;
      const row = {
        timestamp: Number(timestamp),
        open: Number(open),
        high: Number(high),
        low: Number(low),
        close: Number(close),
        volume: Number(volume),
        timeframe: spec.minutes,
      };
      validateRow(row, spec.minutes);
      rows.set(row.timestamp, row);
      downloaded += 1;
    }
    cursor = chunkEnd;
  }

  if (rows.size === 0) throw new Error(`No data available for ${spec.timeframe}`);
  const { ordered, text } = serialise(rows);
  for (let i = 1; i < ordered.length; i += 1) {
    if (ordered[i].timestamp <= ordered[i - 1].timestamp) throw new Error(`Non-monotonic ${spec.timeframe}`);
  }

  const compressed = zlib.gzipSync(Buffer.from(text, 'utf8'), { level: 9, mtime: 0 });
  const oldHash = fs.existsSync(filePath) ? sha256(fs.readFileSync(filePath)) : null;
  const newHash = sha256(compressed);
  if (oldHash !== newHash) fs.writeFileSync(filePath, compressed);

  return {
    source: 'Dukascopy Bank historical feed via dukascopy-node',
    source_version: SOURCE_VERSION,
    instrument: 'XAUUSD',
    price_side: 'bid',
    timeframe: spec.timeframe,
    timeframe_minutes: spec.minutes,
    path: path.relative(ROOT, filePath).replaceAll('\\', '/'),
    rows: ordered.length,
    first_timestamp: new Date(ordered[0].timestamp).toISOString(),
    last_timestamp: new Date(ordered.at(-1).timestamp).toISOString(),
    sha256: newHash,
    bytes_gzip: compressed.length,
    downloaded_rows_this_run: downloaded,
    changed: oldHash !== newHash,
  };
}

async function main() {
  ensureDirs();
  const end = process.env.QUANTOS_DATA_END
    ? new Date(process.env.QUANTOS_DATA_END)
    : utcDayStart();
  if (Number.isNaN(end.getTime())) throw new Error('Invalid QUANTOS_DATA_END');

  const datasets = [];
  for (const spec of SPECS) datasets.push(await updateSpec(spec, end));

  const manifest = {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    complete_through_utc_exclusive: end.toISOString(),
    reproducibility: {
      instrument: 'xauusd',
      utc_offset: 0,
      ignore_flats: true,
      price_type: 'bid',
      source_version: SOURCE_VERSION,
    },
    datasets,
  };
  fs.writeFileSync(MANIFEST, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  console.log(JSON.stringify(manifest, null, 2));
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
