// @vitest-environment happy-dom
import { get, writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type CompanionState, displayOrb, INITIAL_COMPANION } from '@/lib/companion';
import { CompanionController, type CompanionView } from '@/lib/companion-controller';
import type { SwRequest, TabTurnEvent } from '@/lib/messages';
import { planDemo } from '@/lib/overlay/demo';
import { type DirectiveHost, resolveRef } from '@/lib/overlay/directives';
import type { OverlayRenderer } from '@/lib/overlay/renderer';
import { ElementRegistry } from '@/lib/overlay/targets';

const VIEWPORT = { width: 1024, height: 768 };

/** happy-dom has no layout: give elements the rect the planner and renderer will read. */
function place(element: Element, x: number, y: number, width: number, height: number): void {
  element.getBoundingClientRect = () => ({ x, y, width, height, left: x, top: y, right: x + width, bottom: y + height,
    toJSON: () => ({}) }) as DOMRect;
}

function buildPage(): { heading: Element; button: Element; paragraph: Element; image: Element; hidden: Element } {
  document.body.innerHTML = `
    <h1 id="offscreen">Far below</h1>
    <h1 id="title">Welcome to the shop</h1>
    <button id="hidden" style="display:none">Ghost</button>
    <button id="buy">Add to cart</button>
    <p id="short">Too short.</p>
    <p id="copy">This paragraph is long enough to be underlined by the companion demo tour.</p>
    <img id="photo" alt="product" />`;
  const byId = (id: string) => document.getElementById(id)!;
  place(byId('offscreen'), 0, 5000, 300, 40);
  place(byId('title'), 40, 40, 400, 48);
  place(byId('hidden'), 0, 0, 80, 30);
  place(byId('buy'), 60, 120, 120, 36);
  place(byId('short'), 60, 180, 300, 20);
  place(byId('copy'), 60, 220, 600, 40);
  place(byId('photo'), 700, 120, 200, 160);
  return { heading: byId('title'), button: byId('buy'), paragraph: byId('copy'), image: byId('photo'),
    hidden: byId('hidden') };
}

function fakeRenderer() {
  return {
    pointTo: vi.fn(async () => undefined),
    mark: vi.fn(() => 1),
    arrow: vi.fn(() => 2),
    path: vi.fn(() => 3),
    clear: vi.fn(),
    count: 0,
    animating: false,
  };
}

function setup(reply: (message: SwRequest) => unknown = () => ({ ok: true })) {
  const store = writable<CompanionState>({ ...INITIAL_COMPANION, conn: 'connected' });
  const renderer = fakeRenderer();
  const request = vi.fn(async (message: SwRequest) => reply(message)) as unknown as
    (<T>(message: SwRequest) => Promise<T | undefined>) & { mock: { calls: [SwRequest][] } };
  const controller = new CompanionController({
    store,
    request,
    viewport: () => VIEWPORT,
    reducedMotion: () => true, // no synthetic level interval in tests
  });
  const view: CompanionView = {
    renderer: renderer as unknown as OverlayRenderer,
    origin: () => ({ x: 980, y: 720 }),
    raise: vi.fn(),
  };
  controller.attachView(view);
  return { store, renderer, request, controller, view };
}

describe('demo planner', () => {
  beforeEach(() => buildPage());

  it('tours visible elements only, by registry id, with offsets inside each line', () => {
    const page = buildPage();
    const registry = new ElementRegistry();
    const steps = planDemo(document, registry, VIEWPORT);
    const directives = steps.flatMap((step) => step.directives.map((d) => d.directive));

    const ids = (el: Element) => [...Array(registry.size).keys()].map((i) => `e${i + 1}`).find((id) => registry.get(id) === el);
    expect(directives).toContainEqual({ kind: 'point', to: ids(page.heading), label: 'Main heading' });
    expect(directives).toContainEqual({ kind: 'mark', to: ids(page.button), style: 'box' });
    expect(directives).toContainEqual({ kind: 'mark', to: ids(page.paragraph), style: 'underline' });
    expect(directives).toContainEqual({ kind: 'mark', to: ids(page.image), style: 'glow', label: 'Image' });
    expect(directives).toContainEqual({ kind: 'arrow', source: ids(page.button), target: ids(page.image) }); // farthest pair
    expect(directives.some((d) => d.kind === 'path')).toBe(true);
    expect(directives.at(-1)).toEqual({ kind: 'clear' });
    expect(ids(page.hidden)).toBeUndefined();
    for (const step of steps) {
      expect(step.durationMs).toBeGreaterThanOrEqual(1600);
      for (const { at_char } of step.directives) expect(at_char).toBeLessThanOrEqual(step.text.length);
    }
  });

  it('underlines prose, not a row of links that contains the action', () => {
    document.body.innerHTML = `
      <p id="links"><a id="cta" href="#a">Buy the thing</a> · <a href="#b">modal</a> · <a href="#c">popover</a> · x</p>
      <p id="prose">This paragraph is long enough for the demo tour to underline it as the important part.</p>`;
    place(document.getElementById('links')!, 20, 20, 600, 30);
    place(document.getElementById('cta')!, 20, 20, 120, 30);
    place(document.getElementById('prose')!, 20, 80, 900, 30);
    const registry = new ElementRegistry();
    const underline = planDemo(document, registry, VIEWPORT)
      .flatMap((s) => s.directives.map((d) => d.directive))
      .find((d) => d.kind === 'mark' && d.style === 'underline');
    expect(underline?.kind === 'mark' ? registry.get(underline.to)?.id : null).toBe('prose');
  });

  it('still plays an intro, a sketch and a clear on an empty page', () => {
    document.body.innerHTML = '';
    const steps = planDemo(document, new ElementRegistry(), VIEWPORT);
    expect(steps.map((s) => s.directives.map((d) => d.directive.kind))).toEqual([[], ['path'], ['clear']]);
  });
});

describe('directive references', () => {
  it('resolves registry ids and "x,y" points, and skips anything else', () => {
    const { heading } = buildPage();
    const registry = new ElementRegistry();
    const id = registry.register(heading);
    const host = { registry, renderer: {} as OverlayRenderer, origin: () => ({ x: 0, y: 0 }) } satisfies DirectiveHost;
    expect(resolveRef(id, host)).toEqual({ kind: 'element', element: heading });
    expect(resolveRef(' 120, 40.5 ', host)).toEqual({ kind: 'page-point', x: 120 + scrollX, y: 40.5 + scrollY });
    expect(resolveRef('e999', host)).toBeNull();
    expect(resolveRef('#title', host)).toBeNull();
    heading.remove();
    expect(resolveRef(id, host)).toBeNull(); // detached elements are gone
  });
});

describe('companion controller', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('plays the demo through the directive path and ends idle', async () => {
    vi.useFakeTimers();
    buildPage();
    const { store, renderer, controller, view } = setup();
    const done = controller.runDemo();
    expect(get(store)).toMatchObject({ busy: true, activity: 'speaking', userText: 'Show me what you can do.' });
    expect(view.raise).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(60_000);
    await done;

    expect(renderer.pointTo).toHaveBeenCalledTimes(2);
    expect(renderer.mark.mock.calls.map((call: unknown[]) => call[1])).toEqual(['circle', 'box', 'underline', 'glow']);
    expect(renderer.arrow).toHaveBeenCalledTimes(1);
    expect(renderer.path).toHaveBeenCalledTimes(1);
    expect(renderer.clear).toHaveBeenCalled();
    const state = get(store);
    expect(state).toMatchObject({ busy: false, activity: 'idle' });
    expect(state.caption).toMatch(/^Hi, I'm ClickyTabs\..*Cleaning up now\.$/);
  });

  it('stopping the demo cancels the remaining steps', async () => {
    vi.useFakeTimers();
    buildPage();
    const { store, renderer, controller } = setup();
    void controller.runDemo();
    await vi.advanceTimersByTimeAsync(500);
    await controller.stop();
    const calls = renderer.mark.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(renderer.mark.mock.calls.length).toBe(calls);
    expect(get(store)).toMatchObject({ busy: false, activity: 'idle' });
  });

  it('runs a HUD text turn: thinking -> captions -> directives -> idle', async () => {
    const { store, renderer, request, controller } = setup((message) =>
      message.type === 'turn.text' ? { ok: true, turn_id: 't1' } : { ok: true });
    const { heading } = buildPage();
    const id = controller.registry.register(heading);

    const sending = controller.sendText('  what is this?  ');
    expect(get(store)).toMatchObject({ busy: true, activity: 'thinking', userText: 'what is this?', caption: '' });
    await sending;
    expect(request.mock.calls[0]![0]).toEqual({ type: 'turn.text', text: 'what is this?' });
    expect(get(store).turnId).toBe('t1');

    const events: TabTurnEvent[] = [
      { type: 'turn.state', turn_id: 't1', state: 'thinking' },
      { type: 'assistant.text.delta', turn_id: 't1', delta: 'This is ' },
      { type: 'assistant.text.delta', turn_id: 'other', delta: 'IGNORED' },
      { type: 'visual.directive', turn_id: 't1', directive: { kind: 'mark', to: id, style: 'circle' } },
      { type: 'assistant.text.delta', turn_id: 't1', delta: 'the heading.' },
    ];
    for (const event of events) controller.onTurnEvent(event);
    expect(get(store)).toMatchObject({ activity: 'speaking', caption: 'This is the heading.' });
    await vi.waitFor(() =>
      expect(renderer.mark).toHaveBeenCalledWith({ kind: 'element', element: heading }, 'circle', undefined));

    controller.onTurnEvent({ type: 'turn.done', turn_id: 't1', reason: 'end' });
    expect(get(store)).toMatchObject({ busy: false, activity: 'idle', turnId: null, caption: 'This is the heading.' });
    expect(displayOrb(get(store))).toBe('idle');
  });

  it('adopts the turn id when events overtake the worker reply', async () => {
    let release: (value: unknown) => void = () => undefined;
    const { store, controller } = setup();
    const pending = new Promise((resolve) => (release = resolve));
    (controller as unknown as { deps: { request: unknown } }).deps.request = () => pending;
    const sending = controller.sendText('hi');
    await Promise.resolve();
    controller.onTurnEvent({ type: 'assistant.text.delta', turn_id: 't9', delta: 'Hello' });
    expect(get(store)).toMatchObject({ turnId: 't9', caption: 'Hello' });
    release({ ok: true, turn_id: 't9' });
    await sending;
    expect(get(store).turnId).toBe('t9');
  });

  it('stop cancels the running turn through the worker and clears the overlay', async () => {
    const { store, renderer, request, controller } = setup((message) =>
      message.type === 'turn.text' ? { ok: true, turn_id: 't2' } : { ok: true });
    await controller.sendText('count slowly');
    await controller.stop();
    expect(request.mock.calls.map(([m]) => m.type)).toEqual(['turn.text', 'turn.stop']);
    expect(renderer.clear).toHaveBeenCalled();
    expect(get(store)).toMatchObject({ busy: false, turnId: null, activity: 'idle' });
    controller.onTurnEvent({ type: 'assistant.text.delta', turn_id: 't2', delta: 'late' });
    expect(get(store).caption).toBe('');
  });

  it('surfaces rejected turns and refusals as errors', async () => {
    const { store, controller } = setup(() => ({ ok: false, error: 'Not connected to the backend (connecting).' }));
    await controller.sendText('hello');
    expect(get(store)).toMatchObject({ activity: 'error', busy: false, error: 'Not connected to the backend (connecting).' });

    const second = setup((message) => (message.type === 'turn.text' ? { ok: true, turn_id: 't3' } : { ok: true }));
    await second.controller.sendText('something');
    second.controller.onTurnEvent({ type: 'turn.done', turn_id: 't3', reason: 'refusal' });
    expect(get(second.store)).toMatchObject({ activity: 'error', error: 'The model declined to answer that.' });
  });

  it('lets push-to-talk listening win over the page activity', () => {
    expect(displayOrb({ orb: 'listening', activity: 'speaking' })).toBe('listening');
    expect(displayOrb({ orb: 'idle', activity: 'thinking' })).toBe('thinking');
  });

  it('a voice answer is not over at idle while an announced sentence has yet to play', async () => {
    vi.useFakeTimers();
    const { store, controller } = setup();
    controller.beginVoiceTurn('v1');
    controller.onPttReleased();
    const header = (seq: number, text: string) =>
      controller.onTurnEvent({ type: 'speech.segment', turn_id: 'v1', seq, text, mime: 'audio/pcm', sample_rate: 24_000 });
    header(0, 'Right over here.');
    header(1, 'The button is what you want.');
    controller.onPlayback('v1', 0, 'started', 0);
    await vi.advanceTimersByTimeAsync(400);
    controller.onTurnEvent({ type: 'turn.done', turn_id: 'v1', reason: 'end', metrics: { stt_ms: 300 } });
    controller.onPlayback('v1', 0, 'idle', 0);          // the short first sentence ran out before the second arrived
    await vi.advanceTimersByTimeAsync(50);
    expect(get(store).busy).toBe(true);                // not over: sentence 1 is still on its way
    controller.onPlayback('v1', 1, 'started', 0);
    await vi.advanceTimersByTimeAsync(5_000);                // the captions follow the (estimated) speech
    expect(get(store).caption).toBe('Right over here. The button is what you want.');
    controller.onPlayback('v1', 1, 'idle', 0);
    await vi.advanceTimersByTimeAsync(50);
    expect(get(store).busy).toBe(false);
    expect(get(store).timeline?.marks.map((m) => m.label)).toEqual(['heard', 'transcript']);
    vi.useRealTimers();
  });
});
