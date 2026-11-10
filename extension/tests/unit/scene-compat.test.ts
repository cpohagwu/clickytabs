/** How scene code is compiled and run (compat.ts) - the habits of small models that used to fail scenes. */
import { describe, expect, it } from 'vitest';

import {
  type AsyncFunctionConstructor,
  compile,
  describeError,
  entryCalls,
  makeShims,
  repairs,
  trimProse,
} from '@/lib/scene/compat';

const AsyncFunction = Object.getPrototypeOf(async () => {}).constructor as AsyncFunctionConstructor;

async function run(code: string, api: Record<string, unknown>): Promise<unknown> {
  const compiled = compile(AsyncFunction, Object.keys(api), code);
  if (!compiled.ok) throw compiled.error;
  return compiled.run(api);
}

describe('scene code compatibility', () => {
  it('lets the code redeclare API names (it shadows them) and still use them', async () => {
    expect(await run('return g + width;', { g: 2, width: 3 })).toBe(5);
    expect(await run('const g = 10;\nreturn g;', { g: 2 })).toBe(10);
    expect(await run('const scene = { mine: true };\nreturn scene.mine;', { scene: {} })).toBe(true);
    expect(await run('await null;\nreturn ctx.width;', { ctx: { width: 7 } })).toBe(7);    // top-level await
  });

  it('reports runtime errors with the line inside the scene code', async () => {
    const code = 'const a = 1;\nconst b = null;\nb.spin();';
    const error = await run(code, { g: 1 }).catch((e: unknown) => e);
    expect(describeError(error, code)).toMatch(/^TypeError: .*\(line 3: b\.spin\(\);\)$/);
  });

  it('retries code that does not compile with fences, curly quotes and prose cleaned up', async () => {
    const messy = 'Here is the scene you asked for:\n```javascript\nconst label = ‘Earth’;\nreturn label;\n```';
    expect(await run(messy, { g: 1 })).toBe('Earth');
    expect(repairs('ok();')).toEqual([]);
    expect(trimProse('This draws the waves nicely\nonFrame(() => {});\nHope this helps you a lot')).toBe('onFrame(() => {});');
    const broken = compile(AsyncFunction, ['g'], 'onFrame(() => {');
    expect(broken.ok).toBe(false);
  });

  it('calls an entry function the code declares but never calls', async () => {
    expect(entryCalls('async function run() {\n  done = true;\n}')).toBe('\n;await run();');
    expect(entryCalls('function init() {}\ninit();')).toBe('');                 // already called
    expect(entryCalls('function draw() {}\nonFrame(draw);')).toBe('');          // used elsewhere
    expect(entryCalls('function helper() {}')).toBe('');                        // not an entry name
    expect(entryCalls('  function run() {}')).toBe('');                         // not top level
    const api = { state: { ran: false } };
    await run('async function main() {\n  state.ran = true;\n}', api);
    expect(api.state.ran).toBe(true);
  });

  it('stands in for window and document', () => {
    const primary = { id: 'scene-canvas' };
    let made = 0;
    const shims = makeShims({ size: () => ({ width: 760, height: 480 }), dpr: 2, primaryCanvas: primary,
      newCanvas: () => ({ id: `extra-${(made += 1)}` }) });
    const win = shims.window as { innerWidth: number; innerHeight: number; onload: unknown;
      addEventListener: (type: string, fn: () => void) => void };
    const doc = shims.document as { createElement: (tag: string) => unknown; getElementById: (id: string) => unknown;
      body: { appendChild: (x: unknown) => unknown }; addEventListener: (type: string, fn: () => void) => void };
    expect([win.innerWidth, win.innerHeight]).toEqual([760, 480]);
    expect(doc.createElement('canvas')).toBe(primary);                        // the scene's own canvas first
    expect(doc.createElement('CANVAS')).toEqual({ id: 'extra-1' });
    expect(doc.getElementById('c')).toBe(primary);
    expect(doc.body.appendChild(primary)).toBe(primary);
    const fired: string[] = [];
    win.onload = () => fired.push('onload');
    win.addEventListener('load', () => fired.push('window'));
    doc.addEventListener('load', () => fired.push('document'));
    win.addEventListener('resize', () => fired.push('resize'));
    shims.fire('load');
    expect(fired).toEqual(['window', 'document', 'onload']);
  });
});
