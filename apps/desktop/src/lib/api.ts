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

export interface VoiceStatus {
  ready: boolean;
  state: string;
  reason: string;
  components: VoiceComponent[];
  missing: string[];
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

export function auditLog(
  limit = 100,
): Promise<ApiResult<{ entries: Record<string, unknown>[]; chain_intact: boolean }>> {
  return request(`/audit?limit=${limit}`);
}
