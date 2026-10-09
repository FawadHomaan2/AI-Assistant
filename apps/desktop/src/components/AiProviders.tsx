import { useCallback, useEffect, useState } from 'react';
import { StatusBadge } from './StatusBadge';
import {
  addProvider,
  aiState,
  clearProviderKey,
  providerHealth,
  providerPresets,
  providers as listProviders,
  removeProvider,
  setAiSettings,
  setProviderKey,
  type AiState,
  type ProviderPreset,
} from '@/lib/api';
import type { ProviderInfo } from '@/types';

/**
 * Choosing and configuring the model Jarvis thinks with.
 *
 * This replaced a mock: radio buttons and a disabled "API key" field, under a
 * note saying editing providers "is not wired up yet". The shipped
 * `config.toml` has every cloud service commented out, so an installed copy
 * listed `dev_echo` alone and the adapters' "add a key in Settings" named a
 * control that did not exist.
 *
 * Three things the layout is trying to keep true:
 *
 * **A key is not consent.** Storing one and allowing cloud use are separate
 * switches, because a key kept for later must not start sending conversations
 * off the machine on its own.
 *
 * **The key is write-only.** It goes to the OS credential store and is never
 * read back, so the field is always empty and the state is a badge, not a
 * value.
 *
 * **An unavailable credential store is said out loud.** `keyring` is an
 * optional dependency and the core refuses to fall back to a file. Without it a
 * key cannot be stored at all — which must not look like a key that was saved.
 */
export function AiProviders() {
  const [presets, setPresets] = useState<ProviderPreset[] | null>(null);
  const [rows, setRows] = useState<ProviderInfo[] | null>(null);
  const [ai, setAi] = useState<AiState | null>(null);
  const [health, setHealth] = useState<Record<string, { ok: boolean; detail: string }>>({});
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');

  // Which row has its key field open, and what is in it. Kept out of `rows` so
  // a reload cannot resurrect a typed key.
  const [keyFor, setKeyFor] = useState<string | null>(null);
  const [keyValue, setKeyValue] = useState('');

  const [draft, setDraft] = useState({
    presetId: '',
    name: '',
    kind: '',
    model: '',
    baseUrl: '',
    credential: '',
    key: '',
  });

  const load = useCallback(async () => {
    const [p, r, a] = await Promise.all([providerPresets(), listProviders(), aiState()]);
    if (p.ok) setPresets(p.value);
    if (r.ok) setRows(r.value);
    if (a.ok) setAi(a.value);
    // One message rather than three: any of these failing means the core is
    // unreachable, and saying so once is enough.
    if (!p.ok) setError(p.message);
    else if (!r.ok) setError(r.message);
    else if (!a.ok) setError(a.message);
    else setError('');
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Run an action, then reload. Keeps every button's bookkeeping identical. */
  const run = useCallback(
    async (label: string, action: () => Promise<{ ok: boolean; message?: string }>) => {
      setBusy(label);
      setNote('');
      setError('');
      const res = await action();
      setBusy('');
      if (!res.ok) {
        setError(res.message ?? 'The change did not go through.');
        return false;
      }
      await load();
      return true;
    },
    [load],
  );

  const applyPreset = (id: string) => {
    const preset = presets?.find((p) => p.id === id);
    if (!preset) {
      setDraft({
        presetId: '',
        name: '',
        kind: '',
        model: '',
        baseUrl: '',
        credential: '',
        key: '',
      });
      return;
    }
    setDraft({
      presetId: preset.id,
      name: preset.id,
      kind: preset.kind,
      model: preset.model,
      baseUrl: preset.baseUrl,
      credential: preset.credential,
      key: '',
    });
  };

  const chosen = presets?.find((p) => p.id === draft.presetId) ?? null;
  const storeAvailable = ai?.credentialStore.available ?? false;
  const cloudRows = (rows ?? []).filter((r) => r.isCloud);

  if (error !== '' && rows === null) {
    return <p className="card__note card__note--warn">{error}</p>;
  }

  return (
    <>
      {ai !== null && !storeAvailable ? (
        <p className="card__note card__note--warn">
          No credential store is available ({ai.credentialStore.backend}), so API keys
          cannot be saved. {ai.credentialStore.detail} Providers that need no key —
          Ollama, or a local OpenAI-compatible server — still work.
        </p>
      ) : null}

      {/* A radio rather than a button for the one in use: picking it is picking
          among alternatives, and the current choice should be visible without
          reading every row. */}
      {rows === null ? (
        <p className="card__note">Reading the configuration…</p>
      ) : rows.length === 0 ? (
        <p className="card__note">No providers are configured.</p>
      ) : (
        <ul className="datalist">
          {rows.map((row) => {
            const probe = health[row.name];
            return (
              <li key={row.name}>
                <span>
                  <label className="linkbtn">
                    <input
                      type="radio"
                      name="default-provider"
                      checked={ai?.default === row.name}
                      disabled={busy !== ''}
                      onChange={() =>
                        void run(`default:${row.name}`, () => setAiSettings({ default: row.name }))
                      }
                    />{' '}
                    <strong>{row.name}</strong>
                  </label>
                  <span className="datalist__note">
                    {' '}
                    · {row.kind}
                    {row.model !== '' ? ` · ${row.model}` : ''}
                  </span>
                  {probe ? (
                    <span className="datalist__note">
                      <br />
                      {probe.detail}
                    </span>
                  ) : null}
                </span>
                <span className="datalist__v">
                  <StatusBadge
                    label={row.isCloud ? 'Cloud' : 'Local'}
                    kind="info"
                    tone={row.isCloud ? 'warning' : 'success'}
                  />
                </span>
                <span className="datalist__v">
                  <StatusBadge
                    label={row.configured ? 'Ready' : 'Needs a key'}
                    kind={row.configured ? 'ok' : 'blocked'}
                    title={row.detail}
                  />
                </span>
                <span className="datalist__v">
                  {row.kind === 'dev_echo' ? null : (
                    <button
                      type="button"
                      className="linkbtn"
                      disabled={busy !== '' || !storeAvailable}
                      onClick={() => {
                        setKeyFor(keyFor === row.name ? null : row.name);
                        setKeyValue('');
                      }}
                    >
                      {row.configured ? 'Replace key' : 'Add key'}
                    </button>
                  )}{' '}
                  <button
                    type="button"
                    className="linkbtn"
                    disabled={busy !== ''}
                    onClick={() =>
                      void (async () => {
                        setBusy(`test:${row.name}`);
                        const res = await providerHealth();
                        setBusy('');
                        if (res.ok) setHealth(res.value);
                        else setError(res.message);
                      })()
                    }
                  >
                    Test
                  </button>{' '}
                  {/* Offered for every provider, the echo one included: a setup
                      with a real model does not need it, and the core refuses
                      the one case that must not happen — removing the last
                      provider — with a message worth reading. */}
                  <button
                    type="button"
                    className="linkbtn"
                    disabled={busy !== ''}
                    onClick={() => void run(`remove:${row.name}`, () => removeProvider(row.name))}
                  >
                    Remove
                  </button>
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {keyFor !== null ? (
        <div className="field field--row">
          <input
            aria-label={`API key for ${keyFor}`}
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={`Key for ${keyFor} — stored in the OS credential store`}
            value={keyValue}
            onChange={(e) => setKeyValue(e.target.value)}
          />
          <button
            type="button"
            className="btn btn--primary"
            disabled={busy !== '' || keyValue.trim() === ''}
            onClick={() =>
              void (async () => {
                const name = keyFor;
                if (name === null) return;
                const ok = await run(`key:${name}`, () => setProviderKey(name, keyValue));
                if (ok) {
                  setKeyValue('');
                  setKeyFor(null);
                  setNote(`Key stored for ${name}.`);
                }
              })()
            }
          >
            Save key
          </button>
          <button
            type="button"
            className="linkbtn"
            disabled={busy !== ''}
            onClick={() =>
              void (async () => {
                const name = keyFor;
                if (name === null) return;
                const ok = await run(`unkey:${name}`, () => clearProviderKey(name));
                if (ok) {
                  setKeyValue('');
                  setKeyFor(null);
                  setNote(`Key removed for ${name}.`);
                }
              })()
            }
          >
            Remove stored key
          </button>
        </div>
      ) : null}

      {/* Cloud access. Its own decision, and left off by default: a key stored
          for later must not start sending conversations off the machine. */}
      {ai !== null ? (
        <div className="radios">
          <label className={`radio${ai.allowCloud ? ' is-on' : ''}`}>
            <input
              type="checkbox"
              checked={ai.allowCloud}
              disabled={busy !== ''}
              onChange={(e) =>
                void run('allow-cloud', () => setAiSettings({ allowCloud: e.target.checked }))
              }
            />
            <span className="radio__body">
              <strong>Allow cloud models</strong>
              <small>
                Without this, a configured cloud provider is refused rather than used.
                {cloudRows.length > 0 && !ai.allowCloud
                  ? ' You have one configured, so it is currently being refused.'
                  : ''}
              </small>
            </span>
          </label>
          <label className={`radio${ai.allowCloudContent ? ' is-on' : ''}`}>
            <input
              type="checkbox"
              checked={ai.allowCloudContent}
              disabled={busy !== '' || !ai.allowCloud}
              onChange={(e) =>
                void run('allow-cloud-content', () =>
                  setAiSettings({ allowCloudContent: e.target.checked }),
                )
              }
            />
            <span className="radio__body">
              <strong>Allow document and screen content to reach a cloud model</strong>
              <small>
                Separate on purpose. Off, a cloud model can be asked questions but is
                never sent the text of a document, a screenshot or the clipboard.
              </small>
            </span>
          </label>
        </div>
      ) : null}

      {/* Adding one. The list comes from the core, so a new service is one entry
          there rather than an edit in three places. */}
      <div className="field">
        <label htmlFor="preset">Add a model provider</label>
        <select
          id="preset"
          value={draft.presetId}
          disabled={busy !== '' || presets === null}
          onChange={(e) => applyPreset(e.target.value)}
        >
          <option value="">Choose a service…</option>
          {(presets ?? []).map((preset) => (
            <option key={preset.id} value={preset.id}>
              {preset.label}
              {/* "No key needed" is true of a local model and of a relay that
                  holds someone else's key, and those are opposites as far as
                  privacy goes. Said plainly here, because the dropdown is
                  where the choice is made. */}
              {preset.needsKey
                ? ' — needs an API key'
                : preset.isCloud
                  ? ' — no key, but sends to the cloud'
                  : ' — no key, stays on this machine'}
            </option>
          ))}
        </select>
      </div>

      {chosen !== null ? (
        <>
          {chosen.note !== '' ? <p className="card__note">{chosen.note}</p> : null}
          <div className="field">
            <label htmlFor="draft-name">Name</label>
            <input
              id="draft-name"
              value={draft.name}
              spellCheck={false}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </div>
          <div className="field">
            <label htmlFor="draft-model">Model</label>
            <input
              id="draft-model"
              value={draft.model}
              spellCheck={false}
              placeholder="Leave empty for the adapter's default"
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
            />
          </div>
          {chosen.baseUrl !== '' || !chosen.isCloud ? (
            <div className="field">
              <label htmlFor="draft-url">Base URL</label>
              <input
                id="draft-url"
                value={draft.baseUrl}
                spellCheck={false}
                onChange={(e) => setDraft({ ...draft, baseUrl: e.target.value })}
              />
            </div>
          ) : null}
          {chosen.needsKey ? (
            <div className="field">
              <label htmlFor="draft-key">API key</label>
              <input
                id="draft-key"
                type="password"
                autoComplete="off"
                spellCheck={false}
                disabled={!storeAvailable}
                placeholder={
                  storeAvailable
                    ? 'Goes to the OS credential store, never to a file'
                    : 'No credential store available'
                }
                value={draft.key}
                onChange={(e) => setDraft({ ...draft, key: e.target.value })}
              />
              {chosen.keyUrl !== '' ? (
                // Shown as text, not as a link: opening a URL needs a webview
                // capability this app does not grant, and granting one so a
                // settings page can open a browser is a poor trade.
                <small className="card__note">
                  Keys are issued at <code>{chosen.keyUrl}</code>
                </small>
              ) : null}
            </div>
          ) : null}
          <div className="field field--row">
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy !== '' || draft.name.trim() === '' || draft.kind === ''}
              onClick={() =>
                void (async () => {
                  const name = draft.name.trim();
                  const warn = chosen.isCloud && ai?.allowCloud !== true;
                  const ok = await run('add', () =>
                    addProvider({
                      name,
                      kind: draft.kind,
                      model: draft.model.trim(),
                      baseUrl: draft.baseUrl.trim(),
                      credential: draft.credential.trim(),
                      key: draft.key.trim() === '' ? undefined : draft.key.trim(),
                    }),
                  );
                  if (ok) {
                    setNote(
                      `${name} added.` +
                        (warn ? ' Allow cloud models above before it will be used.' : ''),
                    );
                    applyPreset('');
                  }
                })()
              }
            >
              {busy === 'add' ? 'Adding…' : 'Add'}
            </button>
            <button
              type="button"
              className="linkbtn"
              disabled={busy !== ''}
              onClick={() => applyPreset('')}
            >
              Cancel
            </button>
          </div>
        </>
      ) : null}

      {note !== '' ? <p className="card__note">{note}</p> : null}
      {error !== '' ? <p className="card__note card__note--warn">{error}</p> : null}
      <p className="card__note">
        Keys go to the OS credential store — the Windows Credential Manager, protected
        per user — and are never written to <code>config.toml</code>, the audit log, or
        this page. Everything else is saved to <code>config.toml</code>, which keeps its
        comments, and takes effect without a restart.
      </p>
    </>
  );
}
