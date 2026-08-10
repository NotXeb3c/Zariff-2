/** True when running inside the compiled Tauri desktop shell (native IPC
 * bridge present), false in browser/dev mode (`npm run dev` in a regular
 * browser tab). Was previously duplicated inline at each call site
 * (e.g. GitConflictResolver.svelte) - factored out here since GestureControl
 * needed a third one for the gesture-cursor bridge's dual-path RPC calls. */
export function isTauriRuntime(): boolean {
  try {
    return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
  } catch {
    return false;
  }
}
