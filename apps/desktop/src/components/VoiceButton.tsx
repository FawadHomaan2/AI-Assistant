import { Icon } from './Icon';
import { useStore } from '@/state/store';
import './VoiceButton.css';

const LABEL: Record<string, string> = {
  off: 'Voice off',
  unavailable: 'Voice not installed',
  listening: 'Listening for "Juno"',
  recording: 'Recording',
  thinking: 'Thinking',
  speaking: 'Speaking',
};

/**
 * Primary voice control.
 *
 * The microphone state is always visible here and in the tray — there is no
 * state in which audio could be captured without an on-screen indicator. In
 * Phase 1 the state is always `unavailable` because no audio pipeline exists.
 */
export function VoiceButton() {
  const voice = useStore((s) => s.voice);
  const toggleVoice = useStore((s) => s.toggleVoice);
  const stopped = useStore((s) => s.stopped);
  const live = voice === 'recording' || voice === 'listening';

  return (
    <button
      type="button"
      className={`voicebtn voicebtn--${voice}`}
      onClick={toggleVoice}
      disabled={stopped}
      aria-label={LABEL[voice] ?? 'Voice'}
      aria-pressed={live}
      title={
        voice === 'unavailable'
          ? 'Voice needs the Phase 6 speech pipeline (local wake word, STT and TTS)'
          : LABEL[voice]
      }
    >
      <Icon name={voice === 'unavailable' || voice === 'off' ? 'mic-off' : 'mic'} size={20} />
      {live && <span className="voicebtn__pulse" aria-hidden="true" />}
    </button>
  );
}
