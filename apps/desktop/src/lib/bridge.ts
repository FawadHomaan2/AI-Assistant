/**
 * The ONLY module that talks to the desktop shell.
 *
 * Two reasons this seam exists:
 *  1. Portability — if Tauri ever has to become Electron, this file is the port.
 *  2. Browser development — `npm run dev` in a plain browser has no shell, so
 *     every call degrades to a typed "no-bridge" result instead of throwing.
 *     Crucially it degrades to *unavailable*, never to fabricated data.
 */
import type { CoreEndpoint, SystemSnapshot, Unavailable } from '@/types';

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
 * Latch an emergency stop in the shell. Holds even when the window is hidden,
 * and every tool checks it between steps.
 */
export function emergencyStop(): Promise<BridgeResult<null>> {
  return invoke<null>('emergency_stop');
}

/**
 * Release the shell's latch.
 *
 * This had no binding at all, so resuming cleared the core's stop and left the
 * shell's set. The latch is a process-global flag, so the only thing that reset
 * it was restarting the application — which is what people had to do.
 */
export function clearEmergencyStop(): Promise<BridgeResult<null>> {
  return invoke<null>('clear_emergency_stop');
}

/** Whether the shell's latch is currently set. */
export function emergencyStopState(): Promise<BridgeResult<boolean>> {
  return invoke<boolean>('emergency_stop_state');
}

/** Register/replace the global activation shortcut. Returns the accepted accelerator. */
export function setGlobalShortcut(accelerator: string): Promise<BridgeResult<string>> {
  return invoke<string>('set_global_shortcut', { accelerator });
}

/**
 * Where the Python core is listening, plus the per-launch bearer token.
 * The shell starts the core if it is not already running.
 */
export function getCoreEndpoint(): Promise<BridgeResult<CoreEndpoint>> {
  return invoke<CoreEndpoint>('core_endpoint');
}

export function getCoreStatus(): Promise<
  BridgeResult<{ running: boolean; endpoint: CoreEndpoint | null }>
> {
  return invoke('core_status');
}

export function restartCore(): Promise<BridgeResult<CoreEndpoint>> {
  return invoke<CoreEndpoint>('restart_core');
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
