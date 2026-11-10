/**
 * Opt-in acceptance on live sites (network required, never part of the default run):
 *
 *   $env:CC_E2E_REAL_SITES = '1'; npx playwright test real-sites
 *
 * For each site: the companion mounts in the top layer and the orb is hit-testable; the page-context snapshot
 * takes under 30 ms and yields marks plus a fresh screenshot; the overlay tour draws on the real page.
 * Screenshots (what the user sees, and what the brain gets) land in test-results/ for a visual check. Google Docs
 * needs a document URL you can open without signing in: set CC_E2E_GDOC_URL to include it.
 */
import { writeFileSync } from 'node:fs';

import type { PageContextReply } from '../../src/lib/messages';
import { debugState, expect, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const SITES: Record<string, string> = {
  docs: 'https://developer.mozilla.org/en-US/docs/Web/API/Popover_API',
  shop: 'https://books.toscrape.com/',
  youtube: 'https://www.youtube.com/',
  github: 'https://github.com/sveltejs/svelte', // strict CSP + Trusted Types
  ...(process.env.CC_E2E_GDOC_URL ? { gdocs: process.env.CC_E2E_GDOC_URL } : {}),
};

test.describe('real sites', () => {
  test.skip(!process.env.CC_E2E_REAL_SITES, 'set CC_E2E_REAL_SITES=1 to run against live sites');

  for (const [name, url] of Object.entries(SITES)) {
    test(name, async ({ context }, testInfo) => {
      const page = await context.newPage();
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45_000 });
      const sw = context.serviceWorkers()[0]!;
      const tabId: number = await sw.evaluate(async () => {
        const [tab] = await chrome.tabs.query({ url: ['http://*/*', 'https://*/*'] });
        return tab?.id;
      });
      const ui = await popup(context);
      await page.bringToFront();
      await expect.poll(async () => (await debugState(ui, tabId)).layout?.popoverOpen, { timeout: 30_000 }).toBe(true);
      const { layout } = await debugState(ui, tabId);
      testInfo.annotations.push({ type: 'hit-test', description: layout!.hitAtOrb });
      expect(layout!.hitAtOrb).toBe('orb');

      // Measure as a user would ask: on a page that finished loading, not mid-load.
      await page.waitForLoadState('load', { timeout: 15_000 }).catch(() => undefined);
      await page.waitForTimeout(1_500);
      const reply: PageContextReply = await ui.evaluate(
        (id) => chrome.runtime.sendMessage({ type: 'debug.pageContext', tabId: id }), tabId);
      if (!reply.ok) throw new Error(reply.error);
      const { page: ctx } = reply;
      testInfo.annotations.push({
        type: 'page context',
        description: `snapshot ${reply.snapshotMs} ms ${JSON.stringify(reply.phases)}, total ${reply.totalMs} ms, `
          + `${ctx.elements?.length ?? 0} marks, ${ctx.text_excerpt?.length ?? 0} text chars, `
          + `capture ${JSON.stringify(reply.capture)}`,
      });
      writeFileSync(testInfo.outputPath(`${name}-brain.jpg`), Buffer.from(ctx.screenshot!.data_b64, 'base64'));
      expect(reply.snapshotMs).toBeLessThan(30);
      expect(ctx.elements?.length ?? 0).toBeGreaterThan(5);

      expect(await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'demo.run', tabId: id }), tabId)).toEqual({ ok: true });
      await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 20_000 }).toBeGreaterThanOrEqual(1);
      await page.waitForTimeout(6_000);
      await page.screenshot({ path: testInfo.outputPath(`${name}.png`) });
    });
  }
});
