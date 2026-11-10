/**
 * Opt-in: captures the pages of `clicky eval brain` (backend/evals/brain) through the real pipeline - DOM set-of-marks
 * plus the masked, resized screenshot - at both page budgets, so the eval feeds every model exactly what it would get.
 *
 *   $env:CC_EVAL_OUT = "..\backend\.clicky\eval\pages"; npx playwright test eval-capture
 *
 * Writes <out>/<full|compact>/<case id>.json (PageContext). No backend needed.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { PageContextReply } from '../../src/lib/messages';
import { expect, openFixture, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const OUT = process.env.CC_EVAL_OUT;
const EVALS = fileURLToPath(new URL('../../../backend/evals/brain/', import.meta.url));

interface EvalCase { id: string }

test.describe('brain eval pages', () => {
  test.skip(!OUT, 'set CC_EVAL_OUT to the directory the eval reads (see the header)');
  test.setTimeout(5 * 60_000);

  test('capture every case at both budgets', async ({ context }) => {
    const { cases } = JSON.parse(readFileSync(join(EVALS, 'cases.json'), 'utf8')) as { cases: EvalCase[] };
    const ui = await popup(context);
    for (const budget of ['full', 'compact'] as const) mkdirSync(join(OUT!, budget), { recursive: true });
    for (const { id } of cases) {
      const html = readFileSync(join(EVALS, 'pages', `${id}.html`), 'utf8');
      const { page, tabId } = await openFixture(context, { path: `eval/${id}`, html });
      await page.bringToFront();                                   // captureVisibleTab sees the active tab only
      await page.waitForTimeout(300);
      for (const budget of ['full', 'compact'] as const) {
        const reply: PageContextReply = await ui.evaluate(
          ({ tab, name }) => chrome.runtime.sendMessage({ type: 'debug.pageContext', tabId: tab, budget: name }),
          { tab: tabId, name: budget });
        if (!reply.ok) throw new Error(`${id}/${budget}: ${reply.error}`);
        expect(reply.page.screenshot, `${id}/${budget} screenshot`).toBeTruthy();
        writeFileSync(join(OUT!, budget, `${id}.json`), JSON.stringify(reply.page));
      }
      await page.close();
    }
  });
});
