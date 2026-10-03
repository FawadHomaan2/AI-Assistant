import { useCallback, useEffect, useState } from 'react';
import { Page, Explainer, Card, NotImplemented } from './Page';
import { StatusBadge } from '@/components/StatusBadge';
import { useStore, PHASE } from '@/state/store';
import {
  browserStatus,
  memoryClear,
  memoryEdit,
  memoryForget,
  memoryList,
  type BrowserStatus,
  type MemoryRow,
  type MemoryStats,
} from '@/lib/api';
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
  // Read from the core rather than restating the intended default: the config
  // file is editable, and a dashboard that shows the wrong allowlist is worse
  // than one that shows none.
  const [browser, setBrowser] = useState<BrowserStatus | null>(null);
  const [memories, setMemories] = useState<MemoryRow[] | null>(null);
  const [memoryStats, setMemoryStats] = useState<MemoryStats | null>(null);
  const [memoryQuery, setMemoryQuery] = useState('');

  useEffect(() => {
    let live = true;
    browserStatus().then((res) => {
      if (live && res.ok) setBrowser(res.value);
    });
    return () => {
      live = false;
    };
  }, []);

  const loadMemory = useCallback(async (query: string) => {
    const res = await memoryList(query);
    if (!res.ok) {
      // An empty list and a core that cannot be reached look identical to a
      // reader, so the failure is shown rather than rendered as "nothing".
      setMemories([]);
      setMemoryStats(null);
      return;
    }
    setMemories(res.value.memories);
    setMemoryStats(res.value.stats);
  }, []);

  useEffect(() => {
    void loadMemory(memoryQuery);
  }, [loadMemory, memoryQuery]);

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

      <Card title="What Jarvis has learned">
        <p className="card__note">
          Everything Jarvis believes about you, where it came from, and how sure
          it is. A <em>candidate</em> has been noticed but is not acted on —
          Jarvis needs {memoryStats?.promotionThreshold ?? 3} consistent
          observations, or for you to say it outright, before it changes how it
          behaves. Deleting here is permanent; there is no archive.
        </p>
        <div className="memory__controls">
          <input
            type="search"
            className="memory__search"
            placeholder="Search what Jarvis remembers"
            aria-label="Search memory"
            value={memoryQuery}
            onChange={(e) => setMemoryQuery(e.target.value)}
          />
          <button
            type="button"
            className="linkbtn"
            disabled={!memories || memories.length === 0}
            onClick={async () => {
              await memoryClear();
              void loadMemory(memoryQuery);
            }}
          >
            Forget everything
          </button>
        </div>
        {memories === null ? (
          <p className="card__note">Asking the core…</p>
        ) : memories.length === 0 ? (
          <p className="card__note">
            {memoryQuery
              ? `Nothing stored matches “${memoryQuery}”.`
              : 'Nothing stored yet. Tell Jarvis something like "always open PDFs in Acrobat".'}
          </p>
        ) : (
          <ul className="memory">
            {memories.map((m) => (
              <li key={m.id} className="memory__row">
                <div className="memory__main">
                  <code className="memory__key">{m.key}</code>
                  <span className="memory__value">{String(m.value)}</span>
                </div>
                <div className="memory__meta">
                  <StatusBadge
                    label={m.status === 'active' ? 'Active' : 'Candidate'}
                    kind={m.status === 'active' ? 'ok' : 'pending'}
                    tone={m.status === 'active' ? 'accent' : 'muted'}
                    title={
                      m.status === 'active'
                        ? 'Jarvis acts on this'
                        : `Seen ${m.observationCount} time(s); not acted on yet`
                    }
                  />
                  <span className="memory__note">
                    {m.source === 'stated' ? 'you said this' : 'Jarvis noticed this'} ·{' '}
                    {Math.round(m.confidence * 100)}% sure
                  </span>
                  <button
                    type="button"
                    className="linkbtn"
                    aria-label={`Pin ${m.key}`}
                    onClick={async () => {
                      await memoryEdit(m.id, { pinned: !m.pinned });
                      void loadMemory(memoryQuery);
                    }}
                  >
                    {m.pinned ? 'Unpin' : 'Pin'}
                  </button>
                  <button
                    type="button"
                    className="linkbtn"
                    aria-label={`Forget ${m.key}`}
                    onClick={async () => {
                      await memoryForget(m.id);
                      void loadMemory(memoryQuery);
                    }}
                  >
                    Forget
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {memoryStats && (
          <p className="card__note">
            {memoryStats.total} stored. {memoryStats.embedderDetail}
          </p>
        )}
      </Card>

      <Card title="Web browsing">
        <p className="card__note">
          Jarvis drives its own browser profile, never the one you are signed in
          to. A page it reads cannot act as you, and it can only visit the sites
          listed below — that list is what stops a web page from telling Jarvis
          to go somewhere else and paste what it just read.
        </p>
        {browser === null ? (
          <p className="card__note">Asking the core…</p>
        ) : (
          <ul className="datalist">
            <li>
              <span>Browser engine</span>
              <span className="datalist__v">
                <StatusBadge
                  label={browser.available ? 'Installed' : 'Missing'}
                  kind={browser.available ? 'ok' : 'blocked'}
                  title={browser.detail}
                />
              </span>
            </li>
            <li>
              <span>Sites Jarvis may visit</span>
              <span className="datalist__v">
                {browser.allowAnyHost ? 'Any site' : browser.allowedHosts.join(', ') || 'None'}
              </span>
              <span className="datalist__note">
                {browser.allowAnyHost ? 'Allowlist off' : 'Allowlist on'}
              </span>
            </li>
            <li>
              <span>This machine and your network</span>
              <span className="datalist__v">
                {browser.allowLoopback ? 'localhost allowed' : 'Blocked'}
              </span>
              <span className="datalist__note">Local network always blocked</span>
            </li>
            <li>
              <span>Search engine</span>
              <span className="datalist__v">{browser.searchEngine || '—'}</span>
              <span className="datalist__note">Sees what you search for</span>
            </li>
            <li>
              <span>Page currently open</span>
              <span className="datalist__v">{browser.currentUrl || 'None'}</span>
            </li>
          </ul>
        )}
        <p className="card__note">
          Editing the list here arrives with the permission system in Phase{' '}
          {PHASE.permissions}. Until then it is read from config.toml, and this
          card shows what the core actually loaded.
        </p>
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
            <span className="datalist__v">{memoryStats?.total ?? '—'}</span>
            <span className="datalist__note">listed above</span>
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
          'Microphone, cloud AI, file, browser and terminal master switches',
          'Pause all automation',
        ]}
      />
    </Page>
  );
}
