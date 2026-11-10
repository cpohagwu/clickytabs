// @vitest-environment happy-dom
/** The markup sanitizer, the scene message contract and the stage's state machine. */
import { get } from 'svelte/store';
import { describe, expect, it } from 'vitest';

import { isAllowed, parseSwRequest, parseTabMessage, type SenderKind } from '@/lib/messages';
import { sanitizeScene } from '@/lib/scene/sanitize';
import { type SceneResultMessage, StageController, stageStore } from '@/lib/scene/stage';

const html = (markup: string) => sanitizeScene('html', markup);
const svg = (markup: string) => sanitizeScene('svg', markup);

describe('scene markup sanitizer', () => {
  it('drops scripts, navigation and network vectors from html and keeps the content', () => {
    const { root, removed } = html(`<meta http-equiv="refresh" content="0;url=https://evil.example">
      <base href="https://evil.example/"><script>alert(1)</script><style>.c{color:#fff}</style>
      <div class="c" onclick="alert(1)" style="color:red"><a href="https://evil.example">Visit <b>us</b></a>
      <img src="https://evil.example/t.png"><img src="data:image/png;base64,iVBORw0KGgo=" alt="dot">
      <form action="https://evil.example"><input name="q"><button>Go</button></form>
      <iframe name="frame"></iframe><object data="x.swf"></object><p>Safe text</p></div>`);
    expect(root).not.toBeNull();
    const out = root!.innerHTML;
    for (const gone of ['<script', '<meta', '<base', '<form', '<input', '<iframe', '<object', 'onclick', '<a ',
      'evil.example']) {
      expect(out).not.toContain(gone);
    }
    expect(out).toContain('Visit <b>us</b>');                 // the link is unwrapped, its text kept
    expect(out).toContain('<style>.c{color:#fff}</style>');
    expect(out).toContain('style="color:red"');
    expect(out).toContain('data:image/png;base64');
    expect(out).toContain('Safe text');
    expect(removed).toEqual(expect.arrayContaining(['<meta>', '<script>', 'onclick', '<a>', '<form>', '<iframe>']));
  });

  it('keeps svg drawing features, removes handlers, links, foreign content and outside references', () => {
    const { root, removed } = svg(`Here is the diagram: <svg width="200" height="100" onload="alert(1)">
      <defs><marker id="m"><path d="M0,0 L10,5 L0,10z"/></marker></defs>
      <a href="javascript:alert(1)"><rect width="50" height="50" fill="red"/></a>
      <foreignObject><div>html</div></foreignObject>
      <set attributeName="href" to="javascript:alert(1)"/><animate attributeName="opacity" values="0;1" dur="1s"/>
      <use href="#m"/><use href="https://evil.example/x.svg#a"/>
      <image href="data:image/png;base64,iVBORw0KGgo="/><image href="https://evil.example/i.png"/>
      <line x1="0" y1="0" x2="10" y2="10" marker-end="url(#m)" style="background:url(javascript:alert(1))"/></svg>`);
    expect(root?.localName).toBe('svg');
    const out = root!.outerHTML;
    expect(root!.getAttribute('viewBox')).toBe('0 0 200 100');
    expect(root!.getAttribute('width')).toBe('100%');
    expect(out).not.toMatch(/onload|javascript:|foreignobject|evil\.example|<a /i);
    expect(out).toContain('<rect');                            // the link's drawing stays
    expect(out).toContain('marker-end="url(#m)"');
    expect(out).toContain('href="#m"');
    expect(out).toContain('attributeName="opacity"');
    expect(out).toContain('data:image/png;base64');
    expect(removed).toEqual(expect.arrayContaining(['onload', '<a>', '<foreignobject>', 'attributename']));
  });

  it('reports nothing to show', () => {
    expect(svg('<div>no svg</div>').root).toBeNull();
    expect(html('<script>only()</script>').root).toBeNull();
  });
});

describe('scene messages', () => {
  const page: SenderKind = 'extension-page';
  const top: SenderKind = 'content-top';
  const frame: SenderKind = 'content-frame';

  it('accepts well-formed scene results from the top frame only', () => {
    const result = parseSwRequest({ type: 'scene.result', scene_id: 'abcdef0123', attempt: 1, ok: false,
      error: 'x'.repeat(5000), extra: 'dropped' });
    expect(result).toEqual({ type: 'scene.result', scene_id: 'abcdef0123', attempt: 1, ok: false, error: 'x'.repeat(2000) });
    expect(parseSwRequest({ type: 'scene.result', scene_id: '<script>', attempt: 1, ok: true })).toBeNull();
    expect(parseSwRequest({ type: 'scene.result', scene_id: 'abcdef0123', attempt: 0, ok: true })).toBeNull();
    expect(isAllowed('scene.result', top)).toBe(true);
    expect(isAllowed('scene.result', frame)).toBe(false);
    expect(isAllowed('debug.sceneRender', top)).toBe(false);
    expect(isAllowed('debug.sceneRender', page)).toBe(true);
  });

  it('forwards only valid scene events to tabs', () => {
    const render = { type: 'scene.render', turn_id: 't1', scene_id: 'abcdef0123', kind: 'three', code: 'x' };
    expect(parseTabMessage({ type: 'scene.event', event: render })).toEqual({ type: 'scene.event', event: render });
    expect(parseTabMessage({ type: 'scene.event', event: { ...render, kind: 'video' } })).toBeNull();
    expect(parseTabMessage({ type: 'scene.event', event: { ...render, code: 'x'.repeat(60_001) } })).toBeNull();
    expect(parseTabMessage({ type: 'scene.event', event: { type: 'scene.failed', turn_id: 't1', scene_id: 'abcdef0123' } }))
      .toBeNull();
  });
});

describe('scene stage', () => {
  function stage(): { controller: StageController; sent: SceneResultMessage[] } {
    const sent: SceneResultMessage[] = [];
    const controller = new StageController({ sendResult: (r) => sent.push(r), sceneUrl: () => 'about:blank' });
    return { controller, sent };
  }
  const building = { type: 'scene.building' as const, turn_id: 't1', scene_id: 'scene00001', kind: 'three' as const,
    title: 'Cube' };
  const render = { type: 'scene.render' as const, turn_id: 't1', scene_id: 'scene00001', kind: 'three' as const,
    title: 'Cube', code: 'x', attempt: 1 };

  it('opens on building and keeps a render until the panel mounts', () => {
    const { controller, sent } = stage();
    controller.onEvent(building);
    expect(get(stageStore)).toMatchObject({ open: true, status: 'building', title: 'Cube', sceneId: 'scene00001' });
    controller.onEvent(render);
    expect(get(stageStore).status).toBe('rendering');
    expect(controller.openScene()).toBeNull();                // not shown yet: no follow-up edits of it
    controller.close();                                       // closed before it ever mounted
    expect(sent).toEqual([{ scene_id: 'scene00001', attempt: 1, ok: false, unavailable: true,
      error: 'the user closed the stage' }]);
    expect(get(stageStore).open).toBe(false);
  });

  it('turns down a scene that was closed while it was being written', () => {
    const { controller, sent } = stage();
    controller.onEvent(building);
    controller.close();
    controller.onEvent(render);
    expect(get(stageStore).open).toBe(false);
    expect(sent).toEqual([{ scene_id: 'scene00001', attempt: 1, ok: false, unavailable: true,
      error: 'the user closed the stage' }]);
    controller.onEvent({ ...building, scene_id: 'scene00002' });   // the next scene opens again
    expect(get(stageStore)).toMatchObject({ open: true, sceneId: 'scene00002' });
    controller.onEvent({ type: 'scene.failed', turn_id: 't1', scene_id: 'scene00002', error: 'no model' });
    expect(get(stageStore)).toMatchObject({ status: 'failed', error: 'no model' });
  });
});

describe('scene stage and the connection', () => {
  it('fails a scene being written when the backend connection drops, but keeps a shown one', () => {
    const controller = new StageController({ sendResult: () => {}, sceneUrl: () => 'about:blank' });
    controller.onEvent({ type: 'scene.building', turn_id: 't1', scene_id: 'scene00009', kind: 'svg', title: 'Map' });
    controller.connectionLost();
    expect(get(stageStore)).toMatchObject({ status: 'failed' });
    expect(get(stageStore).error).toContain('Lost the connection');
    controller.close();
    controller.connectionLost();                              // nothing open: nothing changes
    expect(get(stageStore).open).toBe(false);
  });
});
