import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig } from 'wxt';

// Scene code may eval but cannot fetch/XHR/WebSocket. Three/canvas code runs in a blob: Worker (no DOM, so it can't
// navigate the frame); svg/html scenes are sanitized markup. The nonce handshake stops other pages driving the frame.
const SANDBOX_CSP =
  "sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-eval'; worker-src blob:; " +
  "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'none'";

/** The name in the manifest (and, via src/lib/brand.ts, in the UI). "Chrome" can't be part of a store item's name. */
const APP_NAME = 'ClickyTabs';
/**
 * The Chrome Web Store build: `npm run zip:store` (= `wxt zip --mode store`, output in .output/chrome-mv3-store).
 * The same extension without the dev `key`: the store assigns the ID (and rejects a manifest with a key). Add that ID
 * to the backend's allowed_extension_ids.
 */
export const STORE_MODE = 'store';

export default defineConfig({
  srcDir: 'src',
  modules: ['@wxt-dev/module-svelte'],
  // Explicit imports only: keeps every content-script dependency visible to tests/unit/guard.test.ts.
  imports: false,
  // WXT's default dev reload chord (Alt+R) is commonly owned by the AMD Software overlay on Windows laptops.
  dev: { reloadCommand: 'Alt+Shift+R' },
  webExt: { chromiumProfile: resolve('.wxt/chrome-profile'), keepProfileChanges: true }, // keeps pairing + mic grant ⟨verify M0⟩
  manifest: ({ mode }) => ({
    name: APP_NAME,
    description: 'A voice-activated AI companion that lives in your tab: it listens, talks, points, draws and acts.',
    // pins one extension ID for dev, prod and e2e (not in the store build: the store assigns the ID)
    ...(mode === STORE_MODE ? {} : { key: readFileSync('keys/dev-public-key.b64', 'utf8').trim() }),
    minimum_chrome_version: '120',
    permissions: ['activeTab', 'scripting', 'cookies', 'storage', 'offscreen', 'alarms'],
    host_permissions: ['<all_urls>'],
    action: { default_title: APP_NAME },
    commands: {
      'toggle-listen': {
        suggested_key: { default: 'Ctrl+Shift+Space', mac: 'Command+Shift+Space' },
        description: `Start / stop talking to ${APP_NAME}`,
      },
      'toggle-companion': { suggested_key: { default: 'Alt+Shift+C' }, description: 'Show / hide the companion' },
    },
    sandbox: { pages: ['scene.html'] }, // static page from public/, classic script only
    content_security_policy: { sandbox: SANDBOX_CSP },
    web_accessible_resources: [
      { resources: ['scene.html'], matches: ['<all_urls>'], use_dynamic_url: true },
    ],
  }),
});
