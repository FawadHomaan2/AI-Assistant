import { Icon, type IconName } from './Icon';
import './StatusBadge.css';

/**
 * State indicator.
 *
 * Colour now carries meaning (success / warning / danger / notice), but it is
 * never the only carrier: every badge also has an icon and a text label. That
 * keeps state readable in greyscale, for colour-blind users, and in a
 * screenshot pasted into a bug report.
 */
export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'notice' | 'muted';

export type BadgeKind = 'ok' | 'attention' | 'pending' | 'blocked' | 'failed' | 'info';

const ICONS: Record<BadgeKind, IconName> = {
  ok: 'check',
  attention: 'alert',
  pending: 'clock',
  blocked: 'minus',
  failed: 'x',
  info: 'info',
};

/** Sensible tone for a kind, so callers only override when they mean to. */
const DEFAULT_TONE: Record<BadgeKind, Tone> = {
  ok: 'success',
  attention: 'warning',
  pending: 'neutral',
  blocked: 'muted',
  failed: 'danger',
  info: 'notice',
};

export function StatusBadge({
  label,
  kind = 'info',
  tone,
  title,
}: {
  label: string;
  kind?: BadgeKind;
  tone?: Tone;
  title?: string;
}) {
  const resolved = tone ?? DEFAULT_TONE[kind];
  return (
    <span className={`badge badge--${resolved}`} title={title ?? label}>
      <Icon name={ICONS[kind]} size={12} strokeWidth={2.2} />
      <span>{label}</span>
    </span>
  );
}
