/**
 * Walking-skeleton E2E: built extension (bundled Chromium; branded Chrome >= 137 ignores --load-extension) + the
 * real backend on the fake profile. Exercises pairing -> SW connection, the in-page companion, the popup's text turn
 * (stream + cancel) and the scene sandbox handshake.
 */
import { E2E_PORT } from '../../playwright.config';
import { connState, expect, openFixture, pair, popup, test } from './fixtures';

// `chrome` exists only inside the extension contexts that the evaluate() callbacks run in.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;

test('pairs, connects and locks storage.local away from content scripts', async ({ context, sw }) => {
  expect(await connState(sw)).toMatch(/unpaired|connecting/);
  await pair(context, sw);
  const { page, tabId } = await openFixture(context);
  await expect(page.locator('clickytabs-ui')).toBeAttached();

  const ui = await popup(context);
  const state = await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.state', tabId: id }), tabId);
  expect(state).toMatchObject({ mounted: true, visible: true, conn: 'connected', storageLocked: true });
  await expect(ui.getByTestId('conn-state')).toHaveAttribute('data-state', 'connected');
});

test('a re-injected content script replaces the companion instead of adding a second one', async ({ context, sw }) => {
  // On install the worker injects into open tabs, which can race the manifest's own injection. Two copies started
  // back to back: the first is invalidated while it awaits its settings and must not mount (a second companion
  // painted its own capture sentinel and made every screenshot look stale).
  const { page, tabId } = await openFixture(context);
  await expect(page.locator('clickytabs-ui')).toHaveCount(1);
  await sw.evaluate(async (id) => {
    const inject = () => chrome.scripting.executeScript({ target: { tabId: id }, files: ['/content-scripts/content.js'] });
    await Promise.all([inject(), inject()]);
  }, tabId);
  await page.waitForTimeout(500); // both copies past their awaits
  await expect(page.locator('clickytabs-ui')).toHaveCount(1);
  const ui = await popup(context);
  const state = await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.state', tabId: id }), tabId);
  expect(state).toMatchObject({ mounted: true, visible: true });
});

test('popup text turn streams fake deltas and can be cancelled', async ({ context, sw }) => {
  await pair(context, sw);
  const ui = await popup(context);
  await expect(ui.getByTestId('conn-state')).toHaveAttribute('data-state', 'connected');

  // Shift+Enter is a new line; Enter sends.
  await ui.getByTestId('turn-input').fill('hello');
  await ui.getByTestId('turn-input').press('Shift+Enter');
  await ui.getByTestId('turn-input').pressSequentially('e2e');
  await expect(ui.getByTestId('turn-input')).toHaveValue('hello\ne2e');
  await ui.getByTestId('turn-input').fill('hello e2e');
  await ui.getByTestId('turn-input').press('Enter');
  await expect(ui.getByTestId('turn-done')).toHaveAttribute('data-reason', 'end');
  await expect(ui.getByTestId('turn-output')).toContainText('You said: hello e2e.');

  await ui.getByTestId('turn-input').fill('[slow] count');
  await ui.getByTestId('turn-send').click();
  await expect(ui.getByTestId('turn-output')).toContainText('Counting slowly:');
  await ui.getByTestId('turn-cancel').click();
  await expect(ui.getByTestId('turn-done')).toHaveAttribute('data-reason', 'cancelled');
  expect((await ui.getByTestId('turn-output').textContent())?.includes(' 60')).toBe(false);
});

test('scene sandbox answers through the nonce + MessagePort handshake', async ({ context, sw }) => {
  await pair(context, sw);
  const { tabId } = await openFixture(context);
  const ui = await popup(context);
  const reply = await ui.evaluate((id) => chrome.runtime.sendMessage({ type: 'debug.scenePing', tabId: id }), tabId);
  expect(reply).toMatchObject({ ok: true, pong: { type: 'pong' } });
});

test('offscreen document answers and a wrong token ends in the terminal unauthorized state', async ({ context, sw }) => {
  const ui = await popup(context);
  const pong = await ui.evaluate(() => chrome.runtime.sendMessage({ type: 'debug.offscreenPing' }));
  expect(pong).toMatchObject({ ok: true, type: 'pong' });

  await sw.evaluate(
    (url) => chrome.storage.local.set({ backendUrl: url, token: 'wrong-token-zzzzzzzzzz' }),
    `ws://127.0.0.1:${E2E_PORT}/v1/ws`,
  );
  await expect.poll(() => connState(sw)).toBe('unauthorized');
  await expect(ui.getByTestId('repair')).toBeVisible();
});
