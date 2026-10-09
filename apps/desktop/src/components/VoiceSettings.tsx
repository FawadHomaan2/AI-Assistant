import { useCallback, useEffect, useState } from 'react';
import { StatusBadge } from './StatusBadge';
import {
  fetchVoiceModels,
  setVoiceSettings,
  voiceStatus,
  type VoiceStatus,
} from '@/lib/api';

/**
 * Whether Jarvis listens for its name from the moment it starts.
 *
 * `[voice] enabled` was dead configuration for two releases — read from
 * config.toml, reported over the API, and acted on by nothing — so the wake
 * word had to be started by hand on every launch. This is the control that
 * makes it mean something, and it is the one deliberate ask that justifies a
 * microphone opening without a button press.
 *
 * It is deliberately not sufficient on its own: `mic.listen` is a separate
 * permission, and the panel says so when the setting is on without it rather
 * than leaving someone to discover that nothing happened at the next launch.
 */
export function VoiceSettings() {
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [downloading, setDownloading] = useState(false);
  // Separate from `error`, which `load()` clears on every successful refresh.
  // A download failure put its reason there and then the reload wiped it, so
  // the button read as doing nothing at all — the very complaint this panel
  // exists to answer.
  const [downloadError, setDownloadError] = useState('');

  const load = useCallback(async () => {
    const res = await voiceStatus();
    if (res.ok) {
      setStatus(res.value);
      setError('');
    } else {
      setError(res.message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (enabled: boolean) => {
    setBusy(true);
    setNote('');
    setError('');
    const res = await setVoiceSettings({ enabled });
    setBusy(false);
    if (!res.ok) {
      setError(res.message);
      return;
    }
    if (res.value.enabled && res.value.needsMicPermission) {
      setNote(
        'Saved — but listening also needs the microphone permission, which is not ' +
          'granted. Turn on "Use the microphone" under Permissions, or nothing will ' +
          'happen at the next launch.',
      );
    } else if (res.value.enabled) {
      setNote('Jarvis will listen for "Hey Jarvis" from the next launch.');
    } else {
      setNote('Jarvis will wait for the microphone button.');
    }
    await load();
  };

  /**
   * Fetch the three models voice needs.
   *
   * This is the action that was missing. The model list could fetch the wake
   * word and the Piper voice, but not the speech model — no catalogue URL, so
   * no Download button — and readiness needs all three. There was no sequence
   * of clicks in the shipped app that ended with a working wake word.
   */
  const download = async () => {
    setDownloading(true);
    setNote('');
    setDownloadError('');
    const res = await fetchVoiceModels();
    setDownloading(false);

    if (!res.ok) {
      setDownloadError(res.message);
      return;
    }
    const { installed, failed, ready } = res.value;
    if (failed.length > 0) {
      // Named individually: "the download failed" after several minutes and
      // 141 MB is not something anyone can act on.
      setDownloadError(failed.map((f) => `${f.model}: ${f.error}`).join(' '));
    }
    if (ready) {
      setNote('Voice is ready. Turn on the switch above to listen from startup.');
    } else if (installed.length > 0) {
      setNote(`Downloaded ${installed.join(', ')}.`);
    }
    await load();
  };

  if (error !== '' && status === null) {
    return <p className="card__note card__note--warn">{error}</p>;
  }
  if (status === null) {
    return <p className="card__note">Reading the voice configuration…</p>;
  }

  const enabled = status.settings.enabled;
  const ready = status.reason === '';

  return (
    <>
      <div className="radios">
        <label className={`radio${enabled ? ' is-on' : ''}`}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy}
            onChange={(e) => void toggle(e.target.checked)}
          />
          <span className="radio__body">
            <strong>Listen for &ldquo;Hey Jarvis&rdquo; at startup</strong>
            <small>
              On, the wake word is live from the moment Jarvis starts — no button to
              press each time. Off, the microphone opens only when you press it. This
              is the one setting that lets the microphone open on its own, which is
              why it is off until you say otherwise.
            </small>
          </span>
        </label>
      </div>

      <ul className="datalist">
        <li>
          <span>Microphone permission</span>
          <span className="datalist__v">
            <StatusBadge
              label={status.micScopeGranted ? 'Granted' : 'Not granted'}
              kind={status.micScopeGranted ? 'ok' : 'blocked'}
              title={
                status.micScopeGranted
                  ? 'Listening is allowed.'
                  : 'Required as well as the setting above. Grant it under Permissions.'
              }
            />
          </span>
        </li>
        <li>
          <span>
            Voice models
            {!ready ? <span className="datalist__note"> · {status.reason}</span> : null}
          </span>
          <span className="datalist__v">
            <StatusBadge
              label={ready ? 'Ready' : 'Incomplete'}
              kind={ready ? 'ok' : 'attention'}
            />
            {/* The row said what was missing and offered no way to get it.
                Here rather than in the model list because the speech model is
                not fetchable from there, and this is where someone looks when
                the wake word does nothing. */}
            {!ready ? (
              <button
                type="button"
                className="linkbtn"
                disabled={downloading}
                onClick={() => void download()}
              >
                {downloading ? 'Downloading…' : 'Download them'}
              </button>
            ) : null}
          </span>
        </li>
        <li>
          <span>
            Microphone
            {!status.microphone.available ? (
              <span className="datalist__note"> · {status.microphone.detail}</span>
            ) : null}
          </span>
          <span className="datalist__v">
            <StatusBadge
              label={status.microphone.listening ? 'Listening now' : 'Idle'}
              kind={status.microphone.listening ? 'ok' : 'pending'}
            />
          </span>
        </li>
      </ul>

      {note !== '' ? <p className="card__note">{note}</p> : null}
      {downloadError !== '' ? (
        <p className="card__note card__note--warn">{downloadError}</p>
      ) : null}
      {error !== '' ? <p className="card__note card__note--warn">{error}</p> : null}
      <p className="card__note">
        All three have to be true before Jarvis listens on its own: this setting, the
        microphone permission, and the models downloaded. Any of them missing and it
        starts without listening and says why in the log, rather than failing to start.
        The emergency stop always closes the microphone.
      </p>
    </>
  );
}
