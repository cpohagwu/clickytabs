/**
 * How the scene runtime runs model-written code, kept apart from three.js so it can be unit-tested.
 *
 * Small models write scenes the way they write web pages: `const g = canvas.getContext('2d')`, `const scene = new
 * THREE.Scene()`, `document.body.appendChild(renderer.domElement)`, `window.innerWidth`, `window.onload = init`,
 * curly quotes, a stray markdown fence. Each of those used to fail the scene. Here:
 * - the code runs inside an inner async function, so redeclaring an API name shadows it instead of being a
 *   SyntaxError (the API names are destructured in the outer function);
 * - a SyntaxError is retried once with fences dropped, curly quotes straightened and surrounding prose trimmed;
 * - `window` and `document` are small stand-ins: sizes, listeners (resize and load fire), canvases handed out
 *   (the scene's own canvas first, for 2D scenes), inert elements for everything else.
 * The worker (worker.ts) adds the three.js side: a tracked THREE namespace and adopting a scene the code made itself.
 */

/** Chrome numbers a Function-constructor body from line 3; wrap() puts two lines before the scene code. */
export const CODE_FIRST_LINE = 5;

export type AsyncFunctionConstructor = new (...args: string[]) => (...values: unknown[]) => Promise<unknown>;

export function wrap(names: string[], code: string): string {
  return `const { ${names.join(', ')} } = __cc;\nreturn (async () => {\n${code}${entryCalls(code)}\n})();`;
}

const ENTRY_NAMES = new Set(['main', 'run', 'init', 'start', 'setup', 'draw', 'animate', 'render', 'create', 'build',
  'initScene', 'createScene', 'buildScene']);

/**
 * Calls for entry functions the code declares but never uses (`async function run() {...}` and nothing else): a
 * zero-argument top-level declaration with a usual entry name that appears nowhere else in the code.
 */
export function entryCalls(code: string): string {
  const calls: string[] = [];
  for (const match of code.matchAll(/^(?:async\s+)?function\s+([A-Za-z_][\w]*)\s*\(\s*\)/gm)) {
    const name = match[1]!;
    if (!ENTRY_NAMES.has(name)) continue;
    const mentions = code.match(new RegExp(`(?<![\\w$.])${name}(?![\\w$])`, 'g'))?.length ?? 0;
    if (mentions === 1) calls.push(`await ${name}();`);
  }
  return calls.length ? `\n;${calls.join(' ')}` : '';
}

/** "TypeError: x is not a function (line 4: cube.spin())": the line counted inside the scene code. */
export function describeError(error: unknown, code: string): string {
  const err = error instanceof Error ? error : new Error(String(error));
  let text = `${err.name}: ${err.message}`;
  const match = /<anonymous>:(\d+):(\d+)/.exec(err.stack ?? '');
  if (match) {
    const line = Number(match[1]) - CODE_FIRST_LINE + 1;
    const source = code.split('\n')[line - 1]?.trim();
    if (line >= 1 && source) text += ` (line ${line}: ${source.slice(0, 120)})`;
  }
  return text.slice(0, 1_500);
}

function isProse(line: string): boolean {
  const text = line.trim();
  return text.length > 0 && !text.startsWith('//') && !/[;{}()=<>[\]`'"]/.test(text) && text.split(/\s+/).length >= 4;
}

/** Drops sentence-like lines before and after the code ("Here is the scene:"). */
export function trimProse(code: string): string {
  const lines = code.split('\n');
  let start = 0;
  let end = lines.length;
  while (start < end && (isProse(lines[start]!) || !lines[start]!.trim())) start += 1;
  while (end > start && (isProse(lines[end - 1]!) || !lines[end - 1]!.trim())) end -= 1;
  return lines.slice(start, end).join('\n');
}

/** Cleaned-up variants of code that didn't compile, mildest first. */
export function repairs(code: string): string[] {
  const variants: string[] = [];
  const add = (variant: string): void => {
    if (variant !== code && variant.trim() && !variants.includes(variant)) variants.push(variant);
  };
  const unfenced = code.split('\n').filter((line) => !/^\s*```/.test(line)).join('\n');
  add(unfenced);
  const straight = unfenced.replace(/[‘’‚′]/g, "'").replace(/[“”„″]/g, '"');
  add(straight);
  add(trimProse(straight));
  return variants;
}

export type Compiled =
  | { ok: true; run: (api: Record<string, unknown>) => Promise<unknown>; code: string }
  | { ok: false; error: unknown };

export function compile(AsyncFunction: AsyncFunctionConstructor, names: string[], code: string): Compiled {
  const attempt = (source: string): Compiled => {
    const fn = new AsyncFunction('__cc', wrap(names, source));
    return { ok: true, run: (api) => fn(api), code: source };
  };
  try {
    return attempt(code);
  } catch (error) {
    for (const variant of repairs(code)) {
      try {
        return attempt(variant);
      } catch {
        // next variant
      }
    }
    return { ok: false, error };
  }
}

// ------------------------------------------------------------------------------------------- window, document

type Listener = (event: { type: string }) => void;

export interface Shims {
  window: Record<string, unknown>;
  document: Record<string, unknown>;
  /** Run the listeners (and the `on<type>` handler) of window and document for an event type. */
  fire: (type: string) => void;
}

function inertElement(): Record<string, unknown> {
  const element: Record<string, unknown> = {
    style: {}, dataset: {}, children: [], textContent: '', innerHTML: '',
    appendChild: (child: unknown) => child, append: () => {}, prepend: () => {}, remove: () => {},
    removeChild: (child: unknown) => child, setAttribute: () => {}, getAttribute: () => null,
    addEventListener: () => {}, removeEventListener: () => {}, focus: () => {},
    classList: { add: () => {}, remove: () => {}, toggle: () => false, contains: () => false },
    getBoundingClientRect: () => ({ left: 0, top: 0, x: 0, y: 0, width: 0, height: 0, right: 0, bottom: 0 }),
  };
  return element;
}

export function makeShims(options: {
  size: () => { width: number; height: number };
  dpr: number;
  /** Handed out by the first createElement('canvas') / getElementById / querySelector('canvas'). */
  primaryCanvas?: object | null;
  newCanvas: (width: number, height: number) => object;
}): Shims {
  const listeners = new Map<string, Set<Listener>>();
  const on = (scope: string) => (type: string, listener: unknown) => {
    if (typeof listener !== 'function') return;
    const key = `${scope}:${type}`;
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key)!.add(listener as Listener);
  };
  const off = (scope: string) => (type: string, listener: unknown) => {
    listeners.get(`${scope}:${type}`)?.delete(listener as Listener);
  };
  let primaryTaken = false;
  const canvas = (): object => {
    if (options.primaryCanvas && !primaryTaken) {
      primaryTaken = true;
      return options.primaryCanvas;
    }
    return options.newCanvas(300, 150);
  };

  const win: Record<string, unknown> = {
    get innerWidth() {
      return options.size().width;
    },
    get innerHeight() {
      return options.size().height;
    },
    devicePixelRatio: options.dpr,
    addEventListener: on('window'),
    removeEventListener: off('window'),
    requestAnimationFrame: (callback: FrameRequestCallback) => requestAnimationFrame(callback),
    cancelAnimationFrame: (id: number) => cancelAnimationFrame(id),
    setTimeout: (handler: () => void, ms?: number) => setTimeout(handler, ms),
    setInterval: (handler: () => void, ms?: number) => setInterval(handler, ms),
    clearTimeout: (id: number) => clearTimeout(id),
    clearInterval: (id: number) => clearInterval(id),
    performance,
    console,
    onload: null,
    onresize: null,
  };
  win.window = win;
  win.self = win;

  const doc: Record<string, unknown> = {
    body: inertElement(),
    head: inertElement(),
    documentElement: inertElement(),
    createElement: (tag: unknown) => (String(tag).toLowerCase() === 'canvas' ? canvas() : inertElement()),
    createElementNS: (_ns: unknown, tag: unknown) => (String(tag).toLowerCase() === 'canvas' ? canvas() : inertElement()),
    getElementById: () => options.primaryCanvas ?? null,
    querySelector: (selector: unknown) => (/canvas/i.test(String(selector)) ? options.primaryCanvas ?? null : null),
    querySelectorAll: () => [],
    addEventListener: on('document'),
    removeEventListener: off('document'),
    readyState: 'complete',
  };

  const fire = (type: string): void => {
    for (const scope of ['window', 'document']) {
      for (const listener of listeners.get(`${scope}:${type}`) ?? []) listener({ type });
    }
    const handler = win[`on${type}`];
    if (typeof handler === 'function') (handler as Listener)({ type });
  };
  return { window: win, document: doc, fire };
}
