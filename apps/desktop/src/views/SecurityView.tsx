import { useCallback, useEffect, useState } from 'react';
import { Page, Explainer, Card } from './Page';
import { StatusBadge } from '@/components/StatusBadge';
import type { BadgeKind } from '@/components/StatusBadge';
import {
  securityAcknowledge,
  securityScan,
  securityStatus,
  type SecurityFinding,
  type SecurityScan,
} from '@/lib/api';
import './views.css';

/**
 * The Security Center.
 *
 * Two rules drive this layout.
 *
 * **Checks that could not run are as prominent as findings.** A panel that
 * shows three green ticks and quietly omits the four checks that failed is
 * worse than no panel: it says "you are fine" on the strength of not having
 * looked. The count in the header is always "N of M", never just N.
 *
 * **The classification word is never shown alone.** Each badge carries what it
 * actually means, because "suspicious" read on its own is heard as "virus",
 * and that is precisely the misreading the four-level scheme exists to prevent.
 */

const BADGE: Record<SecurityFinding['classification'], BadgeKind> = {
  confirmed_event: 'attention',
  suspicious_behavior: 'failed',
  potential_risk: 'pending',
  normal_activity: 'ok',
};

function Finding({ finding, onAcknowledge }: { finding: SecurityFinding; onAcknowledge: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="finding">
      <div className="finding__head">
        <StatusBadge
          label={finding.classificationLabel}
          kind={BADGE[finding.classification]}
          title={finding.classificationMeaning}
        />
        <strong className="finding__title">{finding.title}</strong>
        <span className="finding__sev">{finding.severity}</span>
      </div>
      <p className="finding__why">{finding.explanation}</p>
      <p className="finding__meaning">{finding.classificationMeaning}</p>
      {finding.remediation && <p className="finding__fix">{finding.remediation}</p>}
      <div className="finding__actions">
        <button type="button" className="linkbtn" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide evidence' : 'Show evidence'}
        </button>
        {finding.actionable && (
          <button type="button" className="linkbtn" onClick={onAcknowledge}>
            Mark as seen
          </button>
        )}
      </div>
      {open && (
        <pre className="finding__evidence">{JSON.stringify(finding.evidence, null, 2)}</pre>
      )}
    </li>
  );
}

export function SecurityView() {
  const [scan, setScan] = useState<SecurityScan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let live = true;
    securityStatus().then((res) => {
      if (live && res.ok && res.value.lastScan) setScan(res.value.lastScan);
    });
    return () => {
      live = false;
    };
  }, []);

  const run = useCallback(async () => {
    setBusy(true);
    setError('');
    const res = await securityScan();
    setBusy(false);
    if (!res.ok) {
      // A failed scan must never leave the previous result looking current.
      setError('The scan could not run. The core may not be reachable.');
      return;
    }
    setScan(res.value);
  }, []);

  const normal = scan?.findings.filter((f) => !f.actionable) ?? [];

  return (
    <Page title="Security Center" subtitle="What was checked, what was found, and the evidence.">
      <Explainer>
        Findings are sorted into four levels and never conflated:{' '}
        <strong>confirmed event</strong>, <strong>suspicious behaviour</strong>,{' '}
        <strong>potential risk</strong> and <strong>normal activity</strong>. Something
        unfamiliar is reported as unfamiliar — not as a virus — and every finding carries
        the evidence behind it. Jarvis reads these settings and never changes them.
      </Explainer>

      <Card title="Scan">
        <div className="scan__controls">
          <button type="button" className="btn btn--primary" onClick={run} disabled={busy}>
            {busy ? 'Checking…' : scan ? 'Check again' : 'Run a security check'}
          </button>
          {scan && (
            <span className="scan__count">
              {scan.checksRun} of {scan.checksTotal} checks ran · {scan.elapsedMs} ms
            </span>
          )}
        </div>
        {error && <p className="scan__error">{error}</p>}
        {scan ? (
          <p className="scan__headline">{scan.headline}</p>
        ) : (
          <p className="card__note">
            Nothing has been checked yet, so Jarvis has nothing to report. It will not
            show a status it has not measured.
          </p>
        )}
        {scan?.firstScan && (
          <p className="card__note">
            This was the first scan, so Jarvis is still learning what is normal here.
            Next time it can tell you what changed.
          </p>
        )}
      </Card>

      {scan && scan.unavailable.length > 0 && (
        <Card title={`${scan.unavailable.length} check(s) could not run`}>
          <p className="card__note">
            These are shown as prominently as the findings on purpose. A panel that
            quietly omits what it could not check is telling you that you are fine on
            the strength of not having looked.
          </p>
          <ul className="datalist">
            {scan.unavailable.map((u) => (
              <li key={u.category}>
                <span>{u.name}</span>
                <span className="datalist__note">{u.reason}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {scan && (
        <Card title={`Needs your attention (${scan.actionable.length})`}>
          {scan.actionable.length === 0 ? (
            <p className="card__note">
              Nothing from the checks that ran. That is not the same as “this computer is
              secure” — it means nothing Jarvis knows how to look for was found.
            </p>
          ) : (
            <ul className="findings">
              {scan.actionable.map((f) => (
                <Finding
                  key={f.fingerprint}
                  finding={f}
                  onAcknowledge={async () => {
                    if (f.id) await securityAcknowledge(f.id);
                    void run();
                  }}
                />
              ))}
            </ul>
          )}
        </Card>
      )}

      {normal.length > 0 && (
        <Card title={`Checked and normal (${normal.length})`}>
          <p className="card__note">
            Listed so you can see what was actually looked at, rather than taking “all
            clear” on trust.
          </p>
          <ul className="datalist">
            {normal.map((f) => (
              <li key={f.fingerprint}>
                <span>{f.title}</span>
                <span className="datalist__note">{f.explanation}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </Page>
  );
}
