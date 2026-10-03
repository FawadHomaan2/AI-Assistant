import { Page, Explainer, Card, NotImplemented } from './Page';
import { StatusBadge } from '@/components/StatusBadge';
import { useStore, PHASE } from '@/state/store';
import './views.css';

/** Scopes granted by default on install (ARCHITECTURE.md §7, axis 2). */
const DEFAULT_SCOPES: { scope: string; what: string; on: boolean }[] = [
  { scope: 'fs.read', what: 'Desktop, Documents, Downloads, Pictures', on: true },
  { scope: 'fs.write', what: 'Desktop, Documents, Downloads, Pictures', on: true },
  { scope: 'fs.delete', what: 'Recycle Bin only, with confirmation', on: false },
  { scope: 'app.launch', what: 'Start applications', on: true },
  { scope: 'app.control', what: 'Focus, minimise and close windows', on: false },
  { scope: 'process.read', what: 'List running processes', on: true },
  { scope: 'process.kill', what: 'End processes', on: false },
  { scope: 'system.info', what: 'CPU, memory, disk, network counters', on: true },
  { scope: 'system.settings', what: 'Change Windows settings', on: false },
  { scope: 'shell.run', what: 'Run allowlisted commands', on: false },
  { scope: 'shell.powershell', what: 'Run PowerShell', on: false },
  { scope: 'browser.use', what: 'Drive a browser', on: false },
  { scope: 'screen.capture', what: 'Take screenshots', on: false },
  { scope: 'mic.listen', what: 'Use the microphone', on: false },
  { scope: 'cloud.llm', what: 'Send prompts to a cloud AI provider', on: false },
  { scope: 'security.read', what: 'Read security state', on: true },
];

export function PrivacyView() {
  const messages = useStore((s) => s.messages);
  const activity = useStore((s) => s.activity);
  const clearMessages = useStore((s) => s.clearMessages);
  const clearActivity = useStore((s) => s.clearActivity);

  return (
    <Page title="Privacy" subtitle="What Jarvis can reach, what it has stored, and how to revoke it.">
      <Explainer>
        Jarvis is local-first. Nothing leaves this computer unless you turn on a
        cloud provider, and credentials, security findings and the audit log are
        never eligible to leave at all — that is enforced in code, not by a
        setting you could mis-tune.
      </Explainer>

      <Card title="Capability scopes">
        <p className="card__note">
          The grant set below is the intended install default: narrow, and
          everything else off. Toggles become functional with the permission
          system in Phase {PHASE.permissions}.
        </p>
        <ul className="scopes">
          {DEFAULT_SCOPES.map((s) => (
            <li key={s.scope} className="scopes__row">
              <code className="scopes__name">{s.scope}</code>
              <span className="scopes__what">{s.what}</span>
              <StatusBadge
                label={s.on ? 'Granted' : 'Off'}
                kind={s.on ? 'ok' : 'blocked'}
                tone={s.on ? 'accent' : 'muted'}
              />
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Stored data in this session">
        <ul className="datalist">
          <li>
            <span>Conversation messages</span>
            <span className="datalist__v">{messages.length}</span>
            <button type="button" className="linkbtn" onClick={clearMessages}>
              Clear
            </button>
          </li>
          <li>
            <span>Activity entries</span>
            <span className="datalist__v">{activity.length}</span>
            <button type="button" className="linkbtn" onClick={clearActivity}>
              Clear
            </button>
          </li>
          <li>
            <span>Learned memory</span>
            <span className="datalist__v">—</span>
            <span className="datalist__note">Phase {PHASE.memory}</span>
          </li>
          <li>
            <span>Indexed documents</span>
            <span className="datalist__v">—</span>
            <span className="datalist__note">Phase {PHASE.fileTools}</span>
          </li>
        </ul>
        <p className="card__note">
          This build keeps messages and activity in memory only — closing Jarvis
          discards them. Persistence to SQLite arrives with the core in Phase{' '}
          {PHASE.aiCore}, at which point these controls delete rows from disk.
        </p>
      </Card>

      <NotImplemented
        phase={PHASE.permissions}
        what="The full privacy dashboard needs the governance plane behind it."
        items={[
          'Per-folder allow and deny lists with a path jail',
          'Per-tool enable and disable switches',
          'Exactly which data categories a cloud provider has received',
          'Searchable, editable, deletable learned memory',
          'Microphone, cloud AI, file, browser and terminal master switches',
          'Pause all automation',
        ]}
      />
    </Page>
  );
}
