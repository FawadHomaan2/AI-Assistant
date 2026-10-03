import { Icon, type IconName } from './Icon';
import { useStore, QUICK_ACTIONS, CURRENT_PHASE } from '@/state/store';
import './QuickActions.css';

const ICONS: Record<string, IconName> = {
  'open-apps': 'app',
  'search-files': 'search',
  screenshot: 'camera',
  'security-scan': 'shield',
  'system-check': 'cpu',
  'clean-downloads': 'broom',
  voice: 'mic',
};

export function QuickActions() {
  const pushMessage = useStore((s) => s.pushMessage);
  const logActivity = useStore((s) => s.logActivity);

  return (
    <section className="quick" aria-label="Quick actions">
      <header className="quick__head">
        <h2>Quick actions</h2>
      </header>
      <div className="quick__grid">
        {QUICK_ACTIONS.map((a) => {
          const ready = a.availableIn <= CURRENT_PHASE;
          return (
            <button
              key={a.id}
              type="button"
              className={`quick__btn${ready ? '' : ' is-pending'}`}
              title={ready ? a.hint : `${a.hint} — needs Phase ${a.availableIn}`}
              onClick={() => {
                // Every action is gated on its phase; none of them pretend to work.
                pushMessage({
                  role: 'system',
                  notice: true,
                  content: `"${a.label}" needs Phase ${a.availableIn}. ${a.hint}. This build is Phase ${CURRENT_PHASE} (interface only), so the button is wired but has no tool behind it.`,
                });
                logActivity({ summary: `Quick action: ${a.label}`, status: 'blocked', detail: `Requires Phase ${a.availableIn}` });
              }}
            >
              <Icon name={ICONS[a.id] ?? 'app'} size={18} />
              <span className="quick__label">{a.label}</span>
              {!ready && <span className="quick__phase">P{a.availableIn}</span>}
            </button>
          );
        })}
      </div>
    </section>
  );
}
