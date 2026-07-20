#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const MAGIC = Buffer.from([0x51,0x58,0x31,0x35,0x01]);
const HEADER='timestamp,open,high,low,close,volume,timeframe\n';

function sha256(buf){return crypto.createHash('sha256').update(buf).digest('hex');}
function readUVarint(buf,state){let value=0;let shift=0;while(true){if(state.pos>=buf.length)throw new Error('truncated varint');const b=buf[state.pos++];value+=(b&0x7f)*(2**shift);if(b<0x80)return value;shift+=7;if(shift>49)throw new Error('varint too long');}}
function unzigzag(n){return (n % 2 === 0) ? n/2 : -(n+1)/2;}
function fmt(v){const sign=v<0?'-':'';v=Math.abs(v);return `${sign}${Math.floor(v/1000)}.${String(v%1000).padStart(3,'0')}`;}
function seedParts(dir){return fs.readdirSync(dir).filter(n=>/^XAUUSD15\.seed\.br\.b64\.part-\d+$/.test(n)).sort().map(n=>path.join(dir,n));}
function restore(seedDir=path.join(ROOT,'data','seed'), output=path.join(ROOT,'data','raw','XAUUSD15.csv.gz')){
 const parts=seedParts(seedDir);if(!parts.length)throw new Error(`No seed parts in ${seedDir}`);
 const encoded=parts.map(p=>fs.readFileSync(p,'ascii').trim()).join('');
 const packed=Buffer.from(encoded,'base64');
 const raw=zlib.brotliDecompressSync(packed);
 if(!raw.subarray(0,5).equals(MAGIC))throw new Error('bad seed magic');
 const state={pos:5};const count=raw.readUInt32LE(state.pos);state.pos+=4;let ts=Number(raw.readBigInt64LE(state.pos));state.pos+=8;
 let op=raw.readInt32LE(state.pos);state.pos+=4;let hi=raw.readInt32LE(state.pos);state.pos+=4;let lo=raw.readInt32LE(state.pos);state.pos+=4;let cl=raw.readInt32LE(state.pos);state.pos+=4;
 const lines=[HEADER.trimEnd(),`${ts},${fmt(op)},${fmt(hi)},${fmt(lo)},${fmt(cl)},0,15`];let prevClose=cl;
 for(let i=1;i<count;i++){
  ts+=readUVarint(raw,state)*60000;op=prevClose+unzigzag(readUVarint(raw,state));const hoff=readUVarint(raw,state);const loff=readUVarint(raw,state);cl=op+unzigzag(readUVarint(raw,state));hi=Math.max(op,cl)+hoff;lo=Math.min(op,cl)-loff;
  if(!(hi>=Math.max(op,cl)&&lo<=Math.min(op,cl)&&lo>0))throw new Error(`invalid OHLC row ${i}`);
  lines.push(`${ts},${fmt(op)},${fmt(hi)},${fmt(lo)},${fmt(cl)},0,15`);prevClose=cl;
 }
 if(state.pos!==raw.length)throw new Error(`trailing bytes ${raw.length-state.pos}`);
 fs.mkdirSync(path.dirname(output),{recursive:true});const csv=Buffer.from(`${lines.join('\n')}\n`,'utf8');const compressed=zlib.gzipSync(csv,{level:9,mtime:0});fs.writeFileSync(output,compressed);
 const info={source:'embedded deterministic XAUUSD seed',seed_format:'QX15-v1+brotli+base64-parts',seed_sha256:sha256(packed),rows:count,first_timestamp:Number(lines[1].split(',')[0]),last_timestamp:ts,sha256:sha256(compressed),bytes_gzip:compressed.length,path:path.relative(ROOT,output).replaceAll('\\','/')};console.log(JSON.stringify(info,null,2));return info;
}
restore();
