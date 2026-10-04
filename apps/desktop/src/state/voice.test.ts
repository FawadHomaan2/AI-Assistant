import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore } from './store';
import * as api from '@/lib/api';

/**
 * Voice control in the interface.
 *
 * What matters here is the refusals and the indicator. A microphone that stays
 * open is the most invasive thing this program does, so "it started when it
 * should not have" and "it showed as off while it was on" are both worse bugs
 * than failing to start.
 */
vi.mock('@/lib/bridge', () => ({
  hasShell: () => false,
  getSystemSnapshot: vi
    .fn()
    .mockResolvedValue({ ok: false, reason: { kind: 'no-bridge', what: 'system_snapshot' } }),
  getCoreEndpoint: vi
    .fn()
    .mockResolvedValue({ ok: false, reason: { kind: 'no-bridge', what: 'core_endpoint' } }),
  emergencyStop: vi.fn().mockResolvedValue({ ok: true, value: null }),
  listen: vi.fn().mockResolvedValue(() => {}),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    startListening: vi.fn(),
    stopListening: vi.fn(),
    voiceStatus: vi.fn(),
    engageEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: { engaged: true } }),
  };
});

const READY = {
  available: true,
  detail: '1 input device(s) available.',
  listening: false,
  dropped: 0,
};

/** Put the store in the one state where listening is allowed to start. */
function allowed(): void {
  useStore.setState({
    voice: 'off',
    voiceReason: '',
    voiceMic: READY,
    micScopeGranted: true,
    stopped: false,
    messages: [],
    activity: [],
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  allowed();
});

describe('starting and stopping', () => {
  it('starts listening and shows the state the core reports', async () => {
    vi.mocked(api.startListening).mockResolvedValue({
      ok: true,
      value: { listening: true, state: 'listening' },
    });

    await useStore.getState().toggleVoice();

    expect(api.startListening).toHaveBeenCalled();
    expect(useStore.getState().voice).toBe('listening');
  });

  it('shows the core reported state, not the state it asked for', async () => {
    // The request succeeded but the pipeline says it is recording. Trusting the
    // request rather than the reply is how an indicator drifts out of step.
    vi.mocked(api.startListening).mockResolvedValue({
      ok: true,
      value: { listening: true, state: 'recording' },
    });

    await useStore.getState().toggleVoice();

    expect(useStore.getState().voice).toBe('recording');
  });

  it('stops when already listening', async () => {
    useStore.setState({ voice: 'listening' });
    vi.mocked(api.stopListening).mockResolvedValue({
      ok: true,
      value: { listening: false, state: 'off' },
    });
    vi.mocked(api.voiceStatus).mockResolvedValue({
      ok: true,
      value: {
        ready: true,
        state: 'off',
        reason: '',
        components: [],
        missing: [],
        micScopeGranted: true,
        microphone: { available: true, detail: '', listening: false, framesSeen: 0, framesDropped: 0 },
      },
    });

    await useStore.getState().toggleVoice();

    expect(api.stopListening).toHaveBeenCalled();
    expect(api.startListening).not.toHaveBeenCalled();
    expect(useStore.getState().voice).toBe('off');
  });

  it('stops rather than starts while speaking, so it can be cut off', async () => {
    useStore.setState({ voice: 'speaking' });
    vi.mocked(api.stopListening).mockResolvedValue({
      ok: true,
      value: { listening: false, state: 'off' },
    });
    vi.mocked(api.voiceStatus).mockResolvedValue({ ok: false, message: 'x', reason: undefined } as never);

    await useStore.getState().toggleVoice();

    expect(api.stopListening).toHaveBeenCalled();
  });
});

describe('refusals', () => {
  it('does not start without the microphone permission, and says where to grant it', async () => {
    useStore.setState({ micScopeGranted: false });

    await useStore.getState().toggleVoice();

    expect(api.startListening).not.toHaveBeenCalled();
    const notice = useStore.getState().messages.at(-1);
    expect(notice?.notice).toBe(true);
    expect(notice?.content).toMatch(/Permissions/);
    expect(useStore.getState().activity.at(0)?.status).toBe('blocked');
  });

  it('never grants the microphone permission on its own', async () => {
    useStore.setState({ micScopeGranted: false });
    const grant = vi.spyOn(api, 'grantScope');

    await useStore.getState().toggleVoice();

    // Clicking the button that uses the microphone is not consent to open it
    // indefinitely. The permission is given once, deliberately, elsewhere.
    expect(grant).not.toHaveBeenCalled();
  });

  it('reports the reason when there is no microphone', async () => {
    useStore.setState({ voiceMic: { ...READY, available: false, detail: 'No microphone is connected.' } });

    await useStore.getState().toggleVoice();

    expect(api.startListening).not.toHaveBeenCalled();
    expect(useStore.getState().messages.at(-1)?.content).toBe('No microphone is connected.');
  });

  it('names the missing model when voice is unavailable', async () => {
    useStore.setState({
      voice: 'unavailable',
      voiceReason: 'Voice is not ready: wake-word (Not downloaded yet (4 MB)).',
    });

    await useStore.getState().toggleVoice();

    expect(api.startListening).not.toHaveBeenCalled();
    expect(useStore.getState().messages.at(-1)?.content).toMatch(/4 MB/);
  });

  it('surfaces a refusal from the core rather than claiming to listen', async () => {
    vi.mocked(api.startListening).mockResolvedValue({
      ok: false,
      message: "Listening needs the 'mic.listen' permission, which is not granted.",
      reason: undefined,
    } as never);
    vi.mocked(api.voiceStatus).mockResolvedValue({ ok: false, message: 'x' } as never);

    await useStore.getState().toggleVoice();

    expect(useStore.getState().voice).not.toBe('listening');
    expect(useStore.getState().messages.at(-1)?.content).toMatch(/mic\.listen/);
  });
});

describe('pipeline events drive the indicator', () => {
  it('follows voice.state', () => {
    useStore.getState().handleEvent({ type: 'voice.state', state: 'recording' });
    expect(useStore.getState().voice).toBe('recording');
  });

  it("shows what it heard as the user's own message", () => {
    useStore.getState().handleEvent({
      type: 'voice.transcript',
      text: 'open chrome',
      confidence: 0.9,
    });
    const last = useStore.getState().messages.at(-1);
    expect(last?.role).toBe('user');
    expect(last?.content).toBe('open chrome');
    expect(useStore.getState().lastHeard).toBe('open chrome');
  });

  it('ignores an empty transcript instead of posting a blank turn', () => {
    useStore.getState().handleEvent({ type: 'voice.transcript', text: '   ' });
    expect(useStore.getState().messages).toHaveLength(0);
  });

  it('logs a wake', () => {
    useStore.getState().handleEvent({ type: 'voice.wake', word: 'hey_jarvis' });
    expect(useStore.getState().activity.at(0)?.summary).toMatch(/wake word/i);
  });

  it('reports a capture error as a notice', () => {
    useStore.getState().handleEvent({ type: 'voice.error', detail: 'the microphone was unplugged' });
    expect(useStore.getState().messages.at(-1)?.content).toBe('the microphone was unplugged');
    expect(useStore.getState().activity.at(0)?.status).toBe('failed');
  });
});

describe('the emergency stop', () => {
  it('turns the indicator off and marks the microphone closed', async () => {
    useStore.setState({ voice: 'listening', voiceMic: { ...READY, listening: true } });

    await useStore.getState().triggerEmergencyStop();

    expect(useStore.getState().voice).toBe('off');
    expect(useStore.getState().voiceMic.listening).toBe(false);
  });
});
