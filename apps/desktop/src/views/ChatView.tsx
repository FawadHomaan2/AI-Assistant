import { ChatThread } from '@/components/ChatThread';
import { Composer } from '@/components/Composer';
import { SystemStatus } from '@/components/SystemStatus';
import { QuickActions } from '@/components/QuickActions';
import { ActivityLog } from '@/components/ActivityLog';
import { EmergencyStop } from '@/components/EmergencyStop';
import './views.css';

export function ChatView() {
  return (
    <div className="chatview">
      <div className="chatview__main">
        <ChatThread />
        <Composer />
      </div>
      <aside className="chatview__rail" aria-label="Status and quick actions">
        <SystemStatus />
        <QuickActions />
        <ActivityLog limit={8} />
        <EmergencyStop />
      </aside>
    </div>
  );
}
