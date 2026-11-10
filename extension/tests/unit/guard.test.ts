/**
 * Content scripts run on behalf of the page's origin: talking to the backend from there would hit the page's CSP,
 * CORS and Chrome's Local Network Access prompt, and storage.local is locked to trusted contexts. This test walks the
 * import graph of every content script (and the sandbox harness) and fails if any reachable module opens a socket,
 * fetches, or touches storage.local / the settings module.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src');
const ENTRIES = ['entrypoints/content/index.ts', 'entrypoints/hotkeys.content.ts', 'entrypoints/scene-harness.ts',
  'lib/scene/worker.ts'];   // the scene Worker is imported with ?worker&inline, which the import walk can't follow
const FORBIDDEN: [RegExp, string][] = [
  [/new\s+WebSocket\s*\(/, 'opens a WebSocket'],
  [/\bfetch\s*\(/, 'calls fetch()'],
  [/XMLHttpRequest/, 'uses XMLHttpRequest'],
  [/storage\.local\.(set|remove|clear)\b/, 'writes chrome.storage.local'],
  [/from\s+['"]@\/lib\/(settings|socket|sw-client|offscreen)['"]/, 'imports a privileged module'],
];

function resolveImport(from: string, spec: string): string | null {
  if (!spec.startsWith('@/') && !spec.startsWith('.')) return null;
  const base = spec.startsWith('@/') ? resolve(SRC, spec.slice(2)) : resolve(dirname(from), spec);
  for (const candidate of [base, `${base}.ts`, `${base}/index.ts`]) {
    try {
      readFileSync(candidate);
      return candidate;
    } catch {
      // try the next extension
    }
  }
  return null;
}

function reachable(entry: string): Map<string, string> {
  const files = new Map<string, string>();
  const queue = [resolve(SRC, entry)];
  while (queue.length) {
    const file = queue.pop()!;
    if (files.has(file)) continue;
    const text = readFileSync(file, 'utf8');
    files.set(file, text);
    for (const match of text.matchAll(/(?:import|export)[^'"]*?from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]/g)) {
      const next = resolveImport(file, match[1] ?? match[2] ?? '');
      if (next && (next.endsWith('.ts') || next.endsWith('.svelte'))) queue.push(next);
    }
  }
  return files;
}

describe('content-script guard', () => {
  for (const entry of ENTRIES) {
    it(`${entry} never reaches the backend or privileged storage`, () => {
      const problems: string[] = [];
      for (const [file, text] of reachable(entry)) {
        const code = text.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, ''); // ignore comments
        for (const [pattern, why] of FORBIDDEN) {
          if (pattern.test(code)) problems.push(`${file.slice(SRC.length + 1)} ${why}`);
        }
      }
      expect(problems).toEqual([]);
    });
  }
});
