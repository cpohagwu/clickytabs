import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { defineConfig } from '@playwright/test';

export const E2E_PORT = 18765;
export const E2E_TOKEN = 'e2e-token-0123456789abcdef';
export const DEV_ID = 'fhgliaahcafninaodeelekpdemjkifmf';

export default defineConfig({
  testDir: 'tests/e2e',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  reporter: [['list']],
  use: { trace: 'retain-on-failure' },
  webServer: {
    // The real backend, with deterministic fake providers and a throw-away CLICKY_HOME.
    command: `uv run --directory ../backend clicky serve --port ${E2E_PORT}`,
    url: `http://127.0.0.1:${E2E_PORT}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      CLICKY_HOME: mkdtempSync(join(tmpdir(), 'clicky-e2e-')),
      CLICKY_PROFILE: 'fake',
      CLICKY_AUTH_TOKEN: E2E_TOKEN,
      CLICKY_ALLOWED_EXTENSION_IDS: DEV_ID,
    },
  },
});
