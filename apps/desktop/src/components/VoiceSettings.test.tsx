import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VoiceSettings } from './VoiceSettings';
import * as api from '@/lib/api';

vi.mock('@/lib/api');

function status(over: Partial<api.VoiceStatus> = {}): api.VoiceStatus {
  return {
    ready: true,
    state: 'off',
    reason: '',
    components: [],
    missing: [],
    micScopeGranted: true,
    settings: { enabled: false, wakeWord: 'hey_jarvis', pushToTalk: true },
    microphone: {
      available: true,
      detail: '',
      listening: false,
      framesSeen: 0,
      framesDropped: 0,
    },
    ...over,
  };
}

const mocked = vi.mocked(api);

beforeEach(() => {
  vi.clearAllMocks();
  mocked.voiceStatus.mockResolvedValue({ ok: true, value: status() });
  mocked.setVoiceSettings.mockResolvedValue({
    ok: true,
    value: {
      enabled: true,
      wakeWord: 'hey_jarvis',
      pushToTalk: true,
      needsMicPermission: false,
      listening: false,
    },
  });
  mocked.fetchVoiceModels.mockResolvedValue({
    ok: true,
    value: {
      installed: ['openWakeWord hey_jarvis', 'Piper en_US voice', 'Whisper speech model'],
      failed: [],
      ready: true,
      reason: '',
    },
  });
});

const INCOMPLETE = {
  ready: false,
  reason:
    'Voice is not ready: speech-to-text (74 MB); wake-word (4 MB). ' +
    'About 78 MB would need downloading.',
};

describe('VoiceSettings', () => {
  it('shows the startup setting as off by default', async () => {
    render(<VoiceSettings />);
    const box = (await screen.findByRole('checkbox', {
      name: /listen for .*hey jarvis.* at startup/i,
    })) as HTMLInputElement;
    expect(box.checked).toBe(false);
  });

  it('turns it on and says the wake word will be live next launch', async () => {
    const user = userEvent.setup();
    render(<VoiceSettings />);
    await user.click(
      await screen.findByRole('checkbox', { name: /listen for .*hey jarvis.* at startup/i }),
    );

    await waitFor(() => expect(mocked.setVoiceSettings).toHaveBeenCalledWith({ enabled: true }));
    expect(await screen.findByText(/listen for "Hey Jarvis" from the next launch/i)).toBeTruthy();
  });

  it('warns when the setting is on but the permission is not granted', async () => {
    const user = userEvent.setup();
    mocked.setVoiceSettings.mockResolvedValue({
      ok: true,
      value: {
        enabled: true,
        wakeWord: 'hey_jarvis',
        pushToTalk: true,
        needsMicPermission: true,
        listening: false,
      },
    });
    render(<VoiceSettings />);
    await user.click(
      await screen.findByRole('checkbox', { name: /listen for .*hey jarvis.* at startup/i }),
    );

    // Otherwise the next launch is silently uneventful and nothing says why.
    expect(await screen.findByText(/needs the microphone permission/i)).toBeTruthy();
    expect(screen.getByText(/nothing will happen at the next launch/i)).toBeTruthy();
  });

  it('reports the permission as a separate requirement', async () => {
    mocked.voiceStatus.mockResolvedValue({
      ok: true,
      value: status({ micScopeGranted: false }),
    });
    render(<VoiceSettings />);
    expect(await screen.findByText('Microphone permission')).toBeTruthy();
    expect(screen.getByTitle(/Required as well as the setting above/)).toBeTruthy();
  });

  it('names what is missing when the models are incomplete', async () => {
    mocked.voiceStatus.mockResolvedValue({
      ok: true,
      value: status({
        ready: false,
        reason: 'Voice is not ready: wake-word (3 files missing). About 4 MB to download.',
      }),
    });
    render(<VoiceSettings />);
    expect(await screen.findByText(/wake-word \(3 files missing\)/)).toBeTruthy();
    expect(screen.getByText('Incomplete')).toBeTruthy();
  });

  it('shows when the microphone is actually open', async () => {
    mocked.voiceStatus.mockResolvedValue({
      ok: true,
      value: status({
        microphone: {
          available: true,
          detail: '',
          listening: true,
          framesSeen: 120,
          framesDropped: 0,
        },
      }),
    });
    render(<VoiceSettings />);
    expect(await screen.findByText('Listening now')).toBeTruthy();
  });

  it('turning it off says the button is back in charge', async () => {
    const user = userEvent.setup();
    mocked.voiceStatus.mockResolvedValue({
      ok: true,
      value: status({ settings: { enabled: true, wakeWord: 'hey_jarvis', pushToTalk: true } }),
    });
    mocked.setVoiceSettings.mockResolvedValue({
      ok: true,
      value: {
        enabled: false,
        wakeWord: 'hey_jarvis',
        pushToTalk: true,
        needsMicPermission: false,
        listening: false,
      },
    });
    render(<VoiceSettings />);
    await user.click(
      await screen.findByRole('checkbox', { name: /listen for .*hey jarvis.* at startup/i }),
    );
    await waitFor(() => expect(mocked.setVoiceSettings).toHaveBeenCalledWith({ enabled: false }));
    expect(await screen.findByText(/wait for the microphone button/i)).toBeTruthy();
  });

  it('surfaces a failure to save', async () => {
    const user = userEvent.setup();
    mocked.setVoiceSettings.mockResolvedValue({
      ok: false,
      code: 'jarvis.config',
      message: 'config.toml is not writable.',
    });
    render(<VoiceSettings />);
    await user.click(
      await screen.findByRole('checkbox', { name: /listen for .*hey jarvis.* at startup/i }),
    );
    expect(await screen.findByText('config.toml is not writable.')).toBeTruthy();
  });

  describe('getting the models', () => {
    it('offers a download when they are incomplete', async () => {
      // The whole bug: the panel named what was missing and gave no way to
      // get it. The speech model is not fetchable from the model list, so
      // there was no sequence of clicks that ended with a working wake word.
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      render(<VoiceSettings />);
      expect(await screen.findByRole('button', { name: /download them/i })).toBeTruthy();
    });

    it('does not offer one when voice is already ready', async () => {
      render(<VoiceSettings />);
      await screen.findByText('Ready');
      expect(screen.queryByRole('button', { name: /download them/i })).toBeNull();
    });

    it('downloads all three in one request', async () => {
      const user = userEvent.setup();
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      render(<VoiceSettings />);
      await user.click(await screen.findByRole('button', { name: /download them/i }));

      // One call, not one per model: the core decides which models voice
      // needs, so that mapping cannot drift from a copy kept here.
      await waitFor(() => expect(mocked.fetchVoiceModels).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/voice is ready/i)).toBeTruthy();
    });

    it('says which model failed and why, not just that something did', async () => {
      const user = userEvent.setup();
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      mocked.fetchVoiceModels.mockResolvedValue({
        ok: true,
        value: {
          installed: ['openWakeWord hey_jarvis'],
          failed: [{ model: 'Piper en_US voice', error: 'Checksum did not match.' }],
          ready: false,
          reason: 'Voice is not ready: text-to-speech (63 MB).',
        },
      });
      render(<VoiceSettings />);
      await user.click(await screen.findByRole('button', { name: /download them/i }));

      // After several minutes and tens of megabytes, "the download failed" is
      // not something anyone can act on.
      expect(await screen.findByText(/Piper en_US voice: Checksum did not match/)).toBeTruthy();
    });

    it('keeps what did arrive when part of it fails', async () => {
      const user = userEvent.setup();
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      mocked.fetchVoiceModels.mockResolvedValue({
        ok: true,
        value: {
          installed: ['openWakeWord hey_jarvis'],
          failed: [{ model: 'Piper en_US voice', error: 'Connection reset.' }],
          ready: false,
          reason: 'Voice is not ready: text-to-speech (63 MB).',
        },
      });
      render(<VoiceSettings />);
      await user.click(await screen.findByRole('button', { name: /download them/i }));

      expect(await screen.findByText(/Downloaded openWakeWord hey_jarvis/)).toBeTruthy();
    });

    it('disables the button while it runs', async () => {
      const user = userEvent.setup();
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      type Result = Awaited<ReturnType<typeof api.fetchVoiceModels>>;
      let release: (v: Result) => void = () => {};
      mocked.fetchVoiceModels.mockReturnValue(
        new Promise<Result>((resolve) => {
          release = resolve;
        }),
      );
      render(<VoiceSettings />);
      const button = await screen.findByRole('button', { name: /download them/i });
      await user.click(button);

      // 141 MB with a live button and no label change is a second download
      // one impatient click away, and nothing saying the first one started.
      const busy = await screen.findByRole('button', { name: /downloading/i });
      expect((busy as HTMLButtonElement).disabled).toBe(true);
      release({ ok: true, value: { installed: [], failed: [], ready: true, reason: '' } });
    });

    it('surfaces an unreachable core rather than looking like a dead button', async () => {
      const user = userEvent.setup();
      mocked.voiceStatus.mockResolvedValue({ ok: true, value: status(INCOMPLETE) });
      mocked.fetchVoiceModels.mockResolvedValue({
        ok: false,
        code: 'jarvis.no_core',
        message: 'The Jarvis core is not running.',
      });
      render(<VoiceSettings />);
      await user.click(await screen.findByRole('button', { name: /download them/i }));
      expect(await screen.findByText('The Jarvis core is not running.')).toBeTruthy();
    });
  });

  it('reports an unreachable core instead of an empty panel', async () => {
    mocked.voiceStatus.mockResolvedValue({
      ok: false,
      code: 'jarvis.no_core',
      message: 'The Jarvis core is not running.',
    });
    render(<VoiceSettings />);
    expect(await screen.findByText('The Jarvis core is not running.')).toBeTruthy();
  });
});
