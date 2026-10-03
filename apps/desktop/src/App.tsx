import { useEffect } from 'react';
import { Sidebar } from '@/components/Sidebar';
import { ConsentDialog } from '@/components/ConsentDialog';
import { EmergencyStopBanner } from '@/components/EmergencyStop';
import { ChatView } from '@/views/ChatView';
import { SecurityView } from '@/views/SecurityView';
import { ActivityView } from '@/views/ActivityView';
import { PrivacyView } from '@/views/PrivacyView';
import { SettingsView } from '@/views/SettingsView';
import { useStore } from '@/state/store';
import { listen } from '@/lib/bridge';
import type { ViewId } from '@/types';
import './App.css';

const VIEWS: Record<ViewId, () => JSX.Element> = {
  chat: ChatView,
  security: SecurityView,
  activity: ActivityView,
  privacy: PrivacyView,
  settings: SettingsView,
};

export function App() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const triggerEmergencyStop = useStore((s) => s.triggerEmergencyStop);
  const logActivity = useStore((s) => s.logActivity);
  const Current = VIEWS[view];

  // In-app emergency stop. Ctrl+Shift+Esc is also Windows' Task Manager
  // shortcut, which the OS claims first — so this works while Juno has focus,
  // and the always-visible STOP button covers every other case.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'Escape' || e.code === 'Escape')) {
        e.preventDefault();
        void triggerEmergencyStop();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [triggerEmergencyStop]);

  // Tray menu items are handled in Rust and arrive here as events.
  useEffect(() => {
    const offs: Array<() => void> = [];
    void listen<string>('juno://navigate', (target) => {
      if (target in VIEWS) setView(target as ViewId);
    }).then((off) => offs.push(off));
    void listen<null>('juno://emergency-stop', () => void triggerEmergencyStop()).then((off) => offs.push(off));
    void listen<null>('juno://activated', () =>
      logActivity({ summary: 'Activated via global shortcut', status: 'succeeded' }),
    ).then((off) => offs.push(off));
    return () => offs.forEach((off) => off());
  }, [setView, triggerEmergencyStop, logActivity]);

  return (
    <div className="app">
      <Sidebar />
      <main className="app__main">
        <EmergencyStopBanner />
        <Current />
      </main>
      <ConsentDialog />
    </div>
  );
}
