import { Icon } from './Icon';
import { StatusBadge } from './StatusBadge';
import { useStore } from '@/state/store';
import './CoreStatus.css';

/**
 * Connection to the Jarvis core, and which model is behind it.
 *
 * This panel exists because "why did nothing happen?" should always have a
 * visible answer: either the core is up and a provider is named, or the reason
 * it is not is on screen.
 */
export function CoreStatus() {
  const core = useStore((s) => s.core);
  const socketState = useStore((s) => s.socketState);
  const socketDetail = useStore((s) => s.socketDetail);
  const providers = useStore((s) => s.providers);
  const connectCore = useStore((s) => s.connectCore);

  const active = providers.find(
    (p) => core.state === 'ready' && p.name === core.health.defaultProvider,
  );

  return (
    <section className="core" aria-label="Assistant core">
      <header className="core__head">
        <h2>Core</h2>
        {core.state === 'ready' && (
          <StatusBadge
            label={socketState === 'open' ? 'Connected' : 'Starting'}
            kind={socketState === 'open' ? 'ok' : 'pending'}
          />
        )}
        {core.state === 'connecting' && <StatusBadge label="Connecting" kind="pending" />}
        {core.state === 'failed' && <StatusBadge label="Unavailable" kind="failed" />}
      </header>

      {core.state === 'failed' && (
        <>
          <p className="core__note">{core.message}</p>
          <button type="button" className="core__retry" onClick={() => void connectCore()}>
            <Icon name="play" size={13} /> Try again
          </button>
        </>
      )}

      {core.state === 'connecting' && <p className="core__note">Starting the Jarvis core…</p>}

      {core.state === 'ready' && (
        <>
          <dl className="core__facts">
            <div>
              <dt>Model</dt>
              <dd title={active?.detail}>{active ? active.model : core.health.defaultProvider}</dd>
            </div>
            <div>
              <dt>Data</dt>
              <dd>{active?.isCloud ? 'Sent to cloud' : 'Stays local'}</dd>
            </div>
          </dl>

          {/* The echo provider is not a model. Say so, prominently. */}
          {active?.kind === 'dev_echo' && (
            <p className="core__note core__note--warn">
              No language model is configured, so replies come from a development echo
              — it reflects your message back and is not intelligence. Choose a provider
              in Settings.
            </p>
          )}

          {active && !active.configured && (
            <p className="core__note core__note--warn">{active.detail}</p>
          )}

          {socketState !== 'open' && socketDetail && (
            <p className="core__note">{socketDetail}</p>
          )}
        </>
      )}
    </section>
  );
}
