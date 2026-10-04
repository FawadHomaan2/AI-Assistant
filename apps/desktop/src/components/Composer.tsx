import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { VoiceButton } from './VoiceButton';
import { useStore } from '@/state/store';
import './Composer.css';

/** Text entry + voice trigger. Enter sends, Shift+Enter adds a newline. */
export function Composer() {
  const [text, setText] = useState('');
  const sendMessage = useStore((s) => s.sendMessage);
  const stopped = useStore((s) => s.stopped);
  const busy = useStore((s) => s.busy);
  const draft = useStore((s) => s.draft);
  const setDraft = useStore((s) => s.setDraft);
  const ref = useRef<HTMLTextAreaElement>(null);

  // A quick action pre-fills the composer and focuses it, so the user edits the
  // request before sending rather than firing a guessed command.
  useEffect(() => {
    if (!draft) return;
    setText(draft);
    setDraft('');
    ref.current?.focus();
  }, [draft, setDraft]);

  const submit = () => {
    if (!text.trim() || busy) return;
    sendMessage(text);
    setText('');
    // Reset the auto-grown height after sending.
    if (ref.current) ref.current.style.height = 'auto';
  };

  const grow = (el: HTMLTextAreaElement) => {
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <VoiceButton />
      <div className="composer__field">
        <label className="sr-only" htmlFor="composer-input">
          Message Jarvis
        </label>
        <textarea
          id="composer-input"
          ref={ref}
          className="composer__input"
          rows={1}
          value={text}
          placeholder={stopped ? 'Emergency stop is active — clear it to continue' : 'Ask Jarvis to do something…'}
          onChange={(e) => {
            setText(e.target.value);
            grow(e.target);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
      </div>
      <button
        type="submit"
        className="composer__send"
        disabled={!text.trim() || busy}
        aria-label="Send message"
        title="Send (Enter)"
      >
        <Icon name="send" size={17} />
      </button>
    </form>
  );
}
