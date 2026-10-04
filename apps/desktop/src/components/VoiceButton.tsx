import { Icon } from './Icon';
import { useStore } from '@/state/store';
import './VoiceButton.css';

const LABEL: Record<string, string> = {
  off: 'Voice off',
  unavailable: 'Voice not installed',
  listening: 'Listening for "Jarvis"',
  recording: 'Recording',
  thinking: 'Thinking',
  speaking: 'Speaking',
};

/**
 * Primary voice control.
 *
 * The microphone state is always visible here and in the tray — there is no
 * state in which audio could be captured without an on-screen indicator. The
 * state shown comes from `voice.state` events, which the core emits from the
 * single method every pipeline transition passes through, so the indicator
 * cannot drift out of step with the microphone.
 */
export function VoiceButton() {
  const voice = useStore((s) => s.voice);
  const voiceReason = useStore((s) => s.voiceReason);
  const voiceMic = useStore((s) => s.voiceMic);
  const micScopeGranted = useStore((s) => s.micScopeGranted);
  const toggleVoice = useStore((s) => s.toggleVoice);
  const stopped = useStore((s) => s.stopped);
  const live = voice === 'recording' || voice === 'listening';

  const title = (): string => {
    if (voice === 'unavailable') return voiceReason || 'Voice is not available';
    if (!micScopeGranted) return 'Needs the "Use the microphone" permission — turn it on in Permissions';
    if (!voiceMic.available) return voiceMic.detail || 'No microphone';
    if (voice === 'off') return 'Start listening for "Jarvis"';
    return LABEL[voice] ?? 'Voice';
  };

  return (
    <button
      type="button"
      className={`voicebtn voicebtn--${voice}`}
      onClick={() => void toggleVoice()}
      disabled={stopped}
      aria-label={LABEL[voice] ?? 'Voice'}
      aria-pressed={live}
      title={title()}
    >
      <Icon name={voice === 'unavailable' || voice === 'off' ? 'mic-off' : 'mic'} size={20} />
      {live && <span className="voicebtn__pulse" aria-hidden="true" />}
    </button>
  );
}
