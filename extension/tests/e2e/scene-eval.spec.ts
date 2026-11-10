/**
 * Opt-in acceptance: the 10-prompt scene suite (backend/evals/scenes/cases.json) through the real pipeline.
 *
 * The brain is the fake one (so every case asks for the same scene with the same brief, reproducibly); the codegen
 * model, the sandbox and the automatic repair turn are real. Start a backend whose brain is fake and whose codegen is
 * the model to measure, with a throw-away home and token:
 *
 *   # clicky.toml in that home:  profile = "cloud"  +  [profiles.cloud.capabilities.brain] provider = "fake"
 *   $env:CLICKY_HOME = "$env:TEMP\clicky-scene-eval"; $env:CLICKY_AUTH_TOKEN = 'scene-eval-token-0123456789'
 *   $env:CLICKY_ALLOWED_EXTENSION_IDS = 'fhgliaahcafninaodeelekpdemjkifmf'
 *   uv run --directory ../backend clicky serve --port 18766 --profile cloud
 *
 *   $env:CC_SCENE_EVAL_BACKEND = '18766:scene-eval-token-0123456789'; $env:CC_SCENE_EVAL_OUT = 'C:\path\out'
 *   npx playwright test scene-eval
 *
 * A case passes when its scene renders (on the first try or after the one repair). Results, with a screenshot of
 * every stage, land in CC_SCENE_EVAL_OUT (default test-results/scene-eval).
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

interface Case {
  id: string;
  kind: string;
  title: string;
  brief: string;
}

const CASES: Case[] = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../backend/evals/scenes/cases.json', import.meta.url)), 'utf8'));
const [port, token] = (process.env.CC_SCENE_EVAL_BACKEND ?? '').split(':');
const OUT = process.env.CC_SCENE_EVAL_OUT ?? fileURLToPath(new URL('../../test-results/scene-eval', import.meta.url));
const ONLY = (process.env.CC_SCENE_EVAL_CASES ?? '').split(',').filter(Boolean);
const CASE_TIMEOUT_MS = Number(process.env.CC_SCENE_EVAL_CASE_MS ?? 420_000);

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Scene eval</title></head>
<body style="margin:0;font:16px sans-serif;background:#f4f5f8"><h1 style="margin:40px">Scene eval</h1></body></html>`;

test.describe('scene eval', () => {
  test.skip(!port, 'set CC_SCENE_EVAL_BACKEND (see the header)');
  test.setTimeout(CASES.length * CASE_TIMEOUT_MS);

  test('the 10-prompt suite renders', async ({ context, sw }) => {
    mkdirSync(OUT, { recursive: true });
    await pair(context, sw, { port: Number(port), token: token! });
    const { page, tabId } = await openFixture(context, { path: 'scene-eval', html: PAGE });
    const ui = await popup(context);
    await page.bringToFront();
    await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
    const { orb } = (await debugState(ui, tabId)).layout!;
    await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
    await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);

    const results = [];
    for (const item of CASES.filter((c) => !ONLY.length || ONLY.includes(c.id))) {
      const started = Date.now();
      await page.keyboard.type(`[scene:${item.kind}|${item.title}] ${item.brief}`);
      await page.keyboard.press('Enter');
      await expect.poll(async () => (await debugState(ui, tabId)).scene?.title, { timeout: 30_000 }).toBe(item.title);
      await expect.poll(async () => (await debugState(ui, tabId)).scene?.status,
        { timeout: CASE_TIMEOUT_MS, intervals: [1_000] }).toMatch(/^(shown|failed)$/);
      const scene = (await debugState(ui, tabId)).scene!;
      const totalMs = Date.now() - started;
      await page.waitForTimeout(scene.status === 'shown' ? 1_500 : 200);   // let animations get going
      await page.screenshot({ path: join(OUT, `${item.id}.png`) });
      const result = { id: item.id, kind: item.kind, rendered: scene.status === 'shown', attempts: scene.attempt,
        error: scene.error, codegenMs: scene.codegenMs, totalMs, stats: scene.stats };
      results.push(result);
      console.log(`${item.id.padEnd(14)} ${result.rendered ? 'RENDERED' : 'FAILED  '} attempts=${result.attempts} `
        + `codegen=${Math.round((result.codegenMs ?? 0) / 100) / 10}s total=${Math.round(totalMs / 100) / 10}s`
        + (result.error ? ` error=${result.error.slice(0, 160)}` : ''));
      // Close the stage, so the next case is a new scene rather than an edit of this one.
      await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.sceneClose', tabId: id }), tabId);
      await expect.poll(async () => (await debugState(ui, tabId)).scene?.open).toBe(false);
    }

    const rendered = results.filter((r) => r.rendered).length;
    const firstTry = results.filter((r) => r.rendered && r.attempts === 1).length;
    const sorted = results.map((r) => r.totalMs).sort((a, b) => a - b);
    const summary = { rendered, firstTry, cases: results.length, p50TotalMs: sorted[Math.floor(sorted.length / 2)],
      results };
    writeFileSync(join(OUT, 'scene-eval.json'), JSON.stringify(summary, null, 2));
    console.log(`rendered ${rendered}/${results.length} (${firstTry} on the first try)`);
    expect(results.length).toBeGreaterThan(0);
  });
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;
