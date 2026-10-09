import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AiProviders } from './AiProviders';
import * as api from '@/lib/api';
import { useStore } from '@/state/store';

vi.mock('@/lib/api');

const PRESETS: api.ProviderPreset[] = [
  {
    id: 'anthropic',
    label: 'Claude',
    kind: 'anthropic',
    model: 'claude-opus-5-5',
    baseUrl: '',
    credential: 'jarvis/anthropic',
    isCloud: true,
    needsKey: true,
    keyUrl: 'https://console.anthropic.com/settings/keys',
    note: 'Also claude-sonnet-5-5.',
  },
  {
    id: 'ollama',
    label: 'Ollama (on this machine)',
    kind: 'ollama',
    model: 'qwen2.5:14b-instruct',
    baseUrl: 'http://127.0.0.1:11434',
    credential: '',
    isCloud: false,
    needsKey: false,
    keyUrl: 'https://ollama.com/download',
    note: 'No key and no network.',
  },
  // Cloud with no key: the combination that did not exist until the relay, and
  // the one the dropdown could describe exactly like the local entry above.
  {
    id: 'jarvis_claude',
    label: 'Claude (via Jarvis Cloud, no key)',
    kind: 'jarvis_cloud',
    model: 'anthropic/claude-sonnet-5',
    baseUrl: '',
    credential: '',
    isCloud: true,
    needsKey: false,
    keyUrl: '',
    note: 'Relayed through the Jarvis web app, which sees what you send.',
  },
];

const ECHO = {
  name: 'dev_echo',
  kind: 'dev_echo',
  model: '',
  streaming: true,
  tools: false,
  isCloud: false,
  configured: true,
  needsKey: false,
  detail: 'Ready.',
};

const CLAUDE_UNCONFIGURED = {
  name: 'anthropic',
  kind: 'anthropic',
  model: 'claude-opus-5-5',
  streaming: true,
  tools: true,
  isCloud: true,
  configured: false,
  needsKey: true,
  detail: 'No API key stored. Add one in Settings.',
};

/** Cloud, ready, and nothing to type: the combination the relay introduced. */
const RELAY = {
  name: 'jarvis_chatgpt',
  kind: 'jarvis_cloud',
  model: 'openai/gpt-6-astra',
  streaming: true,
  tools: false,
  isCloud: true,
  configured: true,
  needsKey: false,
  detail: 'No API key needed — the web app holds one.',
};

function state(over: Partial<api.AiState> = {}): api.AiState {
  return {
    default: 'dev_echo',
    allowCloud: false,
    allowCloudContent: false,
    credentialStore: { available: true, backend: 'WinVaultKeyring', detail: 'Ready.' },
    ...over,
  };
}

const mocked = vi.mocked(api);

/**
 * Whether `secret` survives anywhere in the rendered page.
 *
 * `textContent` alone is not enough: it does not include an input's value, so a
 * field that kept the key would pass a textContent check while still holding it.
 */
function leaks(secret: string): boolean {
  if (document.body.textContent?.includes(secret)) return true;
  return [...document.querySelectorAll('input, textarea')].some(
    (el) => (el as HTMLInputElement).value.includes(secret),
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // `clearAllMocks` keeps implementations, so defaults are re-established here
  // rather than once at module scope, where one test's override would leak.
  mocked.providerPresets.mockResolvedValue({ ok: true, value: PRESETS });
  mocked.providers.mockResolvedValue({ ok: true, value: [ECHO] });
  mocked.aiState.mockResolvedValue({ ok: true, value: state() });
  mocked.addProvider.mockResolvedValue({ ok: true, value: { providers: [] } });
  mocked.setProviderKey.mockResolvedValue({ ok: true, value: { configured: true } });
  mocked.clearProviderKey.mockResolvedValue({ ok: true, value: { configured: false, removed: true } });
  mocked.removeProvider.mockResolvedValue({
    ok: true,
    value: { removed: true, keyRemoved: true, default: 'dev_echo' },
  });
  mocked.setAiSettings.mockResolvedValue({
    ok: true,
    value: { default: 'dev_echo', allow_cloud: false, allow_cloud_content: false },
  });
  mocked.providerHealth.mockResolvedValue({ ok: true, value: {} });
});

describe('AiProviders', () => {
  it('lists what is configured, with its kind and model', async () => {
    render(<AiProviders />);
    expect(await screen.findByText('dev_echo')).toBeTruthy();
    expect(screen.getByText(/· dev_echo/)).toBeTruthy();
  });

  it('offers the services the core reports, not a hardcoded list', async () => {
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    // Served from PRESETS in the core, so adding one is a single entry there.
    expect(screen.getByRole('option', { name: /Claude — needs an API key/ })).toBeTruthy();
    expect(screen.getByRole('option', { name: /Ollama.*stays on this machine/ })).toBeTruthy();
  });

  it('does not describe a keyless relay the way it describes a local model', async () => {
    render(<AiProviders />);
    await screen.findByText('dev_echo');

    // Both need no key, and that is the whole of what they have in common: one
    // never leaves the machine and the other goes through someone else's
    // server. A dropdown that said "no key needed" for both would make the
    // more private choice and the less private one read identically, at the
    // moment the choice is made.
    const relay = screen.getByRole('option', { name: /Jarvis Cloud/ });
    const local = screen.getByRole('option', { name: /Ollama/ });
    expect(relay.textContent).toMatch(/sends to the cloud/);
    expect(local.textContent).toMatch(/stays on this machine/);
    expect(relay.textContent).not.toMatch(/stays on this machine/);
  });

  it('says where a relayed conversation goes once it is chosen', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');

    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'jarvis_claude');

    expect(screen.getByText(/sees what you send/)).toBeTruthy();
    // Nothing to type, so no key field should appear asking for one.
    expect(screen.queryByLabelText('API key')).toBeNull();
  });

  it('prefills the fields when a service is chosen', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');

    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'anthropic');

    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('anthropic');
    expect((screen.getByLabelText('Model') as HTMLInputElement).value).toBe('claude-opus-5-5');
    expect(screen.getByText(/Also claude-sonnet-5-5/)).toBeTruthy();
  });

  it('says where keys are issued, as text rather than a link', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'anthropic');

    // Opening a URL would need a webview capability this app does not grant.
    expect(screen.getByText('https://console.anthropic.com/settings/keys')).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('sends the key with the provider, and does not keep it in the field', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'anthropic');

    await user.type(screen.getByLabelText('API key'), 'sk-ant-secret');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => expect(mocked.addProvider).toHaveBeenCalled());
    expect(mocked.addProvider).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'anthropic', kind: 'anthropic', key: 'sk-ant-secret' }),
    );
    // The form is cleared, so the key is not left on screen or in the DOM.
    await waitFor(() => expect(screen.queryByLabelText('API key')).toBeNull());
    expect(leaks('sk-ant-secret')).toBe(false);
  });

  it('warns that a cloud provider will not be used while cloud is off', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'anthropic');
    await user.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByText(/Allow cloud models above before it will be used/)).toBeTruthy();
  });

  it('offers no key field for a cloud provider that takes no key', async () => {
    // The relay is configured, cloud, and has nothing to type. Before
    // `needsKey` the panel decided from `configured` alone, with a hardcoded
    // exception for dev_echo — so this row offered "Replace key", sending
    // someone to the credential store to replace a key that never existed.
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, RELAY] });
    render(<AiProviders />);
    await screen.findByText('jarvis_chatgpt');

    expect(screen.queryByRole('button', { name: /replace key|add key/i })).toBeNull();
  });

  it('still offers one for a provider that does need a key', async () => {
    // The other half: hiding the button whenever a key is absent would hide
    // it exactly when it is needed.
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, CLAUDE_UNCONFIGURED] });
    render(<AiProviders />);
    await screen.findByText('anthropic');

    expect(screen.getByRole('button', { name: /add key/i })).toBeTruthy();
  });

  it('refreshes the app-wide provider list after a change', async () => {
    // `connectCore` read the list once at startup and nothing refreshed it,
    // so Settings' own "Providers the core reports" kept showing the state
    // the app launched with — a provider added here was simply missing from
    // it until a restart, with two panels disagreeing about what exists.
    const user = userEvent.setup();
    useStore.setState({ providers: [] });
    mocked.addProvider.mockResolvedValue({ ok: true, value: { providers: [] } });
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, RELAY] });

    render(<AiProviders />);
    await screen.findByText('dev_echo');
    await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'jarvis_claude');
    await user.click(screen.getByRole('button', { name: /^Add$/ }));

    await waitFor(() =>
      expect(useStore.getState().providers.map((p) => p.name)).toContain('jarvis_chatgpt'),
    );
  });

  it('points out a configured cloud provider that is being refused', async () => {
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, CLAUDE_UNCONFIGURED] });
    render(<AiProviders />);
    expect(await screen.findByText(/it is currently being refused/)).toBeTruthy();
  });

  it('keeps allowing cloud and allowing content as separate switches', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');

    const content = screen.getByRole('checkbox', {
      name: /document and screen content/i,
    }) as HTMLInputElement;
    // A key is not consent, and content is a further step again: the second
    // switch does nothing until the first is on.
    expect(content.disabled).toBe(true);

    await user.click(screen.getByRole('checkbox', { name: /allow cloud models/i }));
    expect(mocked.setAiSettings).toHaveBeenCalledWith({ allowCloud: true });
  });

  it('stores a key for an existing provider without echoing it', async () => {
    const user = userEvent.setup();
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, CLAUDE_UNCONFIGURED] });
    render(<AiProviders />);
    await screen.findByText('anthropic');

    await user.click(screen.getByRole('button', { name: 'Add key' }));
    await user.type(screen.getByLabelText('API key for anthropic'), 'sk-ant-later');
    await user.click(screen.getByRole('button', { name: 'Save key' }));

    await waitFor(() => expect(mocked.setProviderKey).toHaveBeenCalledWith('anthropic', 'sk-ant-later'));
    await waitFor(() => expect(screen.queryByLabelText('API key for anthropic')).toBeNull());
    expect(leaks('sk-ant-later')).toBe(false);
  });

  it('changes which provider is used', async () => {
    const user = userEvent.setup();
    mocked.providers.mockResolvedValue({ ok: true, value: [ECHO, CLAUDE_UNCONFIGURED] });
    render(<AiProviders />);
    await screen.findByText('anthropic');

    // By name rather than by index: the accessible name comes from the label
    // wrapping each radio, so this also checks the rows are labelled at all.
    await user.click(screen.getByRole('radio', { name: 'anthropic' }));
    expect(mocked.setAiSettings).toHaveBeenCalledWith({ default: 'anthropic' });
  });

  it('every provider can be removed, the echo one included', async () => {
    const user = userEvent.setup();
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    // The core refuses the one case that must not happen — removing the last
    // provider — so the button does not need to guess at it.
    await user.click(screen.getByRole('button', { name: 'Remove' }));
    expect(mocked.removeProvider).toHaveBeenCalledWith('dev_echo');
  });

  it('surfaces the reason a removal was refused', async () => {
    const user = userEvent.setup();
    mocked.removeProvider.mockResolvedValue({
      ok: false,
      code: 'jarvis.config',
      message: "'dev_echo' is the only configured provider, so removing it would leave nothing.",
    });
    render(<AiProviders />);
    await screen.findByText('dev_echo');
    await user.click(screen.getByRole('button', { name: 'Remove' }));
    expect(await screen.findByText(/only configured provider/)).toBeTruthy();
  });

  describe('without a credential store', () => {
    beforeEach(() => {
      mocked.aiState.mockResolvedValue({
        ok: true,
        value: state({
          credentialStore: {
            available: false,
            backend: 'none',
            detail: 'The `keyring` package is not installed.',
          },
        }),
      });
    });

    it('says so, and says what still works', async () => {
      render(<AiProviders />);
      expect(await screen.findByText(/No credential store is available/)).toBeTruthy();
      expect(screen.getByText(/keyring. package is not installed/)).toBeTruthy();
      expect(screen.getByText(/Providers that need no key/)).toBeTruthy();
    });

    it('disables the key field rather than letting it look saveable', async () => {
      const user = userEvent.setup();
      render(<AiProviders />);
      await screen.findByText('dev_echo');
      await user.selectOptions(screen.getByLabelText(/add a model provider/i), 'anthropic');

      const field = screen.getByLabelText('API key') as HTMLInputElement;
      expect(field.disabled).toBe(true);
      expect(field.placeholder).toMatch(/no credential store/i);
    });
  });

  it('reports a core that cannot be reached instead of rendering an empty panel', async () => {
    mocked.providerPresets.mockResolvedValue({
      ok: false,
      code: 'jarvis.no_core',
      message: 'The Jarvis core is not running.',
    });
    mocked.providers.mockResolvedValue({
      ok: false,
      code: 'jarvis.no_core',
      message: 'The Jarvis core is not running.',
    });
    mocked.aiState.mockResolvedValue({
      ok: false,
      code: 'jarvis.no_core',
      message: 'The Jarvis core is not running.',
    });
    render(<AiProviders />);
    expect(await screen.findByText('The Jarvis core is not running.')).toBeTruthy();
  });
});
