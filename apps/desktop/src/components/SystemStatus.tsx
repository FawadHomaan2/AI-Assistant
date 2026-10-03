import { useEffect } from 'react';
import { Icon, type IconName } from './Icon';
import { StatusBadge } from './StatusBadge';
import { useStore, PHASE } from '@/state/store';
import type { Unavailable } from '@/types';
import './SystemStatus.css';

const REFRESH_MS = 2000;

export function formatBytes(n: number | null): string {
  if (n === null || !Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function formatUptime(seconds: number | null): string {
  if (seconds === null || seconds < 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function percent(used: number | null, total: number | null): number | null {
  if (used === null || total === null || total <= 0) return null;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

function explain(reason: Unavailable): string {
  switch (reason.kind) {
    case 'no-bridge':
      return 'Live metrics come from the desktop shell. Running in a plain browser, so there is nothing to read — numbers are hidden rather than faked.';
    case 'not-implemented':
      return `Not implemented yet — arrives in Phase ${reason.phase} (${reason.what}).`;
    case 'error':
      return `Could not read system metrics: ${reason.message}`;
  }
}

/**
 * Load bands for the meters. Deliberately conservative: high utilisation is
 * normal on a working machine, so this flags "worth a look", never "broken",
 * and the number itself is always shown next to it.
 */
function band(pct: number | null): 'ok' | 'busy' | 'saturated' {
  if (pct === null) return 'ok';
  if (pct >= 90) return 'saturated';
  if (pct >= 75) return 'busy';
  return 'ok';
}

/** A labelled meter. `value === null` renders an explicit em-dash, never 0%. */
function Meter({
  icon,
  label,
  value,
  caption,
}: {
  icon: IconName;
  label: string;
  value: number | null;
  caption: string;
}) {
  const pct = value === null ? null : Math.round(value);
  return (
    <div className={`meter meter--${band(pct)}`}>
      <div className="meter__top">
        <span className="meter__label">
          <Icon name={icon} size={13} />
          {label}
        </span>
        <span className="meter__value">{pct === null ? '—' : `${pct}%`}</span>
      </div>
      <div
        className="meter__track"
        role="progressbar"
        aria-label={label}
        aria-valuenow={pct ?? undefined}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={pct === null ? 'unavailable' : `${pct} percent`}
      >
        <div className="meter__fill" style={{ width: `${pct ?? 0}%` }} />
      </div>
      <span className="meter__caption">{caption}</span>
    </div>
  );
}

export function SystemStatus() {
  const system = useStore((s) => s.system);
  const refreshSystem = useStore((s) => s.refreshSystem);

  useEffect(() => {
    void refreshSystem();
    const t = setInterval(() => void refreshSystem(), REFRESH_MS);
    return () => clearInterval(t);
  }, [refreshSystem]);

  return (
    <section className="status" aria-label="Computer status">
      <header className="status__head">
        <h2>Computer</h2>
        {system.state === 'ready' && <StatusBadge label="Live" kind="ok" tone="accent" />}
        {system.state === 'loading' && <StatusBadge label="Reading…" kind="pending" />}
        {system.state === 'unavailable' && <StatusBadge label="No data" kind="blocked" />}
      </header>

      {system.state === 'unavailable' && <p className="status__note">{explain(system.reason)}</p>}

      {system.state === 'loading' && <p className="status__note">Reading machine state…</p>}

      {system.state === 'ready' &&
        (() => {
          const s = system.value;
          return (
            <>
              <div className="status__grid">
                <Meter
                  icon="cpu"
                  label="CPU"
                  value={s.cpuPercent}
                  caption={s.processCount === null ? '—' : `${s.processCount} processes`}
                />
                <Meter
                  icon="memory"
                  label="Memory"
                  value={percent(s.memUsedBytes, s.memTotalBytes)}
                  caption={`${formatBytes(s.memUsedBytes)} / ${formatBytes(s.memTotalBytes)}`}
                />
                <Meter
                  icon="disk"
                  label="Disk"
                  value={percent(s.diskUsedBytes, s.diskTotalBytes)}
                  caption={`${formatBytes(s.diskUsedBytes)} / ${formatBytes(s.diskTotalBytes)}`}
                />
              </div>

              <dl className="status__facts">
                <div>
                  <dt>
                    <Icon name="network" size={12} /> Network
                  </dt>
                  <dd>
                    {s.netRxBytes === null
                      ? '—'
                      : `↓ ${formatBytes(s.netRxBytes)} · ↑ ${formatBytes(s.netTxBytes)}`}
                  </dd>
                </div>
                <div>
                  <dt>
                    <Icon name="battery" size={12} /> Battery
                  </dt>
                  {/* null covers both "desktop, no battery" and "could not read",
                      so the tooltip says so rather than asserting one of them. */}
                  <dd title={s.batteryPercent === null ? 'No battery, or the platform did not report one' : undefined}>
                    {s.batteryPercent === null
                      ? '—'
                      : `${Math.round(s.batteryPercent)}%${s.batteryCharging ? ' · charging' : ''}`}
                  </dd>
                </div>
                <div>
                  <dt>
                    <Icon name="clock" size={12} /> Uptime
                  </dt>
                  <dd>{formatUptime(s.uptimeSeconds)}</dd>
                </div>
                <div>
                  <dt>
                    <Icon name="info" size={12} /> System
                  </dt>
                  <dd title={s.hostname ?? undefined}>{s.osName ?? '—'}</dd>
                </div>
              </dl>
            </>
          );
        })()}

      <div className="status__security">
        <span className="status__security-label">
          <Icon name="shield" size={13} /> Security
        </span>
        <StatusBadge
          label={`Phase ${PHASE.security}`}
          kind="blocked"
          tone="muted"
          title="Defender, firewall, startup and network checks are not implemented in this build"
        />
      </div>
      <p className="status__note status__note--tight">
        Security monitoring is not implemented yet. This panel will not show a
        status until the checks behind it are real.
      </p>
    </section>
  );
}
