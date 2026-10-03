import { Page, Explainer, NotImplemented } from './Page';
import { PHASE } from '@/state/store';

/**
 * Security & Privacy Center shell.
 *
 * Deliberately shows no status at all rather than a reassuring green tick: a
 * security panel that reports "protected" without having checked anything is
 * worse than no panel.
 */
export function SecurityView() {
  return (
    <Page title="Security Center" subtitle="Monitoring for suspicious activity on this computer.">
      <Explainer>
        Findings will be sorted into four levels and never conflated:{' '}
        <strong>confirmed event</strong>, <strong>suspicious behaviour</strong>,{' '}
        <strong>potential risk</strong>, and <strong>normal activity</strong>. Something
        unfamiliar is reported as unfamiliar — not as a virus — and every finding
        carries the evidence that triggered it plus an explanation of why.
      </Explainer>

      <NotImplemented
        phase={PHASE.security}
        what="These checks will read Windows' own security state through Defender's WMI provider, the firewall API, the registry's startup locations, and the event log."
        items={[
          'Antivirus and Windows Defender status, including signature age',
          'Firewall state per network profile',
          'Startup programs, compared against a learned baseline',
          'Unusual outbound network connections, with the owning process',
          'Recently installed applications and new signed/unsigned executables',
          'Browser extensions',
          'USB device history',
          'Failed login attempts (needs admin plus audit policy enabled)',
          'Pending Windows and security updates',
          'BitLocker / disk-encryption status',
          'Security-relevant event log entries',
        ]}
      />
    </Page>
  );
}
