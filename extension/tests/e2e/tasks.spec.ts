/**
 * E2E on the fake profile: the fake brain proposes a browser task ("[task] ..."), the real consent window opens
 * (an extension popup window, clicked here like a user would), the service worker shares the tab's cookies and
 * localStorage, the stub worker plays a shopping task, the tracker shows its steps and question, and the result.
 * Also: declining, "Always allow" (the next task skips the window), and forgetting that grant in the options.
 */
import type { BrowserContext, Page } from '@playwright/test';

import { DEV_ID } from '../../playwright.config';
import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

const SHOP = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Mug shop</title></head>
<body style="margin:0;font:16px sans-serif"><h1 style="margin:40px">Mug shop</h1>
<p style="margin:40px">Signed in as alice.</p><button style="margin:40px">Blue Mug - $12</button>
<script>localStorage.setItem('cart', '[]');</script></body></html>`;

/** Type a question into the HUD (reopened each time, so its input has focus after the consent window took it). */
async function ask(page: Page, ui: Page, tabId: number, text: string): Promise<void> {
  await page.bringToFront();
  const { orb } = (await debugState(ui, tabId)).layout!;
  if ((await debugState(ui, tabId)).hudOpen) {
    await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
    await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(false);
  }
  await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);
  await page.keyboard.type(text);
  await page.keyboard.press('Enter');
  await expect.poll(async () => (await debugState(ui, tabId)).caption, { timeout: 10_000 })
    .toContain('I will ask before anything you cannot undo');
}

function consentWindow(context: BrowserContext): Promise<Page> {
  return context.waitForEvent('page', { predicate: (p) => p.url().includes('/consent.html'), timeout: 15_000 });
}

test('a consented browser task runs with the session, asks before ordering, and reports back', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId } = await openFixture(context, {
    path: 'shop', html: SHOP, headers: { 'Set-Cookie': 'session=alice-token; Path=/; HttpOnly; SameSite=Lax' },
  });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');

  // 1. Allow once.
  const opened = consentWindow(context);
  await ask(page, ui, tabId, '[task] add the blue mug to my cart');
  const consent = await opened;
  await expect(consent.getByTestId('consent-goal')).toHaveText('add the blue mug to my cart');
  await expect(consent.getByTestId('consent-tab-site')).toContainText('e2e.test');
  await expect(consent.getByTestId('consent-session')).toContainText('cookies (1)');
  await expect(consent.getByTestId('consent-allow')).toBeDisabled();            // armed after a moment
  expect((await debugState(ui, tabId)).task).toMatchObject({ status: 'consent', goal: 'add the blue mug to my cart' });
  await expect(consent.getByTestId('consent-allow')).toBeEnabled();
  await consent.getByTestId('consent-allow').click();

  await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 20_000 }).toBe('confirm');
  const running = (await debugState(ui, tabId)).task!;
  expect(running.steps[0]?.text).toBe('Opening http://e2e.test/shop with 1 cookie(s) from your session');
  expect(running.confirm?.question).toBe('Place the order for $12.00?');
  expect(running.cookies).toBe(1);
  await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.taskAnswer', tabId: id, approved: true }), tabId);
  await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 15_000 }).toBe('done');
  expect((await debugState(ui, tabId)).task).toMatchObject({
    summary: 'Added the item to your cart and placed the order.', finalUrl: 'http://e2e.test/shop',
  });

  // 2. Don't allow.
  const second = consentWindow(context);
  await ask(page, ui, tabId, '[task] buy ten more');
  await (await second).getByTestId('consent-deny').click();
  await expect.poll(async () => (await debugState(ui, tabId)).task?.summary, { timeout: 15_000 })
    .toBe('You declined the task, so nothing was done.');

  // 3. Always allow -> 4. the next task on this site starts without the window.
  const third = consentWindow(context);
  await ask(page, ui, tabId, '[task] buy a red mug');
  const always = await third;
  await expect(always.getByTestId('consent-always')).toBeEnabled();
  await always.getByTestId('consent-always').click();
  await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 20_000 }).toBe('confirm');
  await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.taskAnswer', tabId: id, approved: false }), tabId);
  await expect.poll(async () => (await debugState(ui, tabId)).task?.summary, { timeout: 15_000 })
    .toBe('Stopped before placing the order: you said no.');

  let windows = 0;
  context.on('page', (p) => { if (p.url().includes('/consent.html')) windows += 1; });
  await ask(page, ui, tabId, '[task] buy a green mug');
  await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 20_000 }).toBe('confirm');
  expect((await debugState(ui, tabId)).task?.auto).toBe(true);
  expect(windows).toBe(0);
  await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.taskAnswer', tabId: id, approved: true }), tabId);
  await expect.poll(async () => (await debugState(ui, tabId)).task?.status, { timeout: 15_000 }).toBe('done');

  // The grant is listed in the options and can be forgotten.
  const options = await context.newPage();
  await options.goto(`chrome-extension://${DEV_ID}/options.html`);
  await expect(options.getByTestId('grants')).toContainText('e2e.test');
  await options.getByRole('button', { name: 'Forget', exact: true }).click();
  await expect(options.getByTestId('grants-empty')).toBeVisible();
});

test('a task from a page that is not a website is refused without asking', async ({ context, sw }) => {
  await pair(context, sw);
  const ui = await popup(context);
  // The popup's debug box has no tab: the backend never even proposes it there.
  await ui.getByTestId('turn-input').fill('[task] add the blue mug');
  await ui.getByTestId('turn-input').press('Enter');
  await expect(ui.getByTestId('turn-done')).toHaveAttribute('data-reason', 'end');
  expect(context.pages().some((p) => p.url().includes('/consent.html'))).toBe(false);
});
