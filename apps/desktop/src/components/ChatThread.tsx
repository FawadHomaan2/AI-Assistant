import { useEffect, useRef } from 'react';
import { Icon } from './Icon';
import { StatusBadge } from './StatusBadge';
import { useStore } from '@/state/store';
import type { ChatMessage } from '@/types';
import './ChatThread.css';

const time = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

const ROLE_LABEL: Record<ChatMessage['role'], string> = {
  user: 'You',
  assistant: 'Jarvis',
  system: 'Jarvis · system',
  tool: 'Tool',
};

function Bubble({ m }: { m: ChatMessage }) {
  const cls = ['msg', `msg--${m.role}`, m.notice ? 'msg--notice' : ''].filter(Boolean).join(' ');
  return (
    <li className={cls}>
      <header className="msg__head">
        <span className="msg__role">{ROLE_LABEL[m.role]}</span>
        {m.notice && <StatusBadge label="Not implemented" kind="blocked" tone="notice" />}
        {m.tool && (
          <StatusBadge
            label={`${m.tool.name} · ${m.tool.status}`}
            kind={m.tool.status === 'succeeded' ? 'ok' : m.tool.status === 'failed' ? 'failed' : 'pending'}
          />
        )}
        <time className="msg__time" dateTime={new Date(m.createdAt).toISOString()}>
          {time(m.createdAt)}
        </time>
      </header>
      <div className="msg__body" data-selectable>
        {m.content}
      </div>
      {m.tool?.detail && <pre className="msg__detail" data-selectable>{m.tool.detail}</pre>}
    </li>
  );
}

export function ChatThread() {
  const messages = useStore((s) => s.messages);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.length]);

  if (messages.length === 0) {
    return (
      <div className="thread thread--empty">
        <Icon name="chat" size={28} />
        <p>No messages. Ask Jarvis something, or press Ctrl+Space from anywhere.</p>
      </div>
    );
  }

  return (
    <div className="thread" role="log" aria-live="polite" aria-label="Conversation">
      <ul className="thread__list">
        {messages.map((m) => (
          <Bubble key={m.id} m={m} />
        ))}
      </ul>
      <div ref={endRef} />
    </div>
  );
}
