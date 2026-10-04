import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore } from './store';
import * as bridge from '@/lib/bridge';
import * as api from '@/lib/api';

/**
 * The emergency stop, and the loop that made it unusable.
 *
 * The shell emits `jarvis://emergency-stop` when it latches, and the interface
 * was wired to respond by engaging the stop — which invoked the shell, which
 * emitted again. One button press became an unbounded run of IPC calls, HTTP
 * posts and chat messages, and the window stopped responding. Resuming then
 * cleared only the core's stop and left the shell's process-global latch set,
 * so the only way back was killing the application.
 *
 * Both halves are pinned here because either one alone brings the bug back.
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
  clearEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: null }),
  emergencyStopState: vi.fn().mockResolvedValue({ ok: true, value: false }),
  listen: vi.fn().mockResolvedValue(() => {}),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    engageEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: { engaged: true } }),
    clearEmergencyStop: vi.fn().mockResolvedValue({ ok: true, value: { engaged: false } }),
    voiceStatus: vi.fn().mockResolvedValue({ ok: false, message: 'no core' }),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  // `clearAllMocks` resets calls but keeps implementations, so an override in
  // one case leaked into the next and a clean resume looked like a failed one.
  vi.mocked(bridge.emergencyStop).mockResolvedValue({ ok: true, value: null });
  vi.mocked(bridge.clearEmergencyStop).mockResolvedValue({ ok: true, value: null });
  vi.mocked(api.engageEmergencyStop).mockResolvedValue({ ok: true, value: { engaged: true } });
  vi.mocked(api.clearEmergencyStop).mockResolvedValue({ ok: true, value: { engaged: false } });
  useStore.setState({
    stopped: false,
    mode: 'guarded',
    busy: false,
    streamingId: null,
    messages: [],
    activity: [],
  });
});

describe('the event path never calls back into the shell', () => {
  it('applyEmergencyStop stops locally and invokes nothing', () => {
    useStore.getState().applyEmergencyStop();

    expect(useStore.getState().stopped).toBe(true);
    expect(useStore.getState().mode).toBe('paused');
    // This is the whole fix. The shell is what emitted the event that calls
    // this, so calling the shell back is what closed the loop.
    expect(bridge.emergencyStop).not.toHaveBeenCalled();
    expect(api.engageEmergencyStop).not.toHaveBeenCalled();
  });

  it('is idempotent, so a repeated event cannot pile up messages', () => {
    const store = useStore.getState();
    store.applyEmergencyStop();
    store.applyEmergencyStop();
    store.applyEmergencyStop();

    // One notice and one activity row, not three. Each turn of the old loop
    // appended both, which is what flooded the interface.
    expect(useStore.getState().messages).toHaveLength(1);
    expect(useStore.getState().activity.filter((a) => /EMERGENCY STOP/.test(a.summary))).toHaveLength(
      1,
    );
  });

  it('clears in-flight work so the interface cannot be left busy', () => {
    useStore.setState({ busy: true, streamingId: 'abc' });
    useStore.getState().applyEmergencyStop();

    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().streamingId).toBeNull();
  });
});

describe('the user path latches both sides', () => {
  it('triggerEmergencyStop tells the shell and the core exactly once', async () => {
    await useStore.getState().triggerEmergencyStop();

    expect(useStore.getState().stopped).toBe(true);
    expect(bridge.emergencyStop).toHaveBeenCalledTimes(1);
    expect(api.engageEmergencyStop).toHaveBeenCalledTimes(1);
  });
});

describe('resume', () => {
  it('clears the shell latch as well as the core', async () => {
    useStore.getState().applyEmergencyStop();

    await useStore.getState().resume();

    // The shell's latch is a process-global flag. Clearing only the core left
    // it set, and nothing but a restart reset it.
    expect(bridge.clearEmergencyStop).toHaveBeenCalledTimes(1);
    expect(api.clearEmergencyStop).toHaveBeenCalledTimes(1);
    expect(useStore.getState().stopped).toBe(false);
    expect(useStore.getState().mode).toBe('guarded');
  });

  it('still resumes when the core cannot be reached, rather than trapping', async () => {
    vi.mocked(api.clearEmergencyStop).mockResolvedValue({
      ok: false,
      message: 'core unreachable',
    } as never);
    useStore.getState().applyEmergencyStop();

    await useStore.getState().resume();

    // Refusing to leave the stopped state because a call failed is the trap
    // this fix removes: it leaves no way back but restarting. A core that
    // cannot be reached also cannot run anything.
    expect(useStore.getState().stopped).toBe(false);
    expect(useStore.getState().messages.at(-1)?.content).toMatch(/did not confirm/i);
    expect(useStore.getState().activity.at(-1)?.status).toBe('blocked');
  });

  it('says so when the shell latch does not clear', async () => {
    vi.mocked(bridge.clearEmergencyStop).mockResolvedValue({
      ok: false,
      reason: { kind: 'no-bridge', what: 'clear_emergency_stop' },
    } as never);
    useStore.getState().applyEmergencyStop();

    await useStore.getState().resume();

    expect(useStore.getState().stopped).toBe(false);
    expect(useStore.getState().messages.at(-1)?.content).toMatch(/desktop shell/i);
  });

  it('reports a clean resume without a notice', async () => {
    useStore.getState().applyEmergencyStop();
    const before = useStore.getState().messages.length;

    await useStore.getState().resume();

    expect(useStore.getState().messages).toHaveLength(before);
    expect(useStore.getState().activity.at(-1)?.status).toBe('succeeded');
  });

  it('leaves nothing busy, so a stop mid-turn does not strand the interface', async () => {
    useStore.setState({ stopped: true, busy: true, streamingId: 'xyz' });

    await useStore.getState().resume();

    expect(useStore.getState().busy).toBe(false);
    expect(useStore.getState().streamingId).toBeNull();
  });
});
