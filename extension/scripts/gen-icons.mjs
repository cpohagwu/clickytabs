#!/usr/bin/env node
// Draws the ClickyTabs orb (neon conic gradient + glow on transparent) and writes public/icons/{16,32,48,128}.png.
// Dependency-free: raw RGBA -> PNG with node:zlib.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { deflateSync } from 'node:zlib';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STOPS = [
  [0, [0, 240, 255]],
  [0.33, [124, 92, 255]],
  [0.66, [255, 43, 214]],
  [1, [0, 240, 255]],
];

function conic(t) {
  for (let i = 1; i < STOPS.length; i++) {
    const [p0, c0] = STOPS[i - 1];
    const [p1, c1] = STOPS[i];
    if (t <= p1) {
      const k = (t - p0) / (p1 - p0);
      return c0.map((v, j) => v + (c1[j] - v) * k);
    }
  }
  return STOPS[0][1];
}

function sample(dx, dy, r, size) {
  const d = Math.hypot(dx, dy);
  if (d <= r) {
    const t = (Math.atan2(dy, dx) / (2 * Math.PI) + 1) % 1;
    const highlight = Math.max(0, 1 - Math.hypot(dx + r * 0.3, dy + r * 0.4) / (r * 0.75));
    const shade = 1 - 0.35 * Math.max(0, dy / r);
    return [conic(t).map((v) => Math.min(255, v * shade + 255 * highlight * 0.6)), 255];
  }
  const glow = Math.max(0, 1 - (d - r) / (size * 0.14));
  return [[0, 240, 255], 170 * glow * glow];
}

function draw(size) {
  const px = Buffer.alloc(size * size * 4);
  const ss = 4; // supersampling
  const r = size * 0.36;
  const c = size / 2;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const acc = [0, 0, 0, 0];
      for (let sy = 0; sy < ss; sy++) {
        for (let sx = 0; sx < ss; sx++) {
          const [rgb, a] = sample(x + (sx + 0.5) / ss - c, y + (sy + 0.5) / ss - c, r, size);
          acc[0] += rgb[0] * a;
          acc[1] += rgb[1] * a;
          acc[2] += rgb[2] * a;
          acc[3] += a;
        }
      }
      const i = (y * size + x) * 4;
      if (acc[3] > 0) {
        px[i] = acc[0] / acc[3];
        px[i + 1] = acc[1] / acc[3];
        px[i + 2] = acc[2] / acc[3];
      }
      px[i + 3] = Math.round(acc[3] / (ss * ss));
    }
  }
  return png(size, px);
}

function crc32(buf) {
  let crc = ~0;
  for (const byte of buf) {
    crc ^= byte;
    for (let k = 0; k < 8; k++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, 'ascii');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, crc]);
}

function png(size, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  const stride = size * 4;
  const rows = Buffer.alloc((stride + 1) * size); // filter byte 0 per row
  for (let y = 0; y < size; y++) rgba.copy(rows, y * (stride + 1) + 1, y * stride, (y + 1) * stride);
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return Buffer.concat([
    signature,
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(rows, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

const outDir = resolve(root, 'public/icons');
mkdirSync(outDir, { recursive: true });
for (const size of [16, 32, 48, 128]) writeFileSync(resolve(outDir, `${size}.png`), draw(size));
console.log(`wrote ${outDir}/{16,32,48,128}.png`);
