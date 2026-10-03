import '@testing-library/react';

/**
 * jsdom lacks a few APIs the components touch. Stub them rather than guarding
 * every call site, so component code stays free of test-only branches.
 */
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Keep the store honest in tests: no Tauri shell is present, so the bridge must
// report `no-bridge` rather than invent metrics.
if ('__TAURI_INTERNALS__' in window) {
  delete (window as unknown as Record<string, unknown>)['__TAURI_INTERNALS__'];
}
