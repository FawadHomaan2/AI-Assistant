import { create } from 'zustand';
import type {
  ActionStatus,
  ActivityEntry,
  AgentEvent,
  AssistantMode,
  ChatMessage,
  ConsentDecision,
  ConsentRequest,
  CoreState,
  Loadable,
  ProviderInfo,
  SystemSnapshot,
  ViewId,
} from '@/types';
import { emergencyStop as shellEmergencyStop, getSystemSnapshot } from '@/lib/bridge';
import * as api from '@/lib/api';
import { CoreSocket, type SocketState } from '@/lib/ws';

/** Monotonic id; `crypto.randomUUID` isn't available in every WebView2 build. */
let seq = 0;
const nextId = (prefix: string) => `${prefix}_${Date.now().toString(36)}_${(seq++).toString(36)}`;

/** Capability gates: which phase each surface actually arrives in (see PHASES.md). */
export const PHASE = {
  aiCore: 2,
  fileTools: 3,
  appControl: 4,
  systemTools: 5,
  voice: 6,
  browser: 7,
  memory: 8,
  security: 9,
  permissions: 10,
  plugins: 11,
} as const;

/** The phase this build has actually shipped. */
export const CURRENT_PHASE = 8;

export interface QuickAction {
  id: string;
  label: string;
  hint: string;
  availableIn: number;
  /**
   * Message put into the composer when the action is available. It is a starting
   * point the user edits rather than a command fired blind — a one-click action
   * that guesses at a folder is how the wrong files get touched.
   */
  template?: string;
  /** A built-in handler, for actions that are not a chat message. */
  handler?: 'voice';
}

export const QUICK_ACTIONS: QuickAction[] = [
  {
    id: 'open-apps',
    label: 'Open Apps',
    hint: 'Launch an installed application by name',
    availableIn: PHASE.appControl,
    template: 'open chrome',
  },
  {
    id: 'running-apps',
    label: "What's Running",
    hint: 'List running programs and what is using the CPU',
    availableIn: PHASE.appControl,
    template: 'what programs are running',
  },
  {
    id: 'search-files',
    label: 'Search Files',
    hint: 'Find files by name, type or date',
    availableIn: PHASE.fileTools,
    template: 'find my pdf files in documents',
  },
  {
    id: 'screenshot',
    label: 'Screenshot',
    hint: 'Capture the screen to an image file',
    availableIn: PHASE.systemTools,
    template: 'take a screenshot',
  },
  { id: 'security-scan', label: 'Security Scan', hint: 'Check Defender, firewall, startup items', availableIn: PHASE.security },
  {
    id: 'system-check',
    label: 'System Check',
    hint: 'Measure CPU, memory, disk and uptime and report what crossed a threshold',
    availableIn: PHASE.systemTools,
    template: 'why is my computer slow',
  },
  {
    id: 'find-duplicates',
    label: 'Find Duplicates',
    hint: 'Group files with identical contents',
    availableIn: PHASE.fileTools,
    template: 'find duplicate files in downloads',
  },
  {
    id: 'web-search',
    label: 'Search the Web',
    hint: 'Search the web and read the results',
    availableIn: PHASE.browser,
    template: 'search the web for ',
  },
  {
    id: 'voice',
    label: 'Voice Assistant',
    hint: 'Talk to Jarvis hands-free',
    availableIn: PHASE.voice,
    handler: 'voice',
  },
];

export type VoiceState = 'off' | 'unavailable' | 'listening' | 'recording' | 'thinking' | 'speaking';

const MAX_ACTIVITY = 500;
const MAX_MESSAGES = 1000;

interface AppState {
  // ── Navigation ──────────────────────────────────────────────────────────
  view: ViewId;
  setView: (v: ViewId) => void;

  // ── Posture ─────────────────────────────────────────────────────────────
  mode: AssistantMode;
  setMode: (m: AssistantMode) => void;
  stopped: boolean;

  // ── Core connection ─────────────────────────────────────────────────────
  core: CoreState;
  socketState: SocketState;
  socketDetail: string;
  providers: ProviderInfo[];
  sessionId: string | null;
  connectCore: () => Promise<void>;

  // ── Chat ────────────────────────────────────────────────────────────────
  messages: ChatMessage[];
  busy: boolean;
  /** Pre-filled composer text, set by an available quick action. */
  draft: string;
  setDraft: (text: string) => void;
  /** Id of the assistant message currently being streamed into. */
  streamingId: string | null;
  sendMessage: (text: string) => void;
  /** Apply one streamed agent event. Called by the socket, and by tests. */
  handleEvent: (event: AgentEvent) => void;
  pushMessage: (m: Omit<ChatMessage, 'id' | 'createdAt'>) => string;
  clearMessages: () => void;

  // ── Activity log ────────────────────────────────────────────────────────
  activity: ActivityEntry[];
  logActivity: (e: Omit<ActivityEntry, 'id' | 'at'>) => void;
  clearActivity: () => void;

  // ── Consent ─────────────────────────────────────────────────────────────
  consent: ConsentRequest | null;
  requestConsent: (r: Omit<ConsentRequest, 'id'>) => void;
  resolveConsent: (d: ConsentDecision) => void;

  // ── System metrics ──────────────────────────────────────────────────────
  system: Loadable<SystemSnapshot>;
  refreshSystem: () => Promise<void>;

  // ── Voice (UI state only until Phase 6) ─────────────────────────────────
  voice: VoiceState;
  /** Why voice is unavailable, straight from the core. */
  voiceReason: string;
  toggleVoice: () => void;
  refreshVoice: () => Promise<void>;

  // ── Emergency stop ──────────────────────────────────────────────────────
  triggerEmergencyStop: () => Promise<void>;
  resume: () => Promise<void>;
}

/** Created lazily so the module can be imported in tests without a socket. */
let socket: CoreSocket | null = null;

export const useStore = create<AppState>((set, get) => {
  const applyEvent = (event: AgentEvent): void => {
    const state = get();
    switch (event.type) {
      case 'turn.start':
        set({ sessionId: event.session_id });
        break;

      case 'route': {
        // The routing decision is shown in the activity log rather than the
        // conversation: it is diagnostic detail, not part of the answer.
        state.logActivity({
          summary: `Routed as ${event.intent.replace('_', ' ')}`,
          status: 'succeeded',
          detail: `${event.reason} (confidence ${event.confidence.toFixed(1)})`,
        });
        break;
      }

      case 'delta': {
        const id = get().streamingId;
        if (id) {
          set((s) => ({
            messages: s.messages.map((m) =>
              m.id === id ? { ...m, content: m.content + event.text } : m,
            ),
          }));
        } else {
          const newId = state.pushMessage({ role: 'assistant', content: event.text });
          set({ streamingId: newId });
        }
        break;
      }

      case 'plan': {
        if (event.unsupported) break; // the notice that follows carries the message
        state.logActivity({
          summary: `Planned ${event.steps.length} step${event.steps.length === 1 ? '' : 's'}`,
          status: 'running',
          detail: event.steps.map((s) => s.rationale).join('; '),
        });
        break;
      }

      case 'tool.planned': {
        state.logActivity({
          summary: `${event.tool}.${event.operation} — ${event.verdict}`,
          status: event.verdict === 'deny' ? 'blocked' : 'running',
          tool: event.tool,
          detail: `${event.summary} · ${event.risk} risk · ${event.reason}`,
        });
        break;
      }

      case 'tool.result': {
        state.pushMessage({
          role: 'tool',
          content: event.summary,
          tool: {
            name: `${event.tool}.${event.operation}`,
            status: event.ok ? 'succeeded' : 'failed',
            risk: event.risk,
            detail: event.changes.join('\n') || undefined,
          },
        });
        state.logActivity({
          summary: event.summary,
          status: event.ok ? 'succeeded' : 'failed',
          tool: event.tool,
          detail: event.verified
            ? `${event.elapsedMs} ms · verified`
            : `${event.elapsedMs} ms · NOT verified — the change could not be confirmed`,
        });
        break;
      }

      case 'consent.request': {
        // The core is waiting on this answer, so show it immediately.
        set({ consent: { ...event, id: event.id } });
        state.logActivity({
          summary: `Confirmation requested: ${event.title}`,
          status: 'pending',
          detail: `${event.affectedCount} item(s) · ${event.risk} risk`,
        });
        break;
      }

      case 'memory.recalled': {
        // Shown rather than silent: a belief that shapes an answer must be
        // visible, or Jarvis behaves oddly for reasons nobody can trace.
        state.logActivity({
          summary: `Recalled ${event.memories.length} thing(s) about you`,
          status: 'succeeded',
          detail: event.memories.map((m) => m.sentence).join('; '),
        });
        break;
      }

      case 'memory.learned': {
        state.pushMessage({ role: 'system', content: event.message, notice: true });
        state.logActivity({
          summary: 'Learned a preference',
          status: 'succeeded',
          detail: event.memories.map((m) => m.sentence).join('; '),
        });
        set({ busy: false, streamingId: null });
        break;
      }

      case 'memory.forgotten': {
        state.pushMessage({ role: 'system', content: event.message, notice: true });
        state.logActivity({
          summary: `Forgot ${event.removed.length} thing(s)`,
          status: 'succeeded',
          detail: event.removed.map((m) => m.sentence).join('; ') || 'nothing matched',
        });
        set({ busy: false, streamingId: null });
        break;
      }

      case 'notice': {
        // Either a capability that is not built yet, or an action the
        // permission engine refused. Both are shown as notices so neither can
        // be mistaken for an answer — but they carry different fields, and
        // reading the wrong one used to throw and report the core as broken.
        state.pushMessage({ role: 'system', content: event.message, notice: true });
        const refused = event.tool !== undefined;
        state.logActivity({
          summary: refused
            ? `Refused: ${event.tool}`
            : `Not available: ${(event.intent ?? 'request').replace('_', ' ')}`,
          status: 'blocked',
          tool: event.tool,
          detail: refused
            ? [event.denialCode, (event.missingScopes ?? []).join(', ')]
                .filter(Boolean)
                .join(' · ') || event.message
            : event.available_in_phase
              ? `Arrives in Phase ${event.available_in_phase}`
              : event.message,
        });
        set({ busy: false, streamingId: null });
        break;
      }

      case 'error': {
        state.pushMessage({
          role: 'system',
          content: `${event.message}${event.provider ? ` (provider: ${event.provider})` : ''}`,
          notice: true,
        });
        state.logActivity({
          summary: 'Turn failed',
          status: 'failed',
          detail: `${event.code}: ${event.message}`,
        });
        set({ busy: false, streamingId: null });
        break;
      }

      case 'turn.end': {
        const parts = [`${event.elapsed_ms} ms`];
        if (event.provider) parts.push(event.provider);
        if (event.tokens_out) parts.push(`${event.tokens_out} tokens out`);
        state.logActivity({
          summary: event.handled === 'chat' ? 'Replied' : 'Turn ended',
          status: 'succeeded',
          detail: parts.join(' · '),
        });
        set({ busy: false, streamingId: null });
        break;
      }

      case 'pong':
        break;
    }
  };

  const ensureSocket = (): CoreSocket => {
    if (!socket) {
      socket = new CoreSocket({
        onEvent: (event) => get().handleEvent(event),
        onState: (socketState, socketDetail) => {
          set({ socketState, socketDetail: socketDetail ?? '' });
          if (socketState === 'failed') set({ busy: false, streamingId: null });
        },
      });
    }
    return socket;
  };

  return {
    view: 'chat',
    setView: (view) => set({ view }),

    mode: 'guarded',
    setMode: (mode) => {
      set({ mode, ...(mode !== 'paused' ? { stopped: false } : {}) });
      get().logActivity({ summary: `Mode set to ${mode}`, status: 'succeeded' });
    },
    stopped: false,

    core: { state: 'connecting' },
    socketState: 'idle',
    socketDetail: '',
    providers: [],
    sessionId: null,

    connectCore: async () => {
      set({ core: { state: 'connecting' } });
      const endpoint = await api.resolveEndpoint(true);
      if (!endpoint.ok) {
        const message =
          endpoint.reason.kind === 'no-bridge'
            ? 'Running in a plain browser, so there is no Jarvis core. Launch the desktop app to talk to it.'
            : `Could not start the Jarvis core: ${
                endpoint.reason.kind === 'error' ? endpoint.reason.message : 'unavailable'
              }`;
        set({ core: { state: 'failed', message } });
        get().logActivity({ summary: 'Core unavailable', status: 'failed', detail: message });
        return;
      }

      const health = await api.health();
      if (!health.ok) {
        set({ core: { state: 'failed', message: health.message } });
        get().logActivity({ summary: 'Core health check failed', status: 'failed', detail: health.message });
        return;
      }

      set({ core: { state: 'ready', endpoint: endpoint.value, health: health.value } });
      get().logActivity({
        summary: `Core connected (v${health.value.version})`,
        status: 'succeeded',
        detail: `provider: ${health.value.defaultProvider} · schema v${health.value.schemaVersion}`,
      });

      const list = await api.providers();
      if (list.ok) set({ providers: list.value });

      await get().refreshVoice();

      void ensureSocket().connect();
    },

    messages: [
      {
        id: 'welcome',
        role: 'assistant',
        content:
          "I'm Jarvis. The core is connected and I can hold a conversation — messages " +
          'stream from whichever model provider you configure in Settings.\n\n' +
          'I can work with your files, launch and control applications, see what is ' +
          'running, measure this machine, take screenshots and browse the web. ' +
          'Changing Windows settings, checking your security and remembering things ' +
          'between conversations are not built yet — and until they are, I say so ' +
          'instead of pretending otherwise.',
        createdAt: Date.now(),
      },
    ],
    busy: false,
    streamingId: null,
    draft: '',
    setDraft: (draft) => set({ draft }),
    handleEvent: applyEvent,

    pushMessage: (m) => {
      const id = nextId('msg');
      set((s) => ({ messages: [...s.messages, { ...m, id, createdAt: Date.now() }].slice(-MAX_MESSAGES) }));
      return id;
    },

    sendMessage: (text) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      const { pushMessage, logActivity, stopped, mode, busy, core } = get();
      if (busy) return;

      pushMessage({ role: 'user', content: trimmed });

      if (stopped) {
        pushMessage({
          role: 'system',
          content: 'Emergency stop is active. Clear it from the banner before Jarvis does anything.',
          notice: true,
        });
        return;
      }
      if (mode === 'paused') {
        pushMessage({
          role: 'system',
          content: 'Jarvis is paused, so nothing will run. Switch to Guarded mode in the sidebar to continue.',
          notice: true,
        });
        return;
      }
      if (core.state !== 'ready') {
        pushMessage({
          role: 'system',
          notice: true,
          content:
            core.state === 'failed'
              ? `The Jarvis core isn't available, so there's nothing to answer with. ${core.message}`
              : 'Still connecting to the Jarvis core — try again in a moment.',
        });
        return;
      }

      set({ busy: true, streamingId: null });
      logActivity({ summary: 'Message sent', status: 'running', detail: trimmed.slice(0, 120) });
      ensureSocket().send({
        type: 'chat',
        message: trimmed,
        session_id: get().sessionId,
      });
    },

    clearMessages: () => {
      set({ messages: [], sessionId: null });
      void api.clearHistory();
      get().logActivity({ summary: 'Conversation cleared', status: 'succeeded' });
    },

    activity: [
      { id: 'boot', at: Date.now(), summary: 'Jarvis started', status: 'succeeded', detail: `Phase ${CURRENT_PHASE} build` },
    ],
    logActivity: (e) =>
      set((s) => ({ activity: [...s.activity, { ...e, id: nextId('act'), at: Date.now() }].slice(-MAX_ACTIVITY) })),
    clearActivity: () => set({ activity: [] }),

    consent: null,
    /** Used by the Settings preview; real prompts arrive from the core. */
    requestConsent: (r) => set({ consent: { ...r, id: nextId('consent'), local: true } }),
    resolveConsent: (d) => {
      const pending = get().consent;
      if (!pending) return;
      set({ consent: null });
      const status: ActionStatus = d.decision === 'confirm' ? 'succeeded' : 'cancelled';
      get().logActivity({
        summary: `${d.decision === 'confirm' ? 'Approved' : 'Declined'}: ${pending.title}`,
        status,
        detail:
          d.decision === 'confirm' && d.remember !== 'no'
            ? `Remembered for: ${d.remember}`
            : pending.summary,
      });

      // A preview prompt has no core waiting on it; a real one does.
      if (pending.local) {
        get().pushMessage({
          role: 'system',
          notice: true,
          content:
            d.decision === 'confirm'
              ? `You approved "${pending.title}". This was a preview of the confirmation dialog — no tool ran.`
              : `You cancelled "${pending.title}". Nothing happened.`,
        });
        return;
      }

      ensureSocket().send({
        type: 'consent.response',
        id: pending.id,
        approved: d.decision === 'confirm',
        remember: d.decision === 'confirm' ? d.remember : 'no',
      });
      if (d.decision !== 'confirm') set({ busy: false });
    },

    system: { state: 'loading' },
    refreshSystem: async () => {
      const res = await getSystemSnapshot();
      set({
        system: res.ok
          ? { state: 'ready', value: res.value }
          : { state: 'unavailable', reason: res.reason },
      });
    },

    voice: 'unavailable',
    voiceReason: '',

    refreshVoice: async () => {
      const res = await api.voiceStatus();
      if (!res.ok) {
        set({ voice: 'unavailable', voiceReason: res.message });
        return;
      }
      set({
        voice: res.value.ready ? (res.value.state as VoiceState) : 'unavailable',
        voiceReason: res.value.reason,
      });
    },

    toggleVoice: () => {
      const { voiceReason, voice } = get();
      if (voice === 'unavailable') {
        // The core says exactly which model is missing and how big it is, so
        // the message is specific rather than "voice is not available".
        get().pushMessage({
          role: 'system',
          notice: true,
          content:
            voiceReason ||
            'Voice is not available. The core could not be reached to say why.',
        });
        get().logActivity({ summary: 'Voice unavailable', status: 'blocked', detail: voiceReason });
        return;
      }
      get().pushMessage({
        role: 'system',
        notice: true,
        content:
          'Audio capture runs in the desktop shell, which is not wired to the ' +
          'microphone yet. The pipeline behind it is built and reports ready.',
      });
      get().logActivity({ summary: 'Voice requested', status: 'blocked' });
    },

    triggerEmergencyStop: async () => {
      set({ stopped: true, mode: 'paused', busy: false, consent: null, voice: 'off', streamingId: null });
      get().logActivity({ summary: 'EMERGENCY STOP activated', status: 'cancelled', detail: 'All automation halted' });
      get().pushMessage({
        role: 'system',
        notice: true,
        content:
          'Emergency stop activated. Jarvis is paused, any in-flight response was abandoned, ' +
          'and the core has been told to stop. Tool execution does not exist yet, so there ' +
          `was nothing else running to abort; per-tool cancellation arrives in Phase ${PHASE.permissions}.`,
      });
      // Latch it in both the shell and the core; neither call is required for
      // the UI to be safe, so failures are logged rather than surfaced twice.
      await shellEmergencyStop();
      await api.engageEmergencyStop();
    },

    resume: async () => {
      set({ stopped: false, mode: 'guarded' });
      await api.clearEmergencyStop();
      get().logActivity({ summary: 'Emergency stop cleared', status: 'succeeded' });
    },
  };
});

/** Test helper: drop the module-level socket between cases. */
export function __resetSocket(): void {
  socket?.close();
  socket = null;
}
