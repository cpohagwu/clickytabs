import { describe, expect, it, vi } from 'vitest';

import type { ConnInfo } from '@/lib/conn';
import { isAllowed, parseSwRequest } from '@/lib/messages';
import { type SocketHandlers, SocketManager } from '@/lib/socket';

const silentLog = { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} };

function connect(stored: string | null) {
  const sent: Record<string, unknown>[] = [];
  const written: string[] = [];
  const published: ConnInfo[] = [];
  let handlers: SocketHandlers | null = null;
  const manager = new SocketManager({
    readCredentials: async () => ({ backendUrl: 'ws://127.0.0.1:8765/v1/ws', token: 'token-0123456789' }),
    createSocket: (_url, h) => {
      handlers = h;
      return { send: (data) => sent.push(JSON.parse(data)), close: () => {} };
    },
    probeHealth: async () => true,
    publish: (info) => published.push(info),
    readBlocked: async () => null,
    writeBlocked: async () => {},
    onServerEvent: () => {},
    conversation: { read: async () => stored, write: (id) => written.push(id) },
    extensionId: 'fhgliaahcafninaodeelekpdemjkifmf',
    clientVersion: 'test',
    log: silentLog,
  });
  void manager.ensureConnected('test');
  return { manager, sent, written, published, handlers: () => handlers };
}

describe('conversation continuity', () => {
  it('sends the stored conversation id in hello and keeps the one welcome names', async () => {
    const { sent, written, published, handlers } = connect('conversation-abc123');
    await vi.waitFor(() => expect(handlers()).not.toBeNull());
    handlers()!.onOpen();
    expect(sent[0]).toMatchObject({ type: 'hello', conversation_id: 'conversation-abc123' });
    handlers()!.onMessage(JSON.stringify({ type: 'welcome', session_id: 's', server_version: '0', profile: 'cloud',
      capabilities: [], conversation_id: 'conversation-abc123', conversation_turns: 3 }));
    expect(written).toEqual(['conversation-abc123']);
    expect(published.at(-1)).toMatchObject({ state: 'connected', conversationId: 'conversation-abc123',
      conversationTurns: 3 });
  });

  it('never sends a malformed stored id', async () => {
    const { sent, handlers } = connect('bad id with spaces');
    await vi.waitFor(() => expect(handlers()).not.toBeNull());
    handlers()!.onOpen();
    expect(sent[0]).not.toHaveProperty('conversation_id');
  });

  it('lets only extension pages reset the conversation or pick a capture budget', () => {
    expect(parseSwRequest({ type: 'conversation.reset' })).toEqual({ type: 'conversation.reset' });
    expect(isAllowed('conversation.reset', 'content-top')).toBe(false);
    expect(parseSwRequest({ type: 'debug.pageContext', tabId: 3, budget: 'full' }))
      .toEqual({ type: 'debug.pageContext', tabId: 3, budget: 'full' });
    expect(parseSwRequest({ type: 'debug.pageContext', tabId: 3, budget: 'huge' })).toBeNull();
  });
});
