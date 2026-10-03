import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore, CURRENT_PHASE, QUICK_ACTIONS, PHASE, __resetSocket } from './store';
import type { CoreEndpoint, CoreHealth } from '@/types';

/**
 * No desktop shell under test, so the bridge reports `no-bridge` and the core
 * is unreachable. Tests that need a connected core install a ready state
 * directly rather than standing up a real WebSocket.
 */
vi.mock('@/lib/bridge', () => ({
  hasShell: () => false,
  getSystemSnapshot: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'system_snapshot' },
  }),
  getCoreEndpoint: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'core_endpoint' },
  }),
  emergencyStop: vi.fn().mockResolvedValue({ ok: true, value: null }),
  listen: vi.fn().mockResolvedValue(() => {}),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    clearHistory: vi.fn().mockResolvedValue({ ok: true, value: { deleted: 0 } }),
    engageEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: { engaged: true } }),
    clearEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: { engaged: false } }),
  };
});

const ENDPOINT: CoreEndpoint = {
  baseUrl: 'http://127.0.0.1:1',
  wsUrl: 'ws://127.0.0.1:1/ws',
  token: 't',
  version: '0.2.0',
  pid: 1,
};

const HEALTH: CoreHealth = {
  status: 'ok',
  version: '0.2.0',
  schemaVersion: 1,
  mode: 'guarded',
  emergencyStop: false,
  defaultProvider: 'dev_echo',
  credentialStore: { available: true, backend: 'test', detail: '' },
};

const reset = () => {
  __resetSocket();
  useStore.setState({
    messages: [],
    activity: [],
    consent: null,
    mode: 'guarded',
    stopped: false,
    busy: false,
    streamingId: null,
    voice: 'unavailable',
    view: 'chat',
    system: { state: 'loading' },
    core: { state: 'connecting' },
    socketState: 'idle',
    sessionId: null,
    providers: [],
  });
};

/** Mark the core as connected without opening a socket. */
const connect = () => useStore.setState({ core: { state: 'ready', endpoint: ENDPOINT, health: HEALTH } });

describe('store: chat', () => {
  beforeEach(reset);

  it('records the user message', () => {
    connect();
    useStore.getState().sendMessage('Hello');
    expect(useStore.getState().messages[0]).toMatchObject({ role: 'user', content: 'Hello' });
  });

  it('ignores blank input', () => {
    connect();
    useStore.getState().sendMessage('   ');
    expect(useStore.getState().messages).toHaveLength(0);
  });

  // The core, not the UI, produces replies — so with no core there must be no
  // answer at all, rather than a fabricated one.
  it('says the core is unavailable rather than inventing a reply', () => {
    useStore.setState({ core: { state: 'failed', message: 'no shell' } });
    useStore.getState().sendMessage('Hello');
    const msgs = useStore.getState().messages;
    expect(msgs).toHaveLength(2);
    expect(msgs[1]?.role).toBe('system');
    expect(msgs[1]?.notice).toBe(true);
    expect(msgs[1]?.content).toContain("core isn't available");
    expect(msgs.some((m) => m.role === 'assistant')).toBe(false);
  });

  it('explains that it is still connecting', () => {
    useStore.getState().sendMessage('Hello');
    expect(useStore.getState().messages.at(-1)?.content).toContain('Still connecting');
  });

  it('marks itself busy while a turn is in flight', () => {
    connect();
    useStore.getState().sendMessage('Hello');
    expect(useStore.getState().busy).toBe(true);
  });

  it('refuses a second message while busy', () => {
    connect();
    useStore.getState().sendMessage('first');
    useStore.getState().sendMessage('second');
    expect(useStore.getState().messages.filter((m) => m.role === 'user')).toHaveLength(1);
  });
});

describe('store: modes and emergency stop', () => {
  beforeEach(reset);

  it('refuses to act while paused', () => {
    connect();
    useStore.getState().setMode('paused');
    useStore.getState().sendMessage('Hello');
    expect(useStore.getState().messages.at(-1)?.content).toContain('paused');
  });

  it('latches stopped, pauses, and dismisses pending consent', async () => {
    useStore.getState().requestConsent({
      title: 'Delete files',
      summary: 'test',
      risk: 'high',
      origin: 'test',
      targets: ['a'],
      affectedCount: 1,
      reversible: 'permanent',
      blastRadius: 'test',
      allowRemember: false,
    });
    await useStore.getState().triggerEmergencyStop();

    const s = useStore.getState();
    expect(s.stopped).toBe(true);
    expect(s.mode).toBe('paused');
    expect(s.consent).toBeNull();
    expect(s.busy).toBe(false);
    expect(s.streamingId).toBeNull();
  });

  it('tells the core as well as the shell', async () => {
    const api = await import('@/lib/api');
    const bridge = await import('@/lib/bridge');
    await useStore.getState().triggerEmergencyStop();
    expect(api.engageEmergencyStop).toHaveBeenCalled();
    expect(bridge.emergencyStop).toHaveBeenCalled();
  });

  it('blocks messages while stopped', async () => {
    connect();
    await useStore.getState().triggerEmergencyStop();
    useStore.getState().sendMessage('Hello');
    expect(useStore.getState().messages.at(-1)?.content).toContain('Emergency stop');
  });

  it('clears on resume and tells the core', async () => {
    const api = await import('@/lib/api');
    await useStore.getState().triggerEmergencyStop();
    await useStore.getState().resume();
    expect(useStore.getState().stopped).toBe(false);
    expect(api.clearEmergencyStop).toHaveBeenCalled();
  });
});

describe('store: consent', () => {
  beforeEach(reset);

  const req = {
    title: 'Move 37 files',
    summary: 'test',
    risk: 'medium' as const,
    origin: 'test',
    targets: ['a', 'b'],
    affectedCount: 37,
    reversible: 'undoable' as const,
    blastRadius: 'test',
    allowRemember: true,
  };

  it('assigns an id and stores the request', () => {
    useStore.getState().requestConsent(req);
    expect(useStore.getState().consent?.id).toMatch(/^consent_/);
  });

  it('confirm on a preview prompt says no tool ran', () => {
    useStore.getState().requestConsent(req);
    useStore.getState().resolveConsent({ decision: 'confirm', remember: 'session' });
    expect(useStore.getState().consent).toBeNull();
    expect(useStore.getState().activity.at(-1)?.status).toBe('succeeded');
    expect(useStore.getState().messages.at(-1)?.content).toContain('no tool ran');
  });

  // A prompt from the core has an action waiting on the answer, so it must be
  // sent back rather than answered locally.
  it('a core prompt is answered over the socket, not locally', () => {
    connect();
    useStore.getState().handleEvent({
      type: 'consent.request',
      id: 'consent_1',
      title: 'Move 37 files',
      summary: 'Sort Downloads by type',
      risk: 'medium',
      origin: 'test',
      targets: ['a'],
      affectedCount: 37,
      reversible: 'undoable',
      blastRadius: 'Files stay inside Downloads.',
      allowRemember: true,
    });
    expect(useStore.getState().consent?.local).toBeUndefined();
    useStore.getState().resolveConsent({ decision: 'confirm', remember: 'no' });
    // No local "no tool ran" message: the core is doing the work.
    expect(useStore.getState().messages.at(-1)?.content ?? '').not.toContain('no tool ran');
  });

  it('declining a core prompt clears busy', () => {
    connect();
    useStore.setState({ busy: true });
    useStore.getState().handleEvent({
      type: 'consent.request',
      id: 'consent_2',
      title: 'Delete 3 files',
      summary: 'test',
      risk: 'high',
      origin: 'test',
      targets: ['a'],
      affectedCount: 3,
      reversible: 'permanent',
      blastRadius: 'test',
      allowRemember: false,
    });
    useStore.getState().resolveConsent({ decision: 'cancel' });
    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().activity.at(-1)?.status).toBe('cancelled');
  });

  it('cancel confirms nothing happened', () => {
    useStore.getState().requestConsent(req);
    useStore.getState().resolveConsent({ decision: 'cancel' });
    expect(useStore.getState().activity.at(-1)?.status).toBe('cancelled');
    expect(useStore.getState().messages.at(-1)?.content).toContain('Nothing happened');
  });

  it('resolving with nothing pending is a no-op', () => {
    useStore.getState().resolveConsent({ decision: 'cancel' });
    expect(useStore.getState().activity).toHaveLength(0);
  });
});

describe('store: core connection', () => {
  beforeEach(reset);

  it('reports failure with an explanation when there is no shell', async () => {
    await useStore.getState().connectCore();
    const core = useStore.getState().core;
    expect(core.state).toBe('failed');
    if (core.state === 'failed') expect(core.message).toContain('plain browser');
    expect(useStore.getState().activity.at(-1)?.status).toBe('failed');
  });
});

describe('store: system metrics', () => {
  beforeEach(reset);

  it('reports unavailable rather than fabricating numbers', async () => {
    await useStore.getState().refreshSystem();
    const sys = useStore.getState().system;
    expect(sys.state).toBe('unavailable');
    if (sys.state === 'unavailable') expect(sys.reason.kind).toBe('no-bridge');
  });
});

describe('store: voice', () => {
  beforeEach(reset);

  it('explains that the pipeline is missing instead of pretending to listen', () => {
    useStore.getState().toggleVoice();
    expect(useStore.getState().voice).toBe('unavailable');
    expect(useStore.getState().messages.at(-1)?.content).toContain('Phase 6');
  });
});

describe('phase gating', () => {
  beforeEach(reset);

  it('shipped phase matches the app-control phase', () => {
    expect(CURRENT_PHASE).toBe(PHASE.appControl);
  });

  // An action that claims to be available must have something behind it.
  it('available quick actions carry a template; gated ones do not', () => {
    for (const a of QUICK_ACTIONS) {
      if (a.availableIn <= CURRENT_PHASE) {
        expect(a.template, `${a.label} is available but has no template`).toBeTruthy();
      } else {
        expect(a.template, `${a.label} is gated but carries a template`).toBeUndefined();
      }
    }
  });

  it('an available quick action fills the composer instead of firing blind', () => {
    const { setDraft } = useStore.getState();
    setDraft('find duplicate files in downloads');
    expect(useStore.getState().draft).toBe('find duplicate files in downloads');
    expect(useStore.getState().messages).toHaveLength(0);
  });
});

describe('store: log bounds', () => {
  beforeEach(reset);

  it('caps the activity log', () => {
    for (let i = 0; i < 600; i++) {
      useStore.getState().logActivity({ summary: `e${i}`, status: 'succeeded' });
    }
    const act = useStore.getState().activity;
    expect(act).toHaveLength(500);
    expect(act.at(-1)?.summary).toBe('e599');
  });
});

describe('store: streamed agent events', () => {
  beforeEach(() => {
    reset();
    connect();
    useStore.setState({ busy: true });
  });

  const handle = (e: Parameters<ReturnType<typeof useStore.getState>['handleEvent']>[0]) =>
    useStore.getState().handleEvent(e);

  it('assembles deltas into one assistant message', () => {
    handle({ type: 'delta', text: 'Hello' });
    handle({ type: 'delta', text: ' there' });
    handle({ type: 'delta', text: '!' });
    const assistant = useStore.getState().messages.filter((m) => m.role === 'assistant');
    expect(assistant).toHaveLength(1);
    expect(assistant[0]?.content).toBe('Hello there!');
  });

  it('turn.start records the session so follow-ups stay in context', () => {
    handle({ type: 'turn.start', turn_id: 't1', session_id: 'ses_abc' });
    expect(useStore.getState().sessionId).toBe('ses_abc');
  });

  it('routing goes to the activity log, not the conversation', () => {
    handle({
      type: 'route',
      intent: 'computer_task',
      confidence: 0.9,
      reason: 'asks Jarvis to act on this computer',
      signals: ['open'],
      available_in_phase: 3,
    });
    expect(useStore.getState().messages).toHaveLength(0);
    expect(useStore.getState().activity.at(-1)?.summary).toContain('computer task');
  });

  // A capability notice must be visibly distinct from an answer.
  it('a notice is marked as such and ends the turn', () => {
    handle({
      type: 'notice',
      message: "I can't act on your computer yet.",
      intent: 'computer_task',
      available_in_phase: 3,
    });
    const last = useStore.getState().messages.at(-1);
    expect(last?.role).toBe('system');
    expect(last?.notice).toBe(true);
    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().activity.at(-1)?.status).toBe('blocked');
  });

  it('an error is surfaced and logged, never swallowed', () => {
    handle({
      type: 'error',
      code: 'jarvis.provider.unavailable',
      message: 'Could not reach the model server.',
      provider: 'ollama',
    });
    const last = useStore.getState().messages.at(-1);
    expect(last?.notice).toBe(true);
    expect(last?.content).toContain('Could not reach the model server');
    expect(last?.content).toContain('ollama');
    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().activity.at(-1)?.status).toBe('failed');
  });

  it('turn.end clears busy and records what ran', () => {
    handle({ type: 'delta', text: 'hi' });
    handle({
      type: 'turn.end',
      elapsed_ms: 420,
      handled: 'chat',
      provider: 'ollama',
      tokens_out: 12,
    });
    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().streamingId).toBeNull();
    expect(useStore.getState().activity.at(-1)?.detail).toContain('420 ms');
    expect(useStore.getState().activity.at(-1)?.detail).toContain('ollama');
  });

  it('a second turn starts a new message rather than appending to the first', () => {
    handle({ type: 'delta', text: 'first' });
    handle({ type: 'turn.end', elapsed_ms: 1, handled: 'chat' });
    handle({ type: 'delta', text: 'second' });
    const assistant = useStore.getState().messages.filter((m) => m.role === 'assistant');
    expect(assistant.map((m) => m.content)).toEqual(['first', 'second']);
  });
});

describe('store: tool events', () => {
  beforeEach(() => {
    reset();
    connect();
    useStore.setState({ busy: true });
  });

  const handle = (e: Parameters<ReturnType<typeof useStore.getState>['handleEvent']>[0]) =>
    useStore.getState().handleEvent(e);

  it('a tool result becomes a tool message, not an assistant reply', () => {
    handle({
      type: 'tool.result',
      tool: 'filesystem',
      operation: 'create_folder',
      ok: true,
      summary: 'Created folder University',
      changes: ['Created C:\\Users\\you\\Desktop\\University'],
      data: {},
      verified: true,
      elapsedMs: 12,
      risk: 'low',
    });
    const last = useStore.getState().messages.at(-1);
    expect(last?.role).toBe('tool');
    expect(last?.tool?.status).toBe('succeeded');
    expect(useStore.getState().messages.some((m) => m.role === 'assistant')).toBe(false);
  });

  // An unverified change must not be logged as a plain success.
  it('an unverified result says so in the activity log', () => {
    handle({
      type: 'tool.result',
      tool: 'filesystem',
      operation: 'create_folder',
      ok: false,
      summary: 'Created folder University',
      changes: [],
      data: {},
      verified: false,
      elapsedMs: 9,
      risk: 'low',
    });
    const entry = useStore.getState().activity.at(-1);
    expect(entry?.status).toBe('failed');
    expect(entry?.detail).toContain('NOT verified');
  });

  it('a denied tool call is logged as blocked', () => {
    handle({
      type: 'tool.planned',
      tool: 'filesystem',
      operation: 'delete',
      summary: 'Delete 3 files',
      affected: 3,
      risk: 'medium',
      verdict: 'deny',
      reason: 'This needs permission Jarvis does not have: Delete files',
    });
    expect(useStore.getState().activity.at(-1)?.status).toBe('blocked');
  });

  it('a plan is logged but does not appear in the conversation', () => {
    handle({
      type: 'plan',
      unsupported: '',
      steps: [{ tool: 'filesystem', args: { operation: 'list' }, rationale: 'list downloads' }],
    });
    expect(useStore.getState().messages).toHaveLength(0);
    expect(useStore.getState().activity.at(-1)?.summary).toContain('Planned 1 step');
  });
});
