/**
 * Streaming channel to the Jarvis core.
 *
 * Reconnects with backoff, queues a message sent while disconnected, and
 * reports state changes so the UI can say what is happening instead of
 * silently doing nothing.
 */
import type { AgentEvent } from '@/types';
import { resolveEndpoint } from '@/lib/api';

export type SocketState = 'idle' | 'connecting' | 'open' | 'closed' | 'failed';

export interface StreamHandlers {
  onEvent: (event: AgentEvent) => void;
  onState: (state: SocketState, detail?: string) => void;
}

const MAX_RETRIES = 5;
const BASE_DELAY_MS = 500;

export class CoreSocket {
  private ws: WebSocket | null = null;
  private state: SocketState = 'idle';
  private retries = 0;
  private pending: string[] = [];
  private closedByUs = false;

  constructor(private handlers: StreamHandlers) {}

  private setState(state: SocketState, detail?: string): void {
    this.state = state;
    this.handlers.onState(state, detail);
  }

  async connect(): Promise<void> {
    if (this.state === 'open' || this.state === 'connecting') return;
    this.closedByUs = false;
    this.setState('connecting');

    const resolved = await resolveEndpoint();
    if (!resolved.ok) {
      this.setState(
        'failed',
        resolved.reason.kind === 'no-bridge'
          ? 'The core runs under the desktop shell; there is none in a plain browser.'
          : 'Could not find the Jarvis core.',
      );
      return;
    }

    // The token rides in the query string: a browser cannot set headers on a
    // WebSocket handshake, and the URL never leaves this machine.
    const url = `${resolved.value.wsUrl}?token=${encodeURIComponent(resolved.value.token)}`;
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      this.setState('failed', `Could not open the channel: ${String(err)}`);
      return;
    }
    this.ws = socket;

    socket.onopen = () => {
      this.retries = 0;
      this.setState('open');
      for (const queued of this.pending.splice(0)) socket.send(queued);
    };

    socket.onmessage = (event) => {
      // Parsing and handling are caught separately. Lumping them together
      // reported a crash in this build as "the core sent something unreadable",
      // which blames the wrong component and sends you looking in the wrong
      // place.
      let parsed: AgentEvent;
      try {
        parsed = JSON.parse(String(event.data)) as AgentEvent;
      } catch {
        this.handlers.onEvent({
          type: 'error',
          code: 'jarvis.bad_event',
          message: 'The core sent a message this build could not read.',
        });
        return;
      }
      try {
        this.handlers.onEvent(parsed);
      } catch (err) {
        this.handlers.onEvent({
          type: 'error',
          code: 'jarvis.event_handler_failed',
          message: `Jarvis received a "${parsed.type}" event but failed to display it: ${String(err)}`,
        });
      }
    };

    socket.onerror = () => {
      // `onclose` always follows, and carries the actionable detail.
    };

    socket.onclose = (event) => {
      this.ws = null;
      if (this.closedByUs) {
        this.setState('closed');
        return;
      }
      // 4401/4403 are our own auth rejections; retrying cannot fix them.
      if (event.code === 4401 || event.code === 4403) {
        this.setState('failed', 'The core rejected this connection.');
        return;
      }
      if (this.retries >= MAX_RETRIES) {
        this.setState('failed', 'Lost the connection to the Jarvis core.');
        return;
      }
      const delay = BASE_DELAY_MS * 2 ** this.retries;
      this.retries += 1;
      this.setState('connecting', `Reconnecting in ${Math.round(delay / 100) / 10}s…`);
      setTimeout(() => void this.connect(), delay);
    };
  }

  send(message: Record<string, unknown>): void {
    const payload = JSON.stringify(message);
    if (this.ws && this.state === 'open') {
      this.ws.send(payload);
      return;
    }
    this.pending.push(payload);
    void this.connect();
  }

  close(): void {
    this.closedByUs = true;
    this.pending = [];
    this.ws?.close();
    this.ws = null;
    this.setState('closed');
  }
}
