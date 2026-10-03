import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SystemStatus, formatBytes, formatUptime, percent } from './SystemStatus';
import { useStore } from '@/state/store';
import type { SystemSnapshot } from '@/types';
import type { BridgeResult } from '@/lib/bridge';

/**
 * The bridge is mocked so the mount-time refresh resolves deterministically
 * instead of racing the assertions.
 */
const getSystemSnapshot = vi.hoisted(() => vi.fn());
vi.mock('@/lib/bridge', () => ({
  getSystemSnapshot,
  hasShell: () => false,
}));

const snapshot = (over: Partial<SystemSnapshot> = {}): SystemSnapshot => ({
  cpuPercent: 23.4,
  memUsedBytes: 8 * 1024 ** 3,
  memTotalBytes: 16 * 1024 ** 3,
  diskUsedBytes: 300 * 1024 ** 3,
  diskTotalBytes: 500 * 1024 ** 3,
  netRxBytes: 1024 ** 3,
  netTxBytes: 512 * 1024 ** 2,
  batteryPercent: 88,
  batteryCharging: true,
  processCount: 231,
  hostname: 'desktop',
  osName: 'Windows 11 Pro',
  uptimeSeconds: 93_600,
  capturedAt: Date.now(),
  ...over,
});

const ok = (s: SystemSnapshot): BridgeResult<SystemSnapshot> => ({ ok: true, value: s });
const noBridge = (): BridgeResult<SystemSnapshot> => ({
  ok: false,
  reason: { kind: 'no-bridge', what: 'system_snapshot' },
});

describe('formatters', () => {
  it('formats bytes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(16 * 1024 ** 3)).toBe('16 GB');
  });

  // Null must render as an em-dash, never as 0 — "unknown" is not "zero".
  it('renders unknown values as an em-dash', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatUptime(null)).toBe('—');
    expect(percent(null, 100)).toBeNull();
    expect(percent(50, 0)).toBeNull();
  });

  it('formats uptime', () => {
    expect(formatUptime(93_600)).toBe('1d 2h');
    expect(formatUptime(3_900)).toBe('1h 5m');
    expect(formatUptime(120)).toBe('2m');
  });

  it('computes percentages', () => {
    expect(percent(8, 16)).toBe(50);
  });
});

describe('SystemStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({ system: { state: 'loading' } });
  });

  it('explains the absence of a shell instead of showing zeros', async () => {
    getSystemSnapshot.mockResolvedValue(noBridge());
    render(<SystemStatus />);
    expect(await screen.findByText(/nothing to read/i)).toBeTruthy();
    expect(screen.queryByText('0%')).toBeNull();
  });

  it('surfaces a read error verbatim', async () => {
    getSystemSnapshot.mockResolvedValue({
      ok: false,
      reason: { kind: 'error', message: 'counter unavailable' },
    });
    render(<SystemStatus />);
    expect(await screen.findByText(/counter unavailable/)).toBeTruthy();
  });

  it('renders real metrics from the snapshot', async () => {
    getSystemSnapshot.mockResolvedValue(ok(snapshot()));
    render(<SystemStatus />);
    expect(await screen.findByText('23%')).toBeTruthy();
    expect(screen.getByText('50%')).toBeTruthy();
    expect(screen.getByText('60%')).toBeTruthy();
    expect(screen.getByText('231 processes')).toBeTruthy();
    expect(screen.getByText('88% · charging')).toBeTruthy();
    expect(screen.getByText('Windows 11 Pro')).toBeTruthy();
  });

  it('shows an em-dash for fields the OS did not report', async () => {
    getSystemSnapshot.mockResolvedValue(
      ok(snapshot({ cpuPercent: null, batteryPercent: null, netRxBytes: null, uptimeSeconds: null })),
    );
    render(<SystemStatus />);
    // Unknown renders as an em-dash and says why on hover — it never claims
    // a definite "no battery" when the read simply failed.
    expect(await screen.findByTitle(/No battery, or the platform did not report one/i)).toBeTruthy();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3);
  });

  // A security panel must not imply a clean bill of health it never checked.
  it('declares security monitoring unimplemented and claims no status', async () => {
    getSystemSnapshot.mockResolvedValue(ok(snapshot()));
    render(<SystemStatus />);
    expect(await screen.findByText(/Security monitoring is not implemented yet/i)).toBeTruthy();
    expect(screen.queryByText(/protected|secure|all clear/i)).toBeNull();
  });
});

describe('SystemStatus load bands', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({ system: { state: 'loading' } });
  });

  const memAt = (pct: number) =>
    snapshot({ memTotalBytes: 100, memUsedBytes: pct, cpuPercent: 1, diskUsedBytes: 1, diskTotalBytes: 100 });

  it('marks normal load as ok', async () => {
    getSystemSnapshot.mockResolvedValue(ok(memAt(50)));
    const { container } = render(<SystemStatus />);
    expect(await screen.findByText('50%')).toBeTruthy();
    expect(container.querySelectorAll('.meter--saturated')).toHaveLength(0);
    expect(container.querySelectorAll('.meter--busy')).toHaveLength(0);
  });

  it('flags busy and saturated separately', async () => {
    getSystemSnapshot.mockResolvedValue(ok(memAt(80)));
    const { container, unmount } = render(<SystemStatus />);
    expect(await screen.findByText('80%')).toBeTruthy();
    expect(container.querySelectorAll('.meter--busy')).toHaveLength(1);
    unmount();

    getSystemSnapshot.mockResolvedValue(ok(memAt(95)));
    const second = render(<SystemStatus />);
    expect(await screen.findByText('95%')).toBeTruthy();
    expect(second.container.querySelectorAll('.meter--saturated')).toHaveLength(1);
  });

  // An unknown value must not be styled as though it were healthy *or* alarming.
  it('does not band an unknown value', async () => {
    getSystemSnapshot.mockResolvedValue(ok(snapshot({ cpuPercent: null })));
    const { container } = render(<SystemStatus />);
    await screen.findByText('231 processes');
    const cpuMeter = container.querySelector('.meter');
    expect(cpuMeter?.className).toContain('meter--ok');
  });
});
