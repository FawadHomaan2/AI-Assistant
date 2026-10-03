import { create } from 'zustand';
import type {
  ActionStatus,
  ActivityEntry,
  AssistantMode,
  ChatMessage,
  ConsentDecision,
  ConsentRequest,
  Loadable,
  SystemSnapshot,
  ViewId,
} from '@/types';
import { emergencyStop as shellEmergencyStop, getSystemSnapshot } from '@/lib/bridge';

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

export interface QuickAction {
  id: string;
  label: string;
  hint: string;
  /** Phase that makes this actually work. */
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

/** The phase this build has actually shipped. Bump as phases land. */
export const CURRENT_PHASE = 1;

export type VoiceState = 'off' | 'unavailable' | 'listening' | 'recording' | 'thinking' | 'speaking';

interface AppState {
  // ── Navigation ──────────────────────────────────────────────────────────
  view: ViewId;
  setView: (v: ViewId) => void;

  // ── Posture ─────────────────────────────────────────────────────────────
  mode: AssistantMode;
  setMode: (m: AssistantMode) => void;
  /** Set by the emergency stop; blocks execution independently of `mode`. */
  stopped: boolean;

  // ── Chat ────────────────────────────────────────────────────────────────
  messages: ChatMessage[];
  /** True while a turn is in flight. Always false in Phase 1 (no core yet). */
  busy: boolean;
  /** Appends a user message and the honest "no core yet" notice. */
  sendMessage: (text: string) => void;
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
  resume: () => void;
}

const MAX_ACTIVITY = 500;
const MAX_MESSAGES = 1000;

export const useStore = create<AppState>((set, get) => ({
  view: 'chat',
  setView: (view) => set({ view }),

  mode: 'guarded',
  setMode: (mode) => {
    set({ mode, ...(mode !== 'paused' ? { stopped: false } : {}) });
    get().logActivity({ summary: `Mode set to ${mode}`, status: 'succeeded' });
  },
  stopped: false,

  messages: [
    {
      id: 'welcome',
      role: 'assistant',
      content:
        "I'm Jarvis. This build is Phase 1 — the desktop interface, system tray, " +
        'global shortcut and live machine stats are working. The AI core that ' +
        'understands instructions and controls your computer arrives in Phase 2, ' +
        "so I can't act on requests yet. Everything you see below reports real " +
        'state or says plainly that it is not implemented.',
      createdAt: Date.now(),
    },
  ],
  busy: false,

  pushMessage: (m) => {
    const id = nextId('msg');
    set((s) => ({ messages: [...s.messages, { ...m, id, createdAt: Date.now() }].slice(-MAX_MESSAGES) }));
    return id;
  },

  sendMessage: (text) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const { pushMessage, logActivity, stopped, mode } = get();
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
        content: 'Jarvis is paused, so nothing will execute. Switch to Guarded mode in the sidebar to continue.',
        notice: true,
      });
      return;
    }

    // Phase 1 has no AI core. Say so — do not simulate an answer.
    pushMessage({
      role: 'system',
      notice: true,
      content:
        `Not wired up yet: understanding and acting on "${
          trimmed.length > 60 ? `${trimmed.slice(0, 60)}…` : trimmed
        }" needs the AI core, which is Phase ${PHASE.aiCore}. ` +
        'This build deliberately has no backend, so rather than inventing a reply it tells you that.',
    });
    logActivity({ summary: 'Message received (no AI core in this build)', status: 'blocked', detail: trimmed });
  },

  clearMessages: () => {
    set({ messages: [] });
    get().logActivity({ summary: 'Conversation cleared', status: 'succeeded' });
  },

  activity: [
    { id: 'boot', at: Date.now(), summary: 'Jarvis started', status: 'succeeded', detail: 'Phase 1 build' },
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
    // Honest no-op: there is no audio pipeline in Phase 1.
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
    set({ stopped: true, mode: 'paused', busy: false, consent: null, voice: 'off' });
    get().logActivity({ summary: 'EMERGENCY STOP activated', status: 'cancelled', detail: 'All automation halted' });
    get().pushMessage({
      role: 'system',
      notice: true,
      content:
        'Emergency stop activated. Jarvis is paused and any pending confirmation was dismissed. ' +
        `In this build there is no running automation to abort; cancelling in-flight tool execution lands in Phase ${PHASE.permissions}.`,
    });
    await shellEmergencyStop(); // best-effort; no-op without the shell
  },

  resume: () => {
    set({ stopped: false, mode: 'guarded' });
    get().logActivity({ summary: 'Emergency stop cleared', status: 'succeeded' });
  },
}));
