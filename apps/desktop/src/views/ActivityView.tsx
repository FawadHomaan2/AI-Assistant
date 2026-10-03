import { ActivityLog } from '@/components/ActivityLog';
import { Page, Explainer } from './Page';

export function ActivityView() {
  return (
    <Page
      title="Activity"
      subtitle="Every action Jarvis takes is recorded here."
    >
      <Explainer>
        In this build the log records interface events only. From Phase 3 each row
        also carries the tool name, the arguments digest, the risk tier, the
        consent decision and which control layer was used (L1 native API through
        L4 screen automation), backed by the append-only, hash-chained audit table.
      </Explainer>
      <ActivityLog showClear />
    </Page>
  );
}
