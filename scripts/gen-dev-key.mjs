#!/usr/bin/env node
// One-time helper: create the dev public key that pins ClickyTabs's extension ID.
//
// Chrome derives an unpacked extension's ID from the `key` (base64 DER SubjectPublicKeyInfo) in manifest.json:
// ID = first 128 bits of SHA-256(DER), hex digits 0-f mapped to letters a-p. The private key is only needed to sign
// CRX packages, so it is never written to disk. Re-run with --force to rotate (this changes the extension ID).
import { createHash, generateKeyPairSync } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const keyFile = resolve(root, 'extension/keys/dev-public-key.b64');
const idFile = resolve(root, 'extension/keys/dev-extension-id.txt');

export function extensionIdFromDer(der) {
  const hex = createHash('sha256').update(der).digest('hex').slice(0, 32);
  return [...hex].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
}

if (existsSync(keyFile) && !process.argv.includes('--force')) {
  const der = Buffer.from(readFileSync(keyFile, 'utf8').trim(), 'base64');
  console.log(extensionIdFromDer(der));
  process.exit(0);
}

const { publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const der = publicKey.export({ type: 'spki', format: 'der' });
const id = extensionIdFromDer(der);

mkdirSync(dirname(keyFile), { recursive: true });
writeFileSync(keyFile, der.toString('base64') + '\n');
writeFileSync(idFile, id + '\n');
console.log(id);
