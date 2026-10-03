/**
 * The ONLY module that talks to the desktop shell.
 *
 * Two reasons this seam exists:
 *  1. Portability — if Tauri ever has to become Electron, this file is the port.
 *  2. Browser development — `npm run dev` in a plain browser has no shell, so
 *     every call degrades to a typed "no-bridge" result instead of throwing.
 *     Crucially it degrades to *unavailable*, never to fabricated data.
 */
import type { SystemSnapshot, Unavailable } from '@/types';

export type BridgeResult<T> = { ok: true; value: T } | { ok: false; reason: Unavailable };

/** True when running inside the Tauri shell. */
export function hasShell(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

const noBridge = (what: string): Unavailable => ({ kind: 'no-bridge', what });

/**
 * Invoke a Rust command. Imported lazily so the Tauri API never has to resolve
 * in a browser or in jsdom tests.
 */
async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<BridgeResult<T>> {
  if (!hasShell()) return { ok: false, reason: noBridge(cmd) };
  try {
    const mod = await import('@tauri-apps/api/core');
    return { ok: true, value: (await mod.invoke(cmd, args)) as T };
  } catch (err) {
    return { ok: false, reason: { kind: 'error', message: String(err) } };
  }
}

/** Real CPU/RAM/disk/network from the Rust `sysinfo` crate. Never synthesised. */
export function getSystemSnapshot(): Promise<BridgeResult<SystemSnapshot>> {
  return invoke<SystemSnapshot>('system_snapshot');
}

/** Hide to tray; the assistant keeps running. */
export function hideWindow(): Promise<BridgeResult<null>> {
  return invoke<null>('hide_to_tray');
}

/**
 * Broadcast an emergency stop. In Phase 1 this cancels shell-side work and flips
 * the UI to Paused; cancelling in-flight *tool* execution lands in Phase 10 when
 * tools exist to cancel.
 */
export function emergencyStop(): Promise<BridgeResult<null>> {
  return invoke<null>('emergency_stop');
}

/** Register/replace the global activation shortcut. Returns the accepted accelerator. */
export function setGlobalShortcut(accelerator: string): Promise<BridgeResult<string>> {
  return invoke<string>('set_global_shortcut', { accelerator });
}

/** Shell + app version strings for the Settings page. */
export function getShellInfo(): Promise<BridgeResult<{ version: string; tauri: string; platform: string }>> {
  return invoke('shell_info');
}

/**
 * Subscribe to a shell event. Returns an unsubscribe function; a no-op when
 * there is no shell, so callers never need to branch.
 */
export async function listen<T>(event: string, handler: (payload: T) => void): Promise<() => void> {
  if (!hasShell()) return () => {};
  try {
    const mod = await import('@tauri-apps/api/event');
    const unlisten = await mod.listen<T>(event, (e) => handler(e.payload));
    return unlisten;
  } catch {
    return () => {};
  }
}
