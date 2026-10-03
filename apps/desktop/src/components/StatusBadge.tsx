import { Icon, type IconName } from './Icon';
import './StatusBadge.css';

/**
 * State indicator.
 *
 * The palette has no status colours, so tone only varies emphasis (accent vs
 * muted) and the icon + label always carry the actual meaning. That is a
 * constraint, but it also makes the UI readable for colour-blind users and in
 * greyscale — state is never colour-only.
 */
export type Tone = 'neutral' | 'accent' | 'muted';

const ICONS: Record<string, IconName> = {
  ok: 'check',
  attention: 'alert',
  pending: 'clock',
  blocked: 'minus',
  failed: 'x',
  info: 'info',
};

export function StatusBadge({
  label,
  kind = 'info',
  tone = 'neutral',
  title,
}: {
  label: string;
  kind?: keyof typeof ICONS;
  tone?: Tone;
  title?: string;
}) {
  return (
    <span className={`badge badge--${tone}`} title={title ?? label}>
      <Icon name={ICONS[kind] ?? 'info'} size={12} strokeWidth={2.2} />
      <span>{label}</span>
    </span>
  );
}
