import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CoreSocket, type SocketState } from './ws';
import type { AgentEvent } from '@/types';

/** Minimal controllable WebSocket stand-in. */
class FakeSocket {
  static last: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(public url: string) {
    FakeSocket.last = this;
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
    this.onclose?.({ code: 1000 });
  }
}

vi.mock('@/lib/api', () => ({
  resolveEndpoint: vi.fn().mockResolvedValue({
    ok: true,
    value: {
      baseUrl: 'http://127.0.0.1:9',
      wsUrl: 'ws://127.0.0.1:9/ws',
      token: 'tok en',
      version: '0.2.0',
      pid: 1,
    },
  }),
}));

beforeEach(() => {
  FakeSocket.last = null;
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket);
});

function makeSocket() {
  const events: AgentEvent[] = [];
  const states: SocketState[] = [];
  const sock = new CoreSocket({
    onEvent: (e) => events.push(e),
    onState: (s) => states.push(s),
  });
  return { sock, events, states };
}

describe('CoreSocket', () => {
  it('url-encodes the token into the query string', async () => {
    const { sock } = makeSocket();
    await sock.connect();
    expect(FakeSocket.last?.url).toBe('ws://127.0.0.1:9/ws?token=tok%20en');
  });

  it('reports connecting then open', async () => {
    const { sock, states } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onopen?.();
    expect(states).toEqual(['connecting', 'open']);
  });

  it('queues a message sent before the socket opens, then flushes it', async () => {
    const { sock } = makeSocket();
    sock.send({ type: 'chat', message: 'hi' });
    await vi.waitFor(() => expect(FakeSocket.last).not.toBeNull());
    expect(FakeSocket.last?.sent).toHaveLength(0);
    FakeSocket.last?.onopen?.();
    expect(JSON.parse(FakeSocket.last!.sent[0]!)).toMatchObject({ message: 'hi' });
  });

  it('parses agent events', async () => {
    const { sock, events } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onopen?.();
    FakeSocket.last?.onmessage?.({ data: JSON.stringify({ type: 'delta', text: 'hello' }) });
    expect(events[0]).toEqual({ type: 'delta', text: 'hello' });
  });

  it('turns unparseable frames into a reported error, not a crash', async () => {
    const { sock, events } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onopen?.();
    FakeSocket.last?.onmessage?.({ data: '{not json' });
    expect(events[0]).toMatchObject({ type: 'error', code: 'jarvis.bad_event' });
  });

  // 4401/4403 are the core's own auth rejections; retrying cannot fix them.
  it.each([4401, 4403])('does not retry after auth rejection %i', async (code) => {
    const { sock, states } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onclose?.({ code });
    expect(states.at(-1)).toBe('failed');
  });

  it('retries after an unexpected close', async () => {
    const { sock, states } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onopen?.();
    FakeSocket.last?.onclose?.({ code: 1006 });
    expect(states.at(-1)).toBe('connecting');
  });

  it('an explicit close does not retry', async () => {
    const { sock, states } = makeSocket();
    await sock.connect();
    FakeSocket.last?.onopen?.();
    sock.close();
    expect(states.at(-1)).toBe('closed');
  });

  it('reports failure when there is no core to reach', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.resolveEndpoint).mockResolvedValueOnce({
      ok: false,
      reason: { kind: 'no-bridge', what: 'core_endpoint' },
    });
    const { sock, states } = makeSocket();
    await sock.connect();
    expect(states.at(-1)).toBe('failed');
  });
});
