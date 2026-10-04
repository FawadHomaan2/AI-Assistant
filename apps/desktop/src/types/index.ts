/**
 * Shared frontend types.
 *
 * In Phase 2 the tool/plan types here get generated from the JSON Schemas in
 * `packages/shared` so the Python core and this UI cannot drift. For Phase 1
 * they are hand-written and intentionally narrow.
 */

/** Risk tiers from ARCHITECTURE.md §7. Drives consent behaviour. */
export type RiskTier = 'safe' | 'low' | 'medium' | 'high' | 'critical';

/** Global operating posture (§7, axis 3). */
export type AssistantMode = 'paused' | 'guarded' | 'assisted' | 'developer';

/** Where a message came from / what it represents. */
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

/** Lifecycle of an assistant turn or tool action. */
export type ActionStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'blocked';

export interface ChatMessage {
  id: string;
  role: MessageRole;
  /** Plain text body. Markdown rendering arrives with the AI core in Phase 2. */
  content: string;
  createdAt: number;
  /** Set when this message reports a tool action rather than prose. */
  tool?: { name: string; status: ActionStatus; risk: RiskTier; detail?: string };
  /**
   * Marks a message as an explicit capability notice (e.g. "needs Phase 2").
   * Rendered distinctly so an unimplemented feature is never mistaken for a
   * working answer.
   */
  notice?: boolean;
}

export interface ActivityEntry {
  id: string;
  at: number;
  /** Short verb phrase, e.g. "Opened Chrome". */
  summary: string;
  status: ActionStatus;
  tool?: string;
  /** Which rung of the control ladder was used (§10). Absent until Phase 4. */
  controlLayer?: 'L1' | 'L2' | 'L3' | 'L4';
  detail?: string;
}

/** A pending confirmation request from the governance plane (§7). */
export interface ConsentRequest {
  id: string;
  title: string;
  /** One-sentence plain-language description of the effect. */
  summary: string;
  risk: RiskTier;
  /** What originated this — the user's request plus the plan step. */
  origin: string;
  /** Exact objects affected, e.g. full paths. Shown under "Review details". */
  targets: string[];
  /** Total affected count; may exceed targets.length when truncated. */
  affectedCount: number;
  reversible: 'recycle-bin' | 'undoable' | 'permanent' | 'unknown';
  /** Largest thing that changes if this proceeds. */
  blastRadius: string;
  /** Tier 4-5 require typing this phrase before Confirm enables. */
  confirmPhrase?: string;
  /**
   * True for a prompt generated in the UI (the Settings preview) rather than by
   * the core. A local prompt has nothing waiting on its answer.
   */
  local?: boolean;
  /** Scoped-remember is never offered above tier 3. */
  allowRemember: boolean;
}

export type ConsentDecision =
  | { decision: 'confirm'; remember: 'no' | 'session' | 'always' }
  | { decision: 'cancel' };

/**
 * Live machine metrics. Every field is nullable because "we could not read this"
 * must be representable — we never substitute a placeholder number.
 */
export interface SystemSnapshot {
  cpuPercent: number | null;
  memUsedBytes: number | null;
  memTotalBytes: number | null;
  diskUsedBytes: number | null;
  diskTotalBytes: number | null;
  /** Cumulative since boot; deltas are computed in the UI. */
  netRxBytes: number | null;
  netTxBytes: number | null;
  batteryPercent: number | null;
  batteryCharging: boolean | null;
  processCount: number | null;
  hostname: string | null;
  osName: string | null;
  uptimeSeconds: number | null;
  capturedAt: number;
}

/** Why a panel has no data, so the UI can explain rather than show zeros. */
export type Unavailable =
  | { kind: 'not-implemented'; phase: number; what: string }
  | { kind: 'no-bridge'; what: string }
  | { kind: 'error'; message: string };

export type Loadable<T> =
  | { state: 'loading' }
  | { state: 'ready'; value: T }
  | { state: 'unavailable'; reason: Unavailable };

export type ViewId = 'chat' | 'security' | 'activity' | 'privacy' | 'settings';

// ── Phase 2: the core ────────────────────────────────────────────────────

/** Where the Python core is listening, from the Rust sidecar handshake. */
export interface CoreEndpoint {
  baseUrl: string;
  wsUrl: string;
  token: string;
  version: string;
  pid: number;
}

/** What the router decided a message was asking for. */
export type IntentId = 'chat' | 'computer_task' | 'diagnostic' | 'security' | 'memory';

/** A step the planner produced. */
export interface PlanStep {
  tool: string;
  args: Record<string, unknown>;
  rationale: string;
}

/** Streamed agent events. Mirrors jarvis/agents/types.py. */
export type AgentEvent =
  | { type: 'turn.start'; turn_id: string; session_id: string }
  | {
      type: 'route';
      intent: IntentId;
      confidence: number;
      reason: string;
      signals: string[];
      available_in_phase: number;
      session_id?: string;
    }
  | { type: 'delta'; text: string; session_id?: string }
  | {
      type: 'notice';
      message: string;
      /**
       * Notices arrive from two places and carry different fields. The
       * orchestrator sends "this capability is not built yet" with an intent
       * and the phase that delivers it; the executor sends "this action was
       * refused" with the tool and why. Everything but `message` is therefore
       * optional, and reading one shape's field off the other is a crash.
       */
      intent?: IntentId;
      available_in_phase?: number;
      tool?: string;
      denialCode?: string;
      missingScopes?: string[];
      blocked?: boolean;
      session_id?: string;
    }
  | {
      type: 'memory.recalled';
      memories: { id: string; key: string; sentence: string; confidence: number }[];
    }
  | {
      type: 'memory.learned';
      message: string;
      memories: { id: string; key: string; sentence: string; confidence: number }[];
    }
  | {
      type: 'memory.forgotten';
      message: string;
      removed: { id: string; key: string; sentence: string }[];
    }
  | { type: 'error'; code: string; message: string; provider?: string; session_id?: string }
  | {
      type: 'turn.end';
      elapsed_ms: number;
      handled: string;
      provider?: string;
      model?: string;
      is_cloud?: boolean;
      tokens_in?: number | null;
      tokens_out?: number | null;
      stop_reason?: string | null;
      session_id?: string;
    }
  | {
      type: 'plan';
      steps: PlanStep[];
      unsupported: string;
      session_id?: string;
    }
  | {
      type: 'tool.planned';
      tool: string;
      operation: string;
      summary: string;
      affected: number;
      risk: RiskTier;
      verdict: 'allow' | 'confirm' | 'deny';
      reason: string;
      session_id?: string;
    }
  | {
      type: 'tool.result';
      tool: string;
      operation: string;
      ok: boolean;
      summary: string;
      changes: string[];
      data: Record<string, unknown>;
      verified: boolean;
      elapsedMs: number;
      risk: RiskTier;
      session_id?: string;
    }
  | ({ type: 'consent.request'; session_id?: string } & ConsentRequest)
  | { type: 'pong' };

export interface ProviderInfo {
  name: string;
  kind: string;
  model: string;
  streaming: boolean;
  tools: boolean;
  isCloud: boolean;
  configured: boolean;
  detail: string;
}

export interface CoreHealth {
  status: string;
  version: string;
  schemaVersion: number;
  mode: string;
  emergencyStop: boolean;
  defaultProvider: string;
  credentialStore: { available: boolean; backend: string; detail: string };
}

/** Connection state of the core, so the UI can explain itself. */
export type CoreState =
  | { state: 'connecting' }
  | { state: 'ready'; endpoint: CoreEndpoint; health: CoreHealth }
  | { state: 'failed'; message: string };
