import { beforeEach, describe, expect, it } from 'vitest';
import { useStore, CURRENT_PHASE, QUICK_ACTIONS } from './store';

const reset = () =>
  useStore.setState({
    messages: [],
    activity: [],
    consent: null,
    mode: 'guarded',
    stopped: false,
    busy: false,
    voice: 'unavailable',
    view: 'chat',
    system: { state: 'loading' },
  });

describe('store: chat', () => {
  beforeEach(reset);

  it('records the user message', () => {
    useStore.getState().sendMessage('Open Chrome');
    const msgs = useStore.getState().messages;
    expect(msgs[0]?.role).toBe('user');
    expect(msgs[0]?.content).toBe('Open Chrome');
  });

  // The central honesty guarantee for Phase 1: no fabricated assistant replies.
  it('answers with a not-implemented notice, never a simulated assistant reply', () => {
    useStore.getState().sendMessage('Open Chrome');
    const msgs = useStore.getState().messages;
    expect(msgs).toHaveLength(2);
    expect(msgs[1]?.role).toBe('system');
    expect(msgs[1]?.notice).toBe(true);
    expect(msgs[1]?.content).toContain('Phase 2');
    expect(msgs.some((m) => m.role === 'assistant')).toBe(false);
  });

  it('ignores blank input', () => {
    useStore.getState().sendMessage('   ');
    expect(useStore.getState().messages).toHaveLength(0);
  });

  it('truncates a long quoted request in the notice', () => {
    useStore.getState().sendMessage('x'.repeat(200));
    expect(useStore.getState().messages[1]?.content).toContain('…');
  });

  it('logs the message as blocked rather than succeeded', () => {
    useStore.getState().sendMessage('Delete everything');
    const act = useStore.getState().activity.at(-1);
    expect(act?.status).toBe('blocked');
  });
});

describe('store: modes and emergency stop', () => {
  beforeEach(reset);

  it('refuses to act while paused', () => {
    useStore.getState().setMode('paused');
    useStore.getState().sendMessage('Open Chrome');
    const last = useStore.getState().messages.at(-1);
    expect(last?.content).toContain('paused');
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
    expect(useStore.getState().consent).not.toBeNull();

    await useStore.getState().triggerEmergencyStop();

    const s = useStore.getState();
    expect(s.stopped).toBe(true);
    expect(s.mode).toBe('paused');
    expect(s.consent).toBeNull();
    expect(s.voice).toBe('off');
  });

  it('blocks messages while stopped', async () => {
    await useStore.getState().triggerEmergencyStop();
    useStore.getState().sendMessage('Open Chrome');
    expect(useStore.getState().messages.at(-1)?.content).toContain('Emergency stop');
  });

  it('clears on resume', async () => {
    await useStore.getState().triggerEmergencyStop();
    useStore.getState().resume();
    expect(useStore.getState().stopped).toBe(false);
    expect(useStore.getState().mode).toBe('guarded');
  });

  it('leaving paused mode clears the stop latch', async () => {
    await useStore.getState().triggerEmergencyStop();
    useStore.getState().setMode('assisted');
    expect(useStore.getState().stopped).toBe(false);
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

  it('confirm logs success and states that no tool ran', () => {
    useStore.getState().requestConsent(req);
    useStore.getState().resolveConsent({ decision: 'confirm', remember: 'session' });
    expect(useStore.getState().consent).toBeNull();
    const act = useStore.getState().activity.at(-1);
    expect(act?.status).toBe('succeeded');
    expect(act?.detail).toContain('session');
    expect(useStore.getState().messages.at(-1)?.content).toContain('No tool ran');
  });

  it('cancel logs cancellation and confirms nothing happened', () => {
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

describe('store: system metrics', () => {
  beforeEach(reset);

  // Without the shell there is no source of truth, so the only correct result
  // is `unavailable`. A zero or a placeholder here would be a lie.
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
  it('every quick action is gated beyond the current phase', () => {
    expect(CURRENT_PHASE).toBe(1);
    for (const a of QUICK_ACTIONS) {
      expect(a.availableIn).toBeGreaterThan(CURRENT_PHASE);
    }
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
