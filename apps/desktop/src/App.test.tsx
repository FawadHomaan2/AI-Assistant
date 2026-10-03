import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { useStore } from '@/state/store';

/**
 * No desktop shell under test. Mocked so the status panel's mount-time refresh
 * settles deterministically rather than racing each assertion.
 */
vi.mock('@/lib/bridge', () => ({
  hasShell: () => false,
  getSystemSnapshot: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'system_snapshot' },
  }),
  getShellInfo: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'shell_info' },
  }),
  setGlobalShortcut: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'set_global_shortcut' },
  }),
  emergencyStop: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'emergency_stop' },
  }),
  hideWindow: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'hide_to_tray' },
  }),
  listen: vi.fn().mockResolvedValue(() => {}),
  getCoreEndpoint: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'core_endpoint' },
  }),
  getCoreStatus: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'core_status' },
  }),
  restartCore: vi.fn().mockResolvedValue({
    ok: false,
    reason: { kind: 'no-bridge', what: 'restart_core' },
  }),
}));

beforeEach(() => {
  useStore.setState({
    view: 'chat',
    messages: [],
    activity: [],
    consent: null,
    mode: 'guarded',
    stopped: false,
    system: { state: 'loading' },
  });
});

/**
 * Renders the app and flushes the status panel's mount-time async refresh, so
 * state settles inside act() instead of landing after the assertions.
 */
async function renderApp() {
  const utils = render(<App />);
  await act(async () => {
    await Promise.resolve();
  });
  return utils;
}

describe('App shell', () => {
  it('renders navigation and the chat view by default', async () => {
    await renderApp();
    expect(screen.getByRole('navigation', { name: /main navigation/i })).toBeTruthy();
    expect(screen.getByLabelText('Message Jarvis')).toBeTruthy();
  });

  it('navigates between all five views', async () => {
    const user = userEvent.setup();
    await renderApp();
    for (const [label, heading] of [
      ['Security', 'Security Center'],
      ['Activity', 'Activity'],
      ['Privacy', 'Privacy'],
      ['Settings', 'Settings'],
    ] as const) {
      await user.click(screen.getByRole('button', { name: label }));
      expect(screen.getByRole('heading', { level: 1, name: heading })).toBeTruthy();
    }
    await user.click(screen.getByRole('button', { name: 'Assistant' }));
    expect(screen.getByLabelText('Message Jarvis')).toBeTruthy();
  });

  it('sends on Enter and explains that no core is reachable', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.type(screen.getByLabelText('Message Jarvis'), 'Open Chrome{Enter}');
    // Scope to the conversation: the activity log echoes the same text.
    const log = within(screen.getByRole('log', { name: /conversation/i }));
    expect(log.getByText('Open Chrome')).toBeTruthy();
    // No shell under test means no core, so the UI must say so rather than
    // invent a reply.
    expect(log.getByText(/core isn't available|Still connecting/i)).toBeTruthy();
  });

  it('Shift+Enter inserts a newline instead of sending', async () => {
    const user = userEvent.setup();
    await renderApp();
    const input = screen.getByLabelText('Message Jarvis') as HTMLTextAreaElement;
    await user.type(input, 'line one{Shift>}{Enter}{/Shift}line two');
    expect(input.value).toContain('\n');
    expect(useStore.getState().messages).toHaveLength(0);
  });

  it('the emergency stop banner appears and clears', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole('button', { name: /stop all actions/i }));
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(useStore.getState().stopped).toBe(true);
    await user.click(screen.getByRole('button', { name: /clear and resume/i }));
    expect(useStore.getState().stopped).toBe(false);
  });

  it('Ctrl+Shift+Escape triggers the emergency stop', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard('{Control>}{Shift>}{Escape}{/Shift}{/Control}');
    expect(useStore.getState().stopped).toBe(true);
  });

  it('a gated quick action names the phase it needs rather than acting', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole('button', { name: /Screenshot/ }));
    expect(screen.getByText(/needs Phase 5/i)).toBeTruthy();
    expect(useStore.getState().activity.at(-1)?.status).toBe('blocked');
  });

  it('an available quick action pre-fills the composer', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole('button', { name: /Search Files/ }));
    const input = screen.getByLabelText('Message Jarvis') as HTMLTextAreaElement;
    expect(input.value).toContain('find my pdf files');
    // Nothing was sent: the user edits first.
    expect(useStore.getState().messages.filter((m) => m.role === 'user')).toHaveLength(0);
  });

  it('exposes the consent dialog from Settings for review', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole('button', { name: 'Settings' }));
    await user.click(screen.getByRole('button', { name: /preview a critical prompt/i }));
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('the security view claims no status it has not checked', async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole('button', { name: 'Security' }));
    expect(screen.getByText(/Not implemented — Phase 9/)).toBeTruthy();
    expect(screen.queryByText(/you are protected|no threats found/i)).toBeNull();
  });
});
