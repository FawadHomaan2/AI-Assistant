import { Icon } from './Icon';
import { StatusBadge } from './StatusBadge';
import { useStore } from '@/state/store';
import type { ActionStatus } from '@/types';
import './ActivityLog.css';

const KIND: Record<ActionStatus, 'ok' | 'failed' | 'pending' | 'blocked'> = {
  succeeded: 'ok',
  failed: 'failed',
  running: 'pending',
  pending: 'pending',
  cancelled: 'blocked',
  blocked: 'blocked',
};

const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

export function ActivityLog({ limit, showClear = false }: { limit?: number; showClear?: boolean }) {
  const activity = useStore((s) => s.activity);
  const clearActivity = useStore((s) => s.clearActivity);

  // Newest first; the rail shows a window, the Activity view shows everything.
  const rows = [...activity].reverse().slice(0, limit ?? activity.length);

  return (
    <section className="activity" aria-label="Activity log">
      <header className="activity__head">
        <h2>Activity</h2>
        {showClear && activity.length > 0 && (
          <button type="button" className="activity__clear" onClick={clearActivity}>
            <Icon name="trash" size={13} /> Clear
          </button>
        )}
      </header>

      {rows.length === 0 ? (
        <p className="activity__empty">Nothing logged yet.</p>
      ) : (
        <ol className="activity__list">
          {rows.map((e) => (
            <li key={e.id} className="activity__row">
              <time className="activity__time" dateTime={new Date(e.at).toISOString()}>
                {clock(e.at)}
              </time>
              <div className="activity__body">
                <span className="activity__summary">{e.summary}</span>
                {e.detail && <span className="activity__detail" data-selectable>{e.detail}</span>}
              </div>
              <div className="activity__meta">
                {e.controlLayer && <StatusBadge label={e.controlLayer} kind="info" tone="muted" />}
                <StatusBadge
                  label={e.status}
                  kind={KIND[e.status]}
                  tone={e.status === 'failed' ? 'accent' : 'neutral'}
                />
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
