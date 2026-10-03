import { Icon } from './Icon';
import { useStore } from '@/state/store';
import './EmergencyStop.css';

/** Always-visible kill switch. Also bound to Ctrl+Shift+Escape in App.tsx. */
export function EmergencyStop() {
  const stopped = useStore((s) => s.stopped);
  const trigger = useStore((s) => s.triggerEmergencyStop);

  return (
    <button
      type="button"
      className={`estop${stopped ? ' is-active' : ''}`}
      onClick={() => void trigger()}
      disabled={stopped}
      title="Stop all automation immediately (Ctrl+Shift+Esc)"
    >
      <Icon name="stop" size={14} strokeWidth={2.4} />
      <span>{stopped ? 'STOPPED' : 'STOP ALL ACTIONS'}</span>
    </button>
  );
}

/** Banner shown while the stop is latched, with the only way to clear it. */
export function EmergencyStopBanner() {
  const stopped = useStore((s) => s.stopped);
  const resume = useStore((s) => s.resume);
  if (!stopped) return null;

  return (
    <div className="estop-banner" role="alert">
      <Icon name="alert" size={16} />
      <span className="estop-banner__text">
        <strong>Emergency stop is active.</strong> All automation is halted and Juno
        is paused. Nothing will run until you clear this.
      </span>
      <button type="button" className="estop-banner__resume" onClick={resume}>
        <Icon name="play" size={13} /> Clear and resume
      </button>
    </div>
  );
}
