#!/usr/bin/env node
/**
 * Chrome Web Store package: `wxt zip --mode store` (name ClickyTabs, no dev key; .output/chrome-mv3-store),
 * then checks what the store would reject, and names the zip clickytabs-<version>-chrome.zip.
 *
 *   npm run zip:store
 */
import { execSync } from 'node:child_process';
import { existsSync, readFileSync, renameSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
execSync('npx wxt zip --mode store', { cwd: root, stdio: 'inherit' });

const manifest = JSON.parse(readFileSync(resolve(root, '.output/chrome-mv3-store/manifest.json'), 'utf8'));
const problems = [];
if (/chrome/i.test(manifest.name)) problems.push(`the name "${manifest.name}" contains "Chrome"`);
if ('key' in manifest) problems.push('the manifest has a "key" (the store rejects it)');
if (manifest.manifest_version !== 3) problems.push('not Manifest V3');
for (const field of ['description', 'version', 'icons']) {
  if (!manifest[field]) problems.push(`missing "${field}"`);
}
if (problems.length) {
  console.error(`store package check failed:\n- ${problems.join('\n- ')}`);
  process.exit(1);
}

const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const built = resolve(root, `.output/${pkg.name}-${pkg.version}-chrome-store.zip`);
const target = resolve(root, `.output/${manifest.name.toLowerCase()}-${manifest.version}-chrome.zip`);
if (!existsSync(built)) {
  console.error(`expected ${built}`);
  process.exit(1);
}
renameSync(built, target);
console.log(`store package OK: ${target} (${manifest.name} ${manifest.version}, no key)`);
