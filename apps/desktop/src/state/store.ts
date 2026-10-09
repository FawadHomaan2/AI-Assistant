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
import {
  emergencyStop as shellEmergencyStop,
  clearEmergencyStop as shellClearEmergencyStop,
  getSystemSnapshot,
} from '@/lib/bridge';
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
  packaging: 12,
  /** Scheduled tasks and reminders are explicitly post-v1 (ARCHITECTURE §15). */
  reminders: 13,
} as const;

/** The phase this build has actually shipped. */
export const CURRENT_PHASE = 12;

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
  handler?: 'voice' | 'privacy' | 'settings';
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
  {
    id: 'security-scan',
    label: 'Security Scan',
    hint: 'Check antivirus, firewall, encryption, startup programs and network listeners',
    availableIn: PHASE.security,
    template: 'check my security',
  },
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
    id: 'permissions',
    label: 'Manage Permissions',
    hint: 'Grant and revoke what Jarvis is allowed to do',
    availableIn: PHASE.permissions,
    handler: 'privacy',
  },
  {
    id: 'plugins',
    label: 'Plugins',
    hint: 'Add capabilities from isolated, scoped plugins',
    availableIn: PHASE.plugins,
    handler: 'settings',
  },
  {
    // Honestly post-v1. Listed so the capability is discoverable, and clicking
    // it says so rather than doing nothing.
    id: 'reminders',
    label: 'Set a Reminder',
    hint: 'Be reminded of something later',
    availableIn: PHASE.reminders,
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
  readOnly: boolean;
  setReadOnly: (readOnly: boolean) => void;
  setMode: (m: AssistantMode) => void;
  stopped: boolean;

  // ── Core connection ─────────────────────────────────────────────────────
  core: CoreState;
  socketState: SocketState;
  socketDetail: string;
  providers: ProviderInfo[];
  sessionId: string | null;
  connectCore: () => Promise<void>;
  /**
   * Re-read the provider list from the core.
   *
   * `connectCore` fetched it once at startup and nothing refreshed it, so
   * adding or removing a provider left the Settings list showing the state
   * the app launched with — a provider you had just added was simply absent,
   * with nothing saying the list was a snapshot.
   */
  refreshProviders: () => Promise<void>;

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

  // ── Voice ───────────────────────────────────────────────────────────────
  voice: VoiceState;
  /** Why voice is unavailable, straight from the core. */
  voiceReason: string;
  /** The microphone, reported apart from the models — different fixes. */
  voiceMic: { available: boolean; detail: string; listening: boolean; dropped: number };
  /** Whether `mic.listen` is granted. Listening is refused without it. */
  micScopeGranted: boolean;
  /** What the wake word last heard, so the interface can show it. */
  lastHeard: string;
  toggleVoice: () => Promise<void>;
  refreshVoice: () => Promise<void>;

  // ── Emergency stop ──────────────────────────────────────────────────────
  /** User-initiated: latches the shell and the core, then updates the UI. */
  triggerEmergencyStop: () => Promise<void>;
  /**
   * Apply a stop that has already happened elsewhere — the tray, or another
   * window. Local state only: it must never call back into the shell, because
   * the shell is what told us.
   */
  applyEmergencyStop: () => void;
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

      // ── voice ──────────────────────────────────────────────────────────
      // The pipeline routes every microphone transition through one method, so
      // these events are the authority on what the microphone is doing. The
      // indicator follows them rather than guessing from what was requested.
      case 'voice.state': {
        set({ voice: event.state as VoiceState });
        break;
      }

      case 'voice.wake': {
        state.logActivity({ summary: 'Woken by the wake word', status: 'succeeded' });
        break;
      }

      case 'voice.transcript': {
        // Shown as the user's own message: it is what they said, and a spoken
        // turn should read in the transcript exactly like a typed one.
        const heard = String(event.text ?? '').trim();
        if (heard) {
          set({ lastHeard: heard });
          state.pushMessage({ role: 'user', content: heard });
        }
        break;
      }

      case 'voice.interrupted': {
        state.logActivity({ summary: 'Interrupted while speaking', status: 'cancelled' });
        break;
      }

      case 'voice.unavailable':
      case 'voice.error': {
        state.pushMessage({
          role: 'system',
          notice: true,
          content: String(event.detail ?? 'The microphone stopped working.'),
        });
        state.logActivity({
          summary: 'Voice problem',
          status: 'failed',
          detail: String(event.detail ?? ''),
        });
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
      // Optimistic in the interface, authoritative in the core. The picker used
      // to change only this store, which meant selecting "Paused" looked like
      // it had worked while the policy engine carried on at its old ceiling.
      set({ mode, ...(mode !== 'paused' ? { stopped: false } : {}) });
      get().logActivity({ summary: `Mode set to ${mode}`, status: 'succeeded' });
      void api.setPolicyMode(mode, get().readOnly).then((res) => {
        if (res.ok) return;
        get().logActivity({
          summary: 'The core did not accept that mode',
          status: 'failed',
          detail: 'The interface and the core disagree; nothing has changed in the core.',
        });
      });
    },

    readOnly: false,
    setReadOnly: (readOnly) => {
      set({ readOnly });
      get().logActivity({
        summary: readOnly ? 'Read-only mode on' : 'Read-only mode off',
        status: 'succeeded',
        detail: readOnly
          ? 'Jarvis will say what an action would do and not do it.'
          : 'Actions run again, subject to the usual confirmations.',
      });
      void api.setPolicyMode(get().mode, readOnly);
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

      await get().refreshProviders();

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
    voiceMic: { available: false, detail: '', listening: false, dropped: 0 },
    micScopeGranted: false,
    lastHeard: '',

    refreshProviders: async () => {
      const list = await api.providers();
      // Left alone on failure: the previous list is more use than an empty
      // one, and the core being briefly unreachable is not news here.
      if (list.ok) set({ providers: list.value });
    },

    refreshVoice: async () => {
      const res = await api.voiceStatus();
      if (!res.ok) {
        set({ voice: 'unavailable', voiceReason: res.message });
        return;
      }
      const { microphone, micScopeGranted, ready, state: coreState, reason } = res.value;
      set({
        // Ready means the models are present. The microphone is a separate
        // question, and `voice` reflects what the pipeline reports either way
        // so a listening indicator can never be on while the core says off.
        voice: ready ? (coreState as VoiceState) : 'unavailable',
        voiceReason: reason,
        micScopeGranted,
        voiceMic: {
          available: microphone.available,
          detail: microphone.detail,
          listening: microphone.listening,
          dropped: microphone.framesDropped,
        },
      });
    },

    toggleVoice: async () => {
      const { voice, voiceReason, voiceMic, micScopeGranted } = get();

      // Already live: stop. Stopping is always allowed and always works, so it
      // is checked before any of the reasons starting might not be.
      if (voice === 'listening' || voice === 'recording' || voice === 'speaking') {
        const res = await api.stopListening();
        set({ voice: res.ok ? (res.value.state as VoiceState) : 'off' });
        get().logActivity({ summary: 'Stopped listening', status: 'succeeded' });
        void get().refreshVoice();
        return;
      }

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

      if (!micScopeGranted) {
        // Deliberately not granted from here. A microphone that stays open is
        // the most invasive thing this program does, so the permission is
        // given once, on purpose, in Permissions — not as a side effect of
        // clicking the button that uses it.
        get().pushMessage({
          role: 'system',
          notice: true,
          content:
            'Listening needs the "Use the microphone" permission, which is off by ' +
            'default. Turn it on in Permissions, then press the microphone again.',
        });
        get().logActivity({
          summary: 'Listening refused',
          status: 'blocked',
          detail: 'mic.listen is not granted',
        });
        return;
      }

      if (!voiceMic.available) {
        get().pushMessage({ role: 'system', notice: true, content: voiceMic.detail });
        get().logActivity({
          summary: 'No microphone',
          status: 'blocked',
          detail: voiceMic.detail,
        });
        return;
      }

      const res = await api.startListening();
      if (!res.ok) {
        get().pushMessage({ role: 'system', notice: true, content: res.message });
        get().logActivity({ summary: 'Could not listen', status: 'failed', detail: res.message });
        void get().refreshVoice();
        return;
      }
      // The state shown comes from the core's reply, not from the fact that the
      // request succeeded, so the indicator cannot claim to be listening while
      // the pipeline says otherwise.
      set({ voice: res.value.state as VoiceState });
      get().logActivity({ summary: 'Listening for "Jarvis"', status: 'succeeded' });
    },

    applyEmergencyStop: () => {
      // Local only. The shell emits `jarvis://emergency-stop` when it latches,
      // and this used to be wired straight to `triggerEmergencyStop`, which
      // invokes the shell again: engage, emit, listen, engage, with an HTTP
      // post and a chat message every time round. One button press froze the
      // window. The shell now only emits on a real transition, and this path
      // calls nothing, so neither side can restart the loop on its own.
      if (get().stopped) return;
      set({
        stopped: true,
        mode: 'paused',
        busy: false,
        consent: null,
        voice: 'off',
        streamingId: null,
        voiceMic: { ...get().voiceMic, listening: false },
      });
      get().logActivity({
        summary: 'EMERGENCY STOP activated',
        status: 'cancelled',
        detail: 'All automation halted',
      });
      get().pushMessage({
        role: 'system',
        notice: true,
        content:
          'Emergency stop activated. Jarvis is paused, any in-flight response was ' +
          'abandoned, the microphone is closed, and every tool refuses until you ' +
          'resume. Press Resume to clear it.',
      });
    },

    triggerEmergencyStop: async () => {
      get().applyEmergencyStop();
      // Latch it in both the shell and the core. Neither call is needed for the
      // UI to be safe, so a failure is logged rather than surfaced twice.
      await shellEmergencyStop();
      await api.engageEmergencyStop();
    },

    resume: async () => {
      // Clear the shell's latch as well as the core's. Only the core's was ever
      // cleared, and the shell's is a process-global flag, so the stop survived
      // every resume and the only way back was restarting the application.
      const [shell, core] = await Promise.all([
        shellClearEmergencyStop(),
        api.clearEmergencyStop(),
      ]);

      // Resume is never blocked on a reachable core. Refusing to leave the
      // stopped state because a call failed is the trap this fix exists to
      // remove: it leaves no way back except restarting. A core that cannot be
      // reached also cannot run anything, so clearing locally is not a claim
      // that something unsafe is now permitted.
      set({ stopped: false, mode: 'guarded', busy: false, streamingId: null });

      const failures = [
        core.ok ? '' : `the core (${core.message})`,
        shell.ok ? '' : 'the desktop shell',
      ].filter(Boolean);

      get().logActivity({
        summary: 'Emergency stop cleared',
        status: failures.length ? 'blocked' : 'succeeded',
        detail: failures.length
          ? `Did not confirm: ${failures.join(' and ')}`
          : 'Shell and core both released',
      });

      if (failures.length) {
        // Said plainly, because the half-state is genuinely confusing: the
        // interface is running but something downstream may still refuse.
        get().pushMessage({
          role: 'system',
          notice: true,
          content:
            `Jarvis has resumed here, but ${failures.join(' and ')} did not confirm. ` +
            'If actions are still refused, press Stop and Resume once more, or restart Jarvis.',
        });
      }
      void get().refreshVoice();
    },
  };
});

/** Test helper: drop the module-level socket between cases. */
export function __resetSocket(): void {
  socket?.close();
  socket = null;
}
