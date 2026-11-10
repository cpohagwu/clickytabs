/**
 * E2E: the brain's directives reach the overlay (the fake brain answers "[point]" with a <point/> tag split across
 * deltas, which the backend parses out), and follow-up questions continue the conversation until "New conversation".
 */
import { debugState, expect, openFixture, pair, popup, test } from './fixtures';

const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Brain fixture</title></head>
<body style="margin:0;font:16px sans-serif">
  <h1 style="margin:40px">Popover API</h1>
  <button style="position:absolute;left:600px;top:300px;padding:12px 24px">Show popover</button>
</body></html>`;

test('a HUD question points at the element the answer names', async ({ context, sw }) => {
  await pair(context, sw);
  const { page, tabId } = await openFixture(context, { path: 'm4', html: PAGE });
  const ui = await popup(context);
  await page.bringToFront();
  await expect.poll(async () => (await debugState(ui, tabId)).conn).toBe('connected');
  const { orb } = (await debugState(ui, tabId)).layout!;
  await page.mouse.click(orb.x + orb.width / 2, orb.y + orb.height / 2);
  await expect.poll(async () => (await debugState(ui, tabId)).hudOpen).toBe(true);
  await page.keyboard.type('[point] where is the button?');
  await page.keyboard.press('Enter');
  await expect.poll(async () => (await debugState(ui, tabId)).caption)
    .toBe('Right over here. The Show popover is what you want.');          // the tag is never shown or spoken
  await expect.poll(async () => (await debugState(ui, tabId)).annotations, { timeout: 10_000 }).toBeGreaterThanOrEqual(1);
  expect((await debugState(ui, tabId)).lastDirective).toEqual({ kind: 'point', target: 'Show popover' });
});

test('follow-up questions continue the conversation until "New conversation"', async ({ context, sw }) => {
  await pair(context, sw);
  const ui = await popup(context);
  await expect(ui.getByTestId('conn-state')).toHaveAttribute('data-state', 'connected');
  const ask = async (text: string): Promise<string> => {
    await ui.getByTestId('turn-input').fill(text);
    await ui.getByTestId('turn-input').press('Enter');
    await expect(ui.getByTestId('turn-done')).toHaveAttribute('data-reason', 'end');
    return (await ui.getByTestId('turn-output').textContent()) ?? '';
  };
  expect(await ask('What is a popover?')).toContain('You said: What is a popover?');
  expect(await ask('[history] and then?')).toBe('I remember 1 earlier turn.');
  const stored = await sw.evaluate(async () => (await chrome.storage.session.get('conversation')).conversation);
  expect(stored).toMatch(/^[A-Za-z0-9_-]{8,64}$/);

  await ui.getByTestId('conversation-reset').click();
  await expect(ui.getByTestId('conversation-note')).toHaveText('Started a new conversation.');
  await expect.poll(async () => sw.evaluate(async () => (await chrome.storage.session.get('conversation')).conversation))
    .not.toBe(stored);
  expect(await ask('[history] fresh start?')).toBe('I remember 0 earlier turns.');
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
declare const chrome: any;
