import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ConsentDialog } from './ConsentDialog';
import { useStore } from '@/state/store';

const base = {
  title: 'Move 37 files into 5 new folders',
  summary: 'Sort Downloads by type.',
  risk: 'medium' as const,
  origin: '"Organize my Downloads" → step 5',
  targets: ['C:\\a.pdf', 'C:\\b.exe'],
  affectedCount: 37,
  reversible: 'undoable' as const,
  blastRadius: 'Files stay inside Downloads.',
  allowRemember: true,
};

beforeEach(() => {
  useStore.setState({ consent: null, messages: [], activity: [], stopped: false, mode: 'guarded' });
});

describe('ConsentDialog', () => {
  it('renders nothing when no consent is pending', () => {
    render(<ConsentDialog />);
    expect(screen.queryByRole('alertdialog')).toBeNull();
  });

  it('states what, how many, reversibility, blast radius and why', () => {
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    expect(screen.getByText(base.title)).toBeTruthy();
    expect(screen.getByText('37 items')).toBeTruthy();
    expect(screen.getByText(/Reversible/)).toBeTruthy();
    expect(screen.getByText(base.blastRadius)).toBeTruthy();
    expect(screen.getByText(base.origin)).toBeTruthy();
  });

  it('hides exact targets until Review details is opened', async () => {
    const user = userEvent.setup();
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    expect(screen.queryByText('C:\\a.pdf')).toBeNull();
    await user.click(screen.getByRole('button', { name: /review details/i }));
    expect(screen.getByText('C:\\a.pdf')).toBeTruthy();
  });

  it('shows how many targets were truncated', async () => {
    const user = userEvent.setup();
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    await user.click(screen.getByRole('button', { name: /review details/i }));
    expect(screen.getByText(/and 35 more/)).toBeTruthy();
  });

  it('focuses Cancel, so a reflexive Enter does not approve', () => {
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
  });

  it('Escape cancels', async () => {
    const user = userEvent.setup();
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    await user.keyboard('{Escape}');
    expect(useStore.getState().consent).toBeNull();
    expect(useStore.getState().activity.at(-1)?.status).toBe('cancelled');
  });

  it('offers scoped remember for medium risk', () => {
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    expect(screen.getByText('Remember this decision')).toBeTruthy();
  });

  // Tier 4-5 contract: typed phrase required, and "remember" never offered.
  it('keeps Confirm disabled until the exact phrase is typed', async () => {
    const user = userEvent.setup();
    useStore.getState().requestConsent({
      ...base,
      title: 'Permanently delete 37 files',
      risk: 'critical',
      reversible: 'permanent',
      confirmPhrase: 'DELETE',
      allowRemember: false,
    });
    render(<ConsentDialog />);

    const confirm = screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(screen.queryByText('Remember this decision')).toBeNull();

    const input = screen.getByLabelText(/type delete to confirm/i);
    await user.type(input, 'delete');
    expect((screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement).disabled).toBe(true);

    await user.clear(input);
    await user.type(input, 'DELETE');
    expect((screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('confirming resolves the request', async () => {
    const user = userEvent.setup();
    useStore.getState().requestConsent(base);
    render(<ConsentDialog />);
    await user.click(screen.getByRole('button', { name: 'Confirm' }));
    expect(useStore.getState().consent).toBeNull();
    expect(useStore.getState().activity.at(-1)?.status).toBe('succeeded');
  });
});
