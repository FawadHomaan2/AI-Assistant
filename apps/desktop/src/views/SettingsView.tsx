import { useCallback, useEffect, useState } from 'react';
import { Page, Explainer, Card } from './Page';
import { StatusBadge } from '@/components/StatusBadge';
import { useStore, CURRENT_PHASE } from '@/state/store';
import { getShellInfo, hasShell, setGlobalShortcut } from '@/lib/bridge';
import { listPlugins, setPluginEnabled, type PluginRow } from '@/lib/api';
import './views.css';

type Provider = 'local' | 'cloud' | 'custom';

const PROVIDERS: { id: Provider; label: string; detail: string }[] = [
  { id: 'local', label: 'Local model', detail: 'llama.cpp (GGUF) or Ollama on this machine. Nothing leaves the computer.' },
  { id: 'cloud', label: 'Cloud API', detail: 'Anthropic or OpenAI. Stronger planning; prompts leave the machine.' },
  { id: 'custom', label: 'Custom OpenAI-compatible endpoint', detail: 'LM Studio, vLLM, OpenRouter, Azure, or any OpenAI-shaped base URL.' },
];

export function SettingsView() {
  const [provider, setProvider] = useState<Provider>('local');
  const [shortcut, setShortcut] = useState('Ctrl+Space');
  const [shortcutNote, setShortcutNote] = useState<string | null>(null);
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');
  const [shell, setShell] = useState<string>('—');
  const requestConsent = useStore((s) => s.requestConsent);
  const providers = useStore((s) => s.providers);
  const core = useStore((s) => s.core);
  const [plugins, setPlugins] = useState<PluginRow[] | null>(null);
  const [pluginDir, setPluginDir] = useState('');

  const loadPlugins = useCallback(async () => {
    const res = await listPlugins();
    if (!res.ok) {
      // An empty list and an unreachable core look identical to a reader, so
      // the failure is left visible rather than rendered as "none installed".
      setPlugins([]);
      return;
    }
    setPlugins(res.value.plugins);
    setPluginDir(res.value.directory);
  }, []);

  useEffect(() => {
    void loadPlugins();
  }, [loadPlugins]);

  useEffect(() => {
    void getShellInfo().then((r) => {
      if (r.ok) setShell(`${r.value.platform} · app ${r.value.version} · tauri ${r.value.tauri}`);
      else setShell('not running in the desktop shell');
    });
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const applyShortcut = async () => {
    const res = await setGlobalShortcut(shortcut);
    setShortcutNote(
      res.ok
        ? `Registered ${res.value}.`
        : res.reason.kind === 'no-bridge'
          ? 'Needs the desktop shell — a browser cannot register a system-wide shortcut.'
          : `Could not register: ${res.reason.kind === 'error' ? res.reason.message : 'unavailable'}`,
    );
  };

  return (
    <Page title="Settings" subtitle={`Jarvis build — Phase ${CURRENT_PHASE}`}>
      <Explainer>
        API keys are never stored in configuration files and never written into
        this page. They go straight into Windows Credential Manager, and the
        field below reads back as "configured", not as the value. Nothing is
        hard-coded.
      </Explainer>

      <Card title="AI provider">
        <div className="radios">
          {PROVIDERS.map((p) => (
            <label key={p.id} className={`radio${provider === p.id ? ' is-on' : ''}`}>
              <input
                type="radio"
                name="provider"
                value={p.id}
                checked={provider === p.id}
                onChange={() => setProvider(p.id)}
              />
              <span className="radio__body">
                <strong>{p.label}</strong>
                <small>{p.detail}</small>
              </span>
            </label>
          ))}
        </div>
        <div className="field">
          <label htmlFor="api-key">
            {provider === 'local' ? 'Model path or Ollama tag' : 'API key'}
          </label>
          <input
            id="api-key"
            type={provider === 'local' ? 'text' : 'password'}
            placeholder={
              provider === 'local'
                ? 'C:\\Users\\you\\models\\qwen2.5-14b-instruct-q4.gguf'
                : 'Stored in Windows Credential Manager'
            }
            autoComplete="off"
            disabled
          />
        </div>
        <p className="card__note">
          Editing providers from this page is not wired up yet — for now, edit{' '}
          <code>config.toml</code> in the Jarvis data folder and restart. Keys go to
          the Windows Credential Manager by name; the file never holds one.
        </p>
      </Card>

      <Card title="Providers the core reports">
        {providers.length === 0 ? (
          <p className="card__note">
            {core.state === 'ready'
              ? 'The core reported no providers.'
              : 'Not connected to the core, so there is nothing to list.'}
          </p>
        ) : (
          <ul className="datalist">
            {providers.map((p) => (
              <li key={p.name}>
                <span>
                  {p.name}
                  <span className="datalist__note"> · {p.model}</span>
                </span>
                <span className="datalist__v">
                  <StatusBadge
                    label={p.isCloud ? 'Cloud' : 'Local'}
                    kind="info"
                    tone={p.isCloud ? 'warning' : 'success'}
                  />
                </span>
                <span className="datalist__v">
                  <StatusBadge
                    label={p.configured ? 'Ready' : 'Needs setup'}
                    kind={p.configured ? 'ok' : 'blocked'}
                  />
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="card__note">
          {providers.find((p) => p.kind === 'dev_echo')
            ? 'The development echo provider is not a language model — it reflects your message back so the pipeline can be exercised without one.'
            : 'Local providers keep every prompt on this machine.'}
        </p>
      </Card>

      <Card title="Global shortcut">
        <div className="field field--row">
          <input
            aria-label="Global shortcut accelerator"
            value={shortcut}
            onChange={(e) => setShortcut(e.target.value)}
            spellCheck={false}
          />
          <button type="button" className="btn btn--primary" onClick={() => void applyShortcut()}>
            Apply
          </button>
        </div>
        {shortcutNote && <p className="card__note">{shortcutNote}</p>}
        <p className="card__note">
          Pressing this anywhere in Windows shows and focuses Jarvis. Emergency stop
          is separately bound to <code>Ctrl+Shift+Esc</code> inside the app.
        </p>
      </Card>

      <Card title="Appearance">
        <div className="field field--row">
          <button
            type="button"
            className={`btn ${theme === 'dark' ? 'btn--primary' : 'btn--ghost'}`}
            onClick={() => setTheme('dark')}
          >
            Dark
          </button>
          <button
            type="button"
            className={`btn ${theme === 'light' ? 'btn--primary' : 'btn--ghost'}`}
            onClick={() => setTheme('light')}
          >
            Light
          </button>
        </div>
      </Card>

      <Card title="Confirmation prompts">
        <p className="card__note">
          The consent gate is built and functional. Nothing destructive exists in
          this build, so you can inspect the real dialog with sample data — it is
          the same component that will gate actual actions.
        </p>
        <div className="field field--row">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() =>
              requestConsent({
                title: 'Move 37 files into 5 new folders',
                summary:
                  'Jarvis wants to sort your Downloads folder by file type, creating 5 folders and moving 37 files into them.',
                risk: 'medium',
                origin: '"Organize my Downloads folder" → plan step 5 of 6',
                targets: [
                  'C:\\Users\\you\\Downloads\\invoice-2026-09.pdf → Documents\\',
                  'C:\\Users\\you\\Downloads\\setup-x64.exe → Installers\\',
                  'C:\\Users\\you\\Downloads\\holiday-01.jpg → Images\\',
                  'C:\\Users\\you\\Downloads\\budget.xlsx → Documents\\',
                ],
                affectedCount: 37,
                reversible: 'undoable',
                blastRadius: 'Files stay inside Downloads; no deletions.',
                allowRemember: true,
              })
            }
          >
            Preview a medium-risk prompt
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() =>
              requestConsent({
                title: 'Permanently delete 37 files',
                summary:
                  'This bypasses the Recycle Bin. The files cannot be recovered by Jarvis or by Windows.',
                risk: 'critical',
                origin: '"Delete the duplicates for good" → plan step 3 of 3',
                targets: [
                  'C:\\Users\\you\\Downloads\\report (1).pdf',
                  'C:\\Users\\you\\Downloads\\report (2).pdf',
                  'C:\\Users\\you\\Pictures\\IMG_0421 - Copy.jpg',
                ],
                affectedCount: 37,
                reversible: 'permanent',
                blastRadius: '37 files across Downloads and Pictures, 2.1 GB total.',
                confirmPhrase: 'DELETE',
                allowRemember: false,
              })
            }
          >
            Preview a critical prompt
          </button>
        </div>
      </Card>

      <Card title="Plugins">
        <p className="card__note">
          Plugins add capabilities. Each runs in its own process with no
          inherited credentials, and can only do what its manifest declares,
          what you approve here, and what Jarvis itself is allowed to do —
          whichever is narrowest. Nothing is enabled until you enable it.
        </p>
        <p className="card__note card__note--warn">
          This is a process boundary, not a sandbox: a plugin runs as you, with
          your file access and your network. Enable one only if you trust whoever
          wrote it.
        </p>
        {plugins === null ? (
          <p className="card__note">Asking the core…</p>
        ) : plugins.length === 0 ? (
          <p className="card__note">
            No plugins installed. Drop a folder containing a <code>plugin.json</code>{' '}
            into <code>{pluginDir || 'the plugins folder'}</code> and it will appear
            here, switched off.
          </p>
        ) : (
          <ul className="plugins">
            {plugins.map((p) => (
              <li key={p.name} className="plugin">
                <div className="plugin__head">
                  <strong className="plugin__name">{p.name}</strong>
                  <span className="plugin__version">v{p.version}</span>
                  <StatusBadge
                    label={p.error ? 'Unusable' : p.enabled ? 'On' : 'Off'}
                    kind={p.error ? 'failed' : p.enabled ? 'ok' : 'blocked'}
                    tone={p.enabled && !p.error ? 'accent' : 'muted'}
                  />
                  {p.running && <StatusBadge label="Running" kind="pending" tone="muted" />}
                </div>
                <p className="plugin__desc">{p.error || p.description}</p>
                <p className="plugin__scopes">
                  {p.scopes.length === 0
                    ? 'Needs no permissions.'
                    : `Asks for: ${p.scopeDetail.map((d) => d.description).join(', ')}.`}
                  {p.enabled && p.scopes.length > 0 && p.effectiveScopes.length < p.scopes.length && (
                    <em> Jarvis does not hold all of these, so the plugin has fewer.</em>
                  )}
                </p>
                {p.needsReapproval && (
                  <p className="plugin__warn">
                    This plugin now asks for more than you approved. It will not run
                    until you enable it again.
                  </p>
                )}
                {!p.error && (
                  <button
                    type="button"
                    className="linkbtn"
                    onClick={async () => {
                      await setPluginEnabled(p.name, !p.enabled);
                      void loadPlugins();
                    }}
                  >
                    {p.enabled
                      ? 'Turn off'
                      : p.scopes.length === 0
                        ? 'Enable — it needs no permissions'
                        : `Enable and allow ${p.scopes.length} permission${
                            p.scopes.length === 1 ? '' : 's'
                          }`}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Build">
        <ul className="datalist">
          <li>
            <span>Desktop shell</span>
            <span className="datalist__v">{shell}</span>
          </li>
          <li>
            <span>AI core</span>
            <span className="datalist__v">
              {core.state === 'ready' ? (
                <StatusBadge label={`v${core.health.version}`} kind="ok" />
              ) : (
                <StatusBadge
                  label={core.state === 'connecting' ? 'Connecting' : 'Unavailable'}
                  kind={core.state === 'connecting' ? 'pending' : 'failed'}
                />
              )}
            </span>
          </li>
          <li>
            <span>Credential store</span>
            <span className="datalist__v">
              {core.state === 'ready' ? (
                <StatusBadge
                  label={core.health.credentialStore.available ? 'Available' : 'Unavailable'}
                  kind={core.health.credentialStore.available ? 'ok' : 'blocked'}
                  title={core.health.credentialStore.detail}
                />
              ) : (
                <span className="datalist__note">—</span>
              )}
            </span>
          </li>
          <li>
            <span>Shell bridge</span>
            <span className="datalist__v">
              <StatusBadge
                label={hasShell() ? 'Connected' : 'Browser (no shell)'}
                kind={hasShell() ? 'ok' : 'blocked'}
                tone={hasShell() ? 'accent' : 'muted'}
              />
            </span>
          </li>
        </ul>
      </Card>
    </Page>
  );
}
