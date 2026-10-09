import { Icon, type IconName } from './Icon';
import { useStore } from '@/state/store';
import type { AssistantMode, ViewId } from '@/types';
import './Sidebar.css';

const NAV: { id: ViewId; label: string; icon: IconName }[] = [
  { id: 'chat', label: 'Assistant', icon: 'chat' },
  { id: 'security', label: 'Security', icon: 'shield' },
  { id: 'activity', label: 'Activity', icon: 'list' },
  { id: 'privacy', label: 'Privacy', icon: 'lock' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
];

const MODES: { id: AssistantMode; label: string; hint: string }[] = [
  { id: 'paused', label: 'Paused', hint: 'Nothing executes. Chat and explanations still work.' },
  { id: 'guarded', label: 'Guarded', hint: 'Safe and low-risk actions run; anything riskier asks first.' },
  { id: 'assisted', label: 'Assisted', hint: 'Medium-risk actions run inside granted scopes; high-risk asks first.' },
  { id: 'developer', label: 'Developer', hint: 'Adds allowlisted shell and build commands in project folders.' },
];

export function Sidebar() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const mode = useStore((s) => s.mode);
  const setMode = useStore((s) => s.setMode);
  const stopped = useStore((s) => s.stopped);
  // The version, not an internal phase number: "Phase 12" meant nothing to
  // anyone running the app, and stayed at 12 across three releases.
  const core = useStore((s) => s.core);
  const version = core.state === 'ready' ? core.health.version : '';

  return (
    <nav className="sidebar" aria-label="Main navigation">
      <div className="sidebar__brand">
        <span className="sidebar__mark" aria-hidden="true">J</span>
        <span className="sidebar__name">
          Jarvis
          <small>{version !== '' ? `v${version}` : 'starting…'}</small>
        </span>
      </div>

      <ul className="sidebar__nav">
        {NAV.map((item) => (
          <li key={item.id}>
            <button
              type="button"
              className={`sidebar__link${view === item.id ? ' is-active' : ''}`}
              aria-current={view === item.id ? 'page' : undefined}
              onClick={() => setView(item.id)}
            >
              <Icon name={item.icon} size={17} />
              <span>{item.label}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="sidebar__modes">
        <label className="sidebar__label" htmlFor="mode-select">
          Operating mode
        </label>
        <select
          id="mode-select"
          className="sidebar__select"
          value={mode}
          disabled={stopped}
          onChange={(e) => setMode(e.target.value as AssistantMode)}
        >
          {MODES.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label}
            </option>
          ))}
        </select>
        <p className="sidebar__hint">{MODES.find((m) => m.id === mode)?.hint}</p>
      </div>
    </nav>
  );
}
