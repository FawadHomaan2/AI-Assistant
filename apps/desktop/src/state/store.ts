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
export const CURRENT_PHASE = 2;

export interface QuickAction {
  id: string;
  label: string;
  hint: string;
  availableIn: number;
}

export const QUICK_ACTIONS: QuickAction[] = [
  { id: 'open-apps', label: 'Open Apps', hint: 'Launch an application by name', availableIn: PHASE.appControl },
  { id: 'search-files', label: 'Search Files', hint: 'Find files by name, type or date', availableIn: PHASE.fileTools },
  { id: 'screenshot', label: 'Screenshot', hint: 'Capture the screen or a window', availableIn: PHASE.systemTools },
  { id: 'security-scan', label: 'Security Scan', hint: 'Check Defender, firewall, startup items', availableIn: PHASE.security },
  { id: 'system-check', label: 'System Check', hint: 'Diagnose CPU, memory, disk and network', availableIn: PHASE.systemTools },
  { id: 'clean-downloads', label: 'Clean Downloads', hint: 'Sort and de-duplicate your Downloads folder', availableIn: PHASE.fileTools },
  { id: 'voice', label: 'Voice Assistant', hint: 'Talk to Jarvis hands-free', availableIn: PHASE.voice },
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
  toggleVoice: () => void;

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

      case 'notice': {
        // A capability the core does not have yet. Shown as a notice so it can
        // never be mistaken for an answer.
        state.pushMessage({ role: 'system', content: event.message, notice: true });
        state.logActivity({
          summary: `Not available: ${event.intent.replace('_', ' ')}`,
          status: 'blocked',
          detail: `Arrives in Phase ${event.available_in_phase}`,
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

      void ensureSocket().connect();
    },

    messages: [
      {
        id: 'welcome',
        role: 'assistant',
        content:
          "I'm Jarvis. The core is connected and I can hold a conversation — messages " +
          'stream from whichever model provider you configure in Settings.\n\n' +
          "I can't act on your computer yet. Files, applications, the system and " +
          'security monitoring arrive in later phases, and until then I say so instead ' +
          'of pretending otherwise.',
        createdAt: Date.now(),
      },
    ],
    busy: false,
    streamingId: null,
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
    requestConsent: (r) => set({ consent: { ...r, id: nextId('consent') } }),
    resolveConsent: (d) => {
      const pending = get().consent;
      if (!pending) return;
      set({ consent: null });
      const status: ActionStatus = d.decision === 'confirm' ? 'succeeded' : 'cancelled';
      get().logActivity({
        summary: `${d.decision === 'confirm' ? 'Approved' : 'Cancelled'}: ${pending.title}`,
        status,
        detail:
          d.decision === 'confirm' && d.remember !== 'no'
            ? `Remembered for: ${d.remember}`
            : pending.summary,
      });
      get().pushMessage({
        role: 'system',
        notice: true,
        content:
          d.decision === 'confirm'
            ? `You approved "${pending.title}". No tool ran — tools arrive in Phase ${PHASE.fileTools}; this was the consent flow only.`
            : `You cancelled "${pending.title}". Nothing happened.`,
      });
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
    toggleVoice: () => {
      get().pushMessage({
        role: 'system',
        notice: true,
        content:
          `Voice needs the speech pipeline from Phase ${PHASE.voice} ` +
          '(local wake word, speech-to-text and text-to-speech). The button and ' +
          'microphone indicator are built; there is no audio capture behind them yet.',
      });
      get().logActivity({ summary: 'Voice requested (pipeline not implemented)', status: 'blocked' });
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
