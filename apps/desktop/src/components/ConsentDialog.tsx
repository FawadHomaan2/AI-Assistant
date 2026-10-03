import { useEffect, useRef, useState } from 'react';
import { Icon } from './Icon';
import { useStore } from '@/state/store';
import type { ConsentRequest } from '@/types';
import './ConsentDialog.css';

const REVERSIBILITY: Record<ConsentRequest['reversible'], string> = {
  'recycle-bin': 'Recoverable — items go to the Recycle Bin',
  undoable: 'Reversible — Juno can undo this',
  permanent: 'Permanent — this cannot be undone',
  unknown: 'Reversibility unknown — treat as permanent',
};

const RISK_LABEL: Record<ConsentRequest['risk'], string> = {
  safe: 'Safe',
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
  critical: 'Critical',
};

/**
 * The consent gate (ARCHITECTURE.md §7).
 *
 * Contract: every prompt states what, where, why, how reversible, and the blast
 * radius. Tier 4-5 require typing a phrase and never offer "remember".
 */
export function ConsentDialog() {
  const consent = useStore((s) => s.consent);
  const resolveConsent = useStore((s) => s.resolveConsent);
  const [details, setDetails] = useState(false);
  const [typed, setTyped] = useState('');
  const [remember, setRemember] = useState<'no' | 'session' | 'always'>('no');
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Reset per request, and focus Cancel — the safe default must be the one
  // that's already selected if someone hits Enter reflexively.
  useEffect(() => {
    setDetails(false);
    setTyped('');
    setRemember('no');
    if (consent) cancelRef.current?.focus();
  }, [consent]);

  useEffect(() => {
    if (!consent) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        resolveConsent({ decision: 'cancel' });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [consent, resolveConsent]);

  if (!consent) return null;

  const phraseOk = !consent.confirmPhrase || typed.trim() === consent.confirmPhrase;

  return (
    <div className="consent__backdrop" role="presentation">
      <div
        className="consent"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="consent-title"
        aria-describedby="consent-summary"
      >
        <header className="consent__head">
          <span className="consent__icon" aria-hidden="true">
            <Icon name="alert" size={18} />
          </span>
          <div>
            <h2 id="consent-title">{consent.title}</h2>
            <span className="consent__risk">{RISK_LABEL[consent.risk]}</span>
          </div>
        </header>

        <p id="consent-summary" className="consent__summary">
          {consent.summary}
        </p>

        <dl className="consent__facts">
          <div>
            <dt>Affects</dt>
            <dd>
              {consent.affectedCount} {consent.affectedCount === 1 ? 'item' : 'items'}
            </dd>
          </div>
          <div>
            <dt>Reversibility</dt>
            <dd>{REVERSIBILITY[consent.reversible]}</dd>
          </div>
          <div>
            <dt>Blast radius</dt>
            <dd>{consent.blastRadius}</dd>
          </div>
          <div>
            <dt>Requested because</dt>
            <dd>{consent.origin}</dd>
          </div>
        </dl>

        <button type="button" className="consent__disclose" onClick={() => setDetails((d) => !d)} aria-expanded={details}>
          <Icon name="chevron" size={13} />
          {details ? 'Hide details' : 'Review details'}
        </button>

        {details && (
          <ul className="consent__targets" data-selectable>
            {consent.targets.map((t) => (
              <li key={t}>{t}</li>
            ))}
            {consent.affectedCount > consent.targets.length && (
              <li className="consent__more">
                …and {consent.affectedCount - consent.targets.length} more
              </li>
            )}
          </ul>
        )}

        {consent.confirmPhrase && (
          <label className="consent__phrase">
            <span>
              Type <code>{consent.confirmPhrase}</code> to enable Confirm
            </span>
            <input
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              aria-label={`Type ${consent.confirmPhrase} to confirm`}
            />
          </label>
        )}

        {consent.allowRemember && (
          <label className="consent__remember">
            <span>Remember this decision</span>
            <select value={remember} onChange={(e) => setRemember(e.target.value as typeof remember)}>
              <option value="no">Just this once</option>
              <option value="session">For this session</option>
              <option value="always">Always for this action</option>
            </select>
          </label>
        )}

        <footer className="consent__actions">
          <button
            ref={cancelRef}
            type="button"
            className="btn btn--ghost"
            onClick={() => resolveConsent({ decision: 'cancel' })}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!phraseOk}
            onClick={() => resolveConsent({ decision: 'confirm', remember })}
          >
            Confirm
          </button>
        </footer>
      </div>
    </div>
  );
}
