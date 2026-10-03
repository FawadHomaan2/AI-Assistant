import { useEffect, useState } from 'react';
import { Page, Explainer, Card } from './Page';
import { StatusBadge } from '@/components/StatusBadge';
import { useStore, PHASE, CURRENT_PHASE } from '@/state/store';
import { getShellInfo, hasShell, setGlobalShortcut } from '@/lib/bridge';
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
    <Page title="Settings" subtitle={`Juno build — Phase ${CURRENT_PHASE}`}>
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
          Inactive until the provider gateway lands in Phase {PHASE.aiCore}.
          Saving a key here would have nowhere to go yet, so the field is
          disabled rather than silently discarding what you type.
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
          Pressing this anywhere in Windows shows and focuses Juno. Emergency stop
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
                  'Juno wants to sort your Downloads folder by file type, creating 5 folders and moving 37 files into them.',
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
                  'This bypasses the Recycle Bin. The files cannot be recovered by Juno or by Windows.',
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

      <Card title="Build">
        <ul className="datalist">
          <li>
            <span>Desktop shell</span>
            <span className="datalist__v">{shell}</span>
          </li>
          <li>
            <span>AI core</span>
            <span className="datalist__v">
              <StatusBadge label={`Phase ${PHASE.aiCore}`} kind="blocked" tone="muted" />
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
