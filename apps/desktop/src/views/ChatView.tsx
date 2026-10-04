import { ChatThread } from '@/components/ChatThread';
import { Composer } from '@/components/Composer';
import { SystemStatus } from '@/components/SystemStatus';
import { CoreStatus } from '@/components/CoreStatus';
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
        <div className="chatview__rail-scroll">
          <CoreStatus />
          <SystemStatus />
          <QuickActions />
          <ActivityLog limit={8} />
        </div>
        {/* Pinned outside the scroll area: the kill switch must never require
            scrolling to reach. */}
        <div className="chatview__rail-pin">
          <EmergencyStop />
        </div>
      </aside>
    </div>
  );
}
