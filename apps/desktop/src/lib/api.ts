/**
 * Typed client for the Jarvis core.
 *
 * The core's address and bearer token come from the Rust sidecar handshake at
 * runtime — nothing is hardcoded, and the token never touches disk. Every call
 * returns a typed result rather than throwing, so callers must handle failure
 * explicitly instead of letting a rejected promise disappear.
 */
import type { CoreEndpoint, CoreHealth, ProviderInfo } from '@/types';
import { getCoreEndpoint, type BridgeResult } from '@/lib/bridge';

export type ApiResult<T> = { ok: true; value: T } | { ok: false; code: string; message: string };

let endpoint: CoreEndpoint | null = null;

/** Cached endpoint, fetched from the shell on first use. */
export async function resolveEndpoint(force = false): Promise<BridgeResult<CoreEndpoint>> {
  if (endpoint && !force) return { ok: true, value: endpoint };
  const res = await getCoreEndpoint();
  if (res.ok) endpoint = res.value;
  return res;
}

export function currentEndpoint(): CoreEndpoint | null {
  return endpoint;
}

export function clearEndpoint(): void {
  endpoint = null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<ApiResult<T>> {
  const resolved = await resolveEndpoint();
  if (!resolved.ok) {
    return {
      ok: false,
      code: 'jarvis.no_core',
      message:
        resolved.reason.kind === 'no-bridge'
          ? 'The Jarvis core runs under the desktop shell. A plain browser has no core to talk to.'
          : `Could not reach the core: ${
              resolved.reason.kind === 'error' ? resolved.reason.message : 'unavailable'
            }`,
    };
  }

  try {
    const response = await fetch(`${resolved.value.baseUrl}${path}`, {
      ...init,
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${resolved.value.token}`,
        ...(init.headers ?? {}),
      },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      return {
        ok: false,
        code: (body?.code as string) ?? `http.${response.status}`,
        message: (body?.message as string) ?? `Request failed with ${response.status}.`,
      };
    }
    return { ok: true, value: body as T };
  } catch (err) {
    return {
      ok: false,
      code: 'jarvis.network',
      message: `Could not reach the Jarvis core: ${String(err)}`,
    };
  }
}

/** The core returns snake_case; the UI uses camelCase. Converted in one place. */
function toHealth(raw: Record<string, unknown>): CoreHealth {
  const store = (raw.credential_store ?? {}) as Record<string, unknown>;
  return {
    status: String(raw.status ?? 'unknown'),
    version: String(raw.version ?? ''),
    schemaVersion: Number(raw.schema_version ?? 0),
    mode: String(raw.mode ?? 'guarded'),
    emergencyStop: Boolean(raw.emergency_stop),
    defaultProvider: String(raw.default_provider ?? ''),
    credentialStore: {
      available: Boolean(store.available),
      backend: String(store.backend ?? 'unknown'),
      detail: String(store.detail ?? ''),
    },
  };
}

export async function health(): Promise<ApiResult<CoreHealth>> {
  const res = await request<Record<string, unknown>>('/health');
  return res.ok ? { ok: true, value: toHealth(res.value) } : res;
}

export async function providers(): Promise<ApiResult<ProviderInfo[]>> {
  const res = await request<Record<string, unknown>[]>('/providers');
  if (!res.ok) return res;
  return {
    ok: true,
    value: res.value.map((p) => ({
      name: String(p.name),
      kind: String(p.kind),
      model: String(p.model),
      streaming: Boolean(p.streaming),
      tools: Boolean(p.tools),
      isCloud: Boolean(p.is_cloud),
      configured: Boolean(p.configured),
      detail: String(p.detail),
    })),
  };
}

/** A service the Settings panel can set up, as the core describes it. */
export interface ProviderPreset {
  id: string;
  label: string;
  kind: string;
  model: string;
  baseUrl: string;
  credential: string;
  isCloud: boolean;
  needsKey: boolean;
  keyUrl: string;
  note: string;
}

/**
 * The services on offer, served by the core rather than listed here.
 *
 * Adding one is then a single entry in `jarvis.config.settings.PRESETS`, instead
 * of an edit in the core, this file and the panel that would drift apart.
 */
export async function providerPresets(): Promise<ApiResult<ProviderPreset[]>> {
  const res = await request<{ presets: Record<string, unknown>[] }>('/providers/presets');
  if (!res.ok) return res;
  return {
    ok: true,
    value: (res.value.presets ?? []).map((p) => ({
      id: String(p.id),
      label: String(p.label),
      kind: String(p.kind),
      model: String(p.model ?? ''),
      baseUrl: String(p.base_url ?? ''),
      credential: String(p.credential ?? ''),
      isCloud: Boolean(p.is_cloud),
      needsKey: Boolean(p.needs_key),
      keyUrl: String(p.key_url ?? ''),
      note: String(p.note ?? ''),
    })),
  };
}

export interface ProviderDraft {
  name: string;
  kind: string;
  model?: string;
  baseUrl?: string;
  credential?: string;
  /** Stored in the OS credential store, never in config.toml. Optional. */
  key?: string;
}

/** Add or replace a provider, optionally storing its key in the same call. */
export function addProvider(draft: ProviderDraft): Promise<ApiResult<{ providers: unknown[] }>> {
  return request('/providers', {
    method: 'POST',
    body: JSON.stringify({
      name: draft.name,
      kind: draft.kind,
      model: draft.model ?? '',
      base_url: draft.baseUrl ?? '',
      credential: draft.credential ?? '',
      ...(draft.key ? { key: draft.key } : {}),
    }),
  });
}

export function removeProvider(
  name: string,
): Promise<ApiResult<{ removed: boolean; keyRemoved: boolean; default: string }>> {
  return request(`/providers/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

/** Store an API key. The key is never read back — Settings shows "configured". */
export function setProviderKey(
  name: string,
  key: string,
): Promise<ApiResult<{ configured: boolean }>> {
  return request(`/providers/${encodeURIComponent(name)}/credential`, {
    method: 'PUT',
    body: JSON.stringify({ key }),
  });
}

export function clearProviderKey(
  name: string,
): Promise<ApiResult<{ configured: boolean; removed: boolean }>> {
  return request(`/providers/${encodeURIComponent(name)}/credential`, { method: 'DELETE' });
}

export interface AiState {
  default: string;
  allowCloud: boolean;
  allowCloudContent: boolean;
  credentialStore: { available: boolean; backend: string; detail: string };
}

/** The provider in use and the two cloud flags, for rendering the panel. */
export async function aiState(): Promise<ApiResult<AiState>> {
  const res = await request<Record<string, unknown>>('/ai');
  if (!res.ok) return res;
  const store = (res.value.credential_store ?? {}) as Record<string, unknown>;
  return {
    ok: true,
    value: {
      default: String(res.value.default ?? ''),
      allowCloud: Boolean(res.value.allow_cloud),
      allowCloudContent: Boolean(res.value.allow_cloud_content),
      credentialStore: {
        available: Boolean(store.available),
        backend: String(store.backend ?? 'unknown'),
        detail: String(store.detail ?? ''),
      },
    },
  };
}

export interface AiSettings {
  default?: string;
  allowCloud?: boolean;
  allowCloudContent?: boolean;
}

/**
 * Which provider is in use, and whether cloud models may be used at all.
 *
 * Deliberately separate from storing a key: a key kept for later must not start
 * sending conversations off the machine by itself.
 */
export function setAiSettings(
  next: AiSettings,
): Promise<ApiResult<{ default: string; allow_cloud: boolean; allow_cloud_content: boolean }>> {
  return request('/ai', {
    method: 'POST',
    body: JSON.stringify({
      ...(next.default === undefined ? {} : { default: next.default }),
      ...(next.allowCloud === undefined ? {} : { allow_cloud: next.allowCloud }),
      ...(next.allowCloudContent === undefined
        ? {}
        : { allow_cloud_content: next.allowCloudContent }),
    }),
  });
}

/** Ask each configured provider whether its key and model actually work. */
export async function providerHealth(): Promise<
  ApiResult<Record<string, { ok: boolean; detail: string }>>
> {
  return request('/providers/health');
}

export function createSession(): Promise<ApiResult<{ id: string }>> {
  return request('/sessions', { method: 'POST' });
}

export function clearHistory(): Promise<ApiResult<{ deleted: number }>> {
  return request('/sessions', { method: 'DELETE' });
}

export function engageEmergencyStop(): Promise<ApiResult<{ engaged: boolean }>> {
  return request('/emergency-stop', { method: 'POST' });
}

export function clearEmergencyStop(): Promise<ApiResult<{ engaged: boolean }>> {
  return request('/emergency-stop', { method: 'DELETE' });
}

export interface VoiceComponent {
  name: string;
  available: boolean;
  detail: string;
  model: string;
  downloadMb: number;
}

/**
 * The microphone is reported apart from the models on purpose. A missing
 * download is something this interface can offer to fix; no audio device is
 * not, and showing both as "voice is not ready" sends people to the wrong
 * place.
 */
export interface VoiceMicrophone {
  available: boolean;
  detail: string;
  listening: boolean;
  framesSeen: number;
  framesDropped: number;
}

export interface VoiceStatus {
  ready: boolean;
  state: string;
  reason: string;
  components: VoiceComponent[];
  missing: string[];
  microphone: VoiceMicrophone;
  micScopeGranted: boolean;
  /** `[voice]` from config.toml. `enabled` means "listen from launch". */
  settings: { enabled: boolean; wakeWord: string; pushToTalk: boolean };
}

export async function voiceStatus(): Promise<ApiResult<VoiceStatus>> {
  const res = await request<Record<string, unknown>>('/voice/status');
  if (!res.ok) return res;
  const raw = res.value;
  return {
    ok: true,
    value: {
      ready: Boolean(raw.ready),
      state: String(raw.state ?? 'off'),
      reason: String(raw.reason ?? ''),
      missing: (raw.missing as string[]) ?? [],
      micScopeGranted: Boolean(raw.micScopeGranted),
      settings: {
        enabled: Boolean((raw.settings as Record<string, unknown>)?.enabled),
        wakeWord: String((raw.settings as Record<string, unknown>)?.wakeWord ?? 'hey_jarvis'),
        pushToTalk: Boolean((raw.settings as Record<string, unknown>)?.pushToTalk),
      },
      microphone: {
        available: Boolean((raw.microphone as Record<string, unknown>)?.available),
        detail: String((raw.microphone as Record<string, unknown>)?.detail ?? ''),
        listening: Boolean((raw.microphone as Record<string, unknown>)?.listening),
        framesSeen: Number((raw.microphone as Record<string, unknown>)?.framesSeen ?? 0),
        framesDropped: Number((raw.microphone as Record<string, unknown>)?.framesDropped ?? 0),
      },
      components: ((raw.components as Record<string, unknown>[]) ?? []).map((c) => ({
        name: String(c.name),
        available: Boolean(c.available),
        detail: String(c.detail),
        model: String(c.model ?? ''),
        downloadMb: Number(c.downloadMb ?? 0),
      })),
    },
  };
}

export interface VoiceSettingsReply {
  enabled: boolean;
  wakeWord: string;
  pushToTalk: boolean;
  needsMicPermission: boolean;
  listening: boolean;
}

/**
 * Change the voice settings, including listening from launch.
 *
 * `enabled` is what makes the wake word behave like one: on, Jarvis listens
 * from the moment it starts rather than waiting for a button every session. It
 * is not sufficient on its own — `mic.listen` is still required — and the reply
 * says so through `needsMicPermission`.
 */
export async function setVoiceSettings(next: {
  enabled?: boolean;
  wakeWord?: string;
  pushToTalk?: boolean;
}): Promise<ApiResult<VoiceSettingsReply>> {
  const res = await request<Record<string, unknown>>('/voice/settings', {
    method: 'POST',
    body: JSON.stringify({
      ...(next.enabled === undefined ? {} : { enabled: next.enabled }),
      ...(next.wakeWord === undefined ? {} : { wake_word: next.wakeWord }),
      ...(next.pushToTalk === undefined ? {} : { push_to_talk: next.pushToTalk }),
    }),
  });
  if (!res.ok) return res;
  return {
    ok: true,
    value: {
      enabled: Boolean(res.value.enabled),
      wakeWord: String(res.value.wakeWord ?? ''),
      pushToTalk: Boolean(res.value.pushToTalk),
      needsMicPermission: Boolean(res.value.needsMicPermission),
      listening: Boolean(res.value.listening),
    },
  };
}

export function startListening(): Promise<ApiResult<{ listening: boolean; state: string }>> {
  return request('/voice/listen', { method: 'POST' });
}

export function stopListening(): Promise<ApiResult<{ listening: boolean; state: string }>> {
  return request('/voice/stop', { method: 'POST' });
}

export interface BrowserStatus {
  available: boolean;
  detail: string;
  running: boolean;
  currentUrl: string;
  allowedHosts: string[];
  allowAnyHost: boolean;
  allowLoopback: boolean;
  headless: boolean;
  searchEngine: string;
}

export async function browserStatus(): Promise<ApiResult<BrowserStatus>> {
  const res = await request<Record<string, unknown>>('/browser/status');
  if (!res.ok) return res;
  const raw = res.value;
  return {
    ok: true,
    value: {
      available: Boolean(raw.available),
      detail: String(raw.detail ?? ''),
      running: Boolean(raw.running),
      currentUrl: String(raw.currentUrl ?? ''),
      allowedHosts: (raw.allowedHosts as string[]) ?? [],
      allowAnyHost: Boolean(raw.allowAnyHost),
      allowLoopback: Boolean(raw.allowLoopback),
      headless: Boolean(raw.headless),
      searchEngine: String(raw.searchEngine ?? ''),
    },
  };
}

export interface MemoryRow {
  id: string;
  tier: string;
  key: string;
  value: unknown;
  confidence: number;
  observationCount: number;
  source: 'stated' | 'observed';
  status: 'candidate' | 'active' | 'retired';
  pinned: boolean;
  useCount: number;
  updatedAt: string;
  sentence: string;
}

export interface MemoryStats {
  total: number;
  embedder: string;
  embedderDetail: string;
  semantic: boolean;
  promotionThreshold: number;
}

export async function memoryList(
  query = '',
): Promise<ApiResult<{ memories: MemoryRow[]; stats: MemoryStats }>> {
  const suffix = query ? `?q=${encodeURIComponent(query)}` : '';
  return request(`/memory${suffix}`);
}

export function memoryForget(id: string): Promise<ApiResult<{ deleted: string }>> {
  return request(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function memoryClear(): Promise<ApiResult<{ deleted: number }>> {
  return request('/memory', { method: 'DELETE' });
}

export function memoryEdit(
  id: string,
  patch: { value?: unknown; pinned?: boolean },
): Promise<ApiResult<MemoryRow>> {
  return request(`/memory/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export interface SecurityFinding {
  id: string;
  category: string;
  title: string;
  classification: 'confirmed_event' | 'suspicious_behavior' | 'potential_risk' | 'normal_activity';
  classificationLabel: string;
  classificationMeaning: string;
  severity: 'info' | 'low' | 'medium' | 'high' | 'critical';
  explanation: string;
  evidence: Record<string, unknown>;
  remediation: string;
  novel: boolean;
  fingerprint: string;
  actionable: boolean;
}

export interface SecurityCheck {
  category: string;
  name: string;
  ran: boolean;
  summary: string;
  unavailableReason: string;
  findings: SecurityFinding[];
}

export interface SecurityScan {
  startedAt: string;
  elapsedMs: number;
  firstScan: boolean;
  headline: string;
  checks: SecurityCheck[];
  findings: SecurityFinding[];
  actionable: SecurityFinding[];
  unavailable: { name: string; category: string; reason: string }[];
  checksRun: number;
  checksTotal: number;
}

export interface SecurityStatus {
  lastScan: SecurityScan | null;
  openFindings: number;
  baselineItems: number;
  baselineEstablished: boolean;
  collector: string;
}

export function securityStatus(): Promise<ApiResult<SecurityStatus>> {
  return request('/security/status');
}

export function securityScan(): Promise<ApiResult<SecurityScan>> {
  return request('/security/scan', { method: 'POST' });
}

export function securityAcknowledge(id: string): Promise<ApiResult<{ acknowledged: string }>> {
  return request(`/security/findings/${encodeURIComponent(id)}/acknowledge`, { method: 'POST' });
}

export interface ScopeRow {
  scope: string;
  description: string;
  consequence: string;
  granted: boolean;
  pathScoped: boolean;
  alwaysConfirmed: boolean;
  targets: { target: string; expiresAt: string; source: string; grantedAt: string }[];
}

export interface PolicyState {
  mode: string;
  read_only: boolean;
  auto_ceiling: string;
  remembered: string[];
  scopes: ScopeRow[];
  limits: {
    windowSeconds: number;
    perMinute: Record<string, number>;
    used: Record<string, number>;
    promptsPerMinute: number;
    promptsUsed: number;
  };
}

export function permissions(): Promise<ApiResult<{ policy: PolicyState; paths: unknown }>> {
  return request('/permissions');
}

export function grantScope(
  scope: string,
  options: { target?: string; ttlMinutes?: number } = {},
): Promise<ApiResult<{ granted: boolean; scopes: ScopeRow[] }>> {
  return request(`/permissions/scopes/${encodeURIComponent(scope)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(options),
  });
}

export function revokeScope(
  scope: string,
  target = '',
): Promise<ApiResult<{ granted: boolean; scopes: ScopeRow[] }>> {
  const suffix = target ? `?target=${encodeURIComponent(target)}` : '';
  return request(`/permissions/scopes/${encodeURIComponent(scope)}${suffix}`, {
    method: 'DELETE',
  });
}

export function setPolicyMode(
  mode: string,
  readOnly: boolean,
): Promise<ApiResult<{ policy: PolicyState }>> {
  return request('/permissions/mode', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ mode, readOnly }),
  });
}

export interface PluginRow {
  name: string;
  version: string;
  description: string;
  author: string;
  enabled: boolean;
  scopes: string[];
  scopeDetail: { scope: string; description: string }[];
  effectiveScopes: string[];
  approvedScopes: string[];
  needsReapproval: boolean;
  maxRisk: string;
  tools: { name: string; description: string; risk: string; scopes: string[] }[];
  error: string;
  running: boolean;
}

export function listPlugins(): Promise<
  ApiResult<{ plugins: PluginRow[]; directory: string; isolation: string }>
> {
  return request('/plugins');
}

export function setPluginEnabled(name: string, enabled: boolean): Promise<ApiResult<PluginRow>> {
  return request(`/plugins/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`, {
    method: 'POST',
  });
}

export interface ModelRow {
  key: string;
  name: string;
  enables: string;
  sizeMb: number;
  installed: boolean;
  fetchable: boolean;
  /**
   * Set when Jarvis cannot download this from the model list but can still
   * get it another way. Unfetchable and unobtainable are different things,
   * and treating them as one made the speech model look like a dead end.
   */
  prepareRoute: string;
  reason: string;
}

export function listModels(): Promise<
  ApiResult<{
    models: ModelRow[];
    installedCount: number;
    totalCount: number;
    missingMb: number;
    directory: string;
    note: string;
  }>
> {
  return request('/models');
}

export function fetchModel(key: string): Promise<ApiResult<{ installed: boolean }>> {
  return request(`/models/${encodeURIComponent(key)}/fetch`, { method: 'POST' });
}

/**
 * Download the speech model, which `fetchModel` cannot.
 *
 * faster-whisper resolves and fetches its own weights, so the catalogue entry
 * carries no URL and no checksum and `fetchable` is false for it. The core has
 * always had this route; nothing in this app called it, which left the speech
 * model the one voice component with no way to obtain it. Voice needs all
 * three, so the wake word could never start however many times you pressed
 * Download in the model list.
 */
export function prepareSpeechModel(): Promise<
  ApiResult<{ available: boolean; detail: string; ready: boolean; reason: string }>
> {
  return request('/voice/stt/prepare', { method: 'POST' });
}

export interface VoiceModelsResult {
  /** Names of the models now in place. */
  installed: string[];
  /** One entry per model that did not arrive, with the reason. */
  failed: { model: string; error: string }[];
  ready: boolean;
  reason: string;
}

/**
 * Download every model voice needs — wake word, Piper voice and speech model.
 *
 * One call rather than three, and the core decides which models those are:
 * the mapping from a pipeline component to a catalogue key belongs there, and
 * a copy of it here is a copy that can drift. Reports partial success, since
 * 141 MB over three sources is exactly where one part fails and the rest are
 * still worth having.
 */
export function fetchVoiceModels(): Promise<ApiResult<VoiceModelsResult>> {
  return request('/voice/models/fetch', { method: 'POST' });
}

export function auditLog(
  limit = 100,
): Promise<ApiResult<{ entries: Record<string, unknown>[]; chain_intact: boolean }>> {
  return request(`/audit?limit=${limit}`);
}
