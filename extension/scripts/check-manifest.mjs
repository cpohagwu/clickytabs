#!/usr/bin/env node
// Asserts the security-relevant SUBSET of the production manifest (plan §6.2). Run after `npm run build`.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(readFileSync(resolve(root, '.output/chrome-mv3/manifest.json'), 'utf8'));
const expectedKey = readFileSync(resolve(root, 'keys/dev-public-key.b64'), 'utf8').trim();
const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

check(manifest.manifest_version === 3, 'manifest_version must be 3');
check(manifest.key === expectedKey, 'key must be the pinned dev public key');
for (const permission of ['activeTab', 'scripting', 'cookies', 'storage', 'offscreen', 'alarms']) {
  check(manifest.permissions?.includes(permission), `permissions must include ${permission}`);
}
check(!manifest.permissions?.includes('tabs'), 'the production build must not request "tabs"');
check(JSON.stringify(manifest.host_permissions) === '["<all_urls>"]', 'host_permissions must be ["<all_urls>"]');
check(manifest.background?.service_worker === 'background.js', 'background.service_worker must be background.js');
check(manifest.background?.type !== 'module', 'background must be the default classic bundle (no type: module)');
check(JSON.stringify(manifest.sandbox?.pages) === '["scene.html"]', 'sandbox.pages must be ["scene.html"]');
const csp = manifest.content_security_policy?.sandbox ?? '';
for (const part of ['sandbox allow-scripts', "connect-src 'none'", "script-src 'self' 'unsafe-eval'"]) {
  check(csp.includes(part), `the sandbox CSP must contain ${part}`);
}
check(!csp.includes('allow-same-origin'), 'the sandbox CSP must not contain allow-same-origin');
const war = manifest.web_accessible_resources ?? [];
const scene = war.find((entry) => entry.resources?.includes('scene.html'));
check(scene?.use_dynamic_url === true, 'scene.html must be web-accessible with use_dynamic_url');
check(!war.some((entry) => entry.resources?.includes('scene-harness.js')), 'scene-harness.js must not be web-accessible');
const scripts = manifest.content_scripts ?? [];
const hotkeys = scripts.find((s) => s.js?.includes('content-scripts/hotkeys.js'));
check(hotkeys?.all_frames === true, 'the hotkeys content script must run in all frames');
check(hotkeys?.match_about_blank === true, 'the hotkeys content script must match about:blank');
check(hotkeys?.match_origin_as_fallback === true, 'the hotkeys content script must use match_origin_as_fallback');
check(hotkeys?.run_at === 'document_start', 'the hotkeys content script must run at document_start');
const companion = scripts.find((s) => s.js?.includes('content-scripts/content.js'));
check(companion && !companion.all_frames, 'the companion content script must be top-frame only');
check(manifest.options_ui?.open_in_tab === true, 'options must open in a tab');
check(manifest.commands?.['toggle-listen'] && manifest.commands?.['toggle-companion'], 'commands are missing');
check(manifest.minimum_chrome_version === '120', 'minimum_chrome_version must be 120');

if (failures.length) {
  console.error(`manifest check failed:\n - ${failures.join('\n - ')}`);
  process.exit(1);
}
console.log(`manifest OK (${manifest.name} ${manifest.version})`);
