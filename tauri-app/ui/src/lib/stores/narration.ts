import { writable, get } from "svelte/store";
import { onNotification, offNotification, call } from "../api/daemon";
import { companion } from "./companion";

/**
 * Live Execution Narrator store — consumes the two notification types the
 * backend's ExecutionNarrator broadcasts:
 *
 * - "execution_narration": ambient, non-blocking (start/complete of an
 *   action) — spoken immediately via speakText(), no UI state kept.
 * - "execution_interrupt": pre-emptive pause awaiting a response — sets
 *   `active` (which InterruptDialog.svelte renders off) *and* speaks the
 *   reason, satisfying "always pair voice with a visual modal" from a
 *   single event rather than two separate code paths.
 * - "companion_plan_intervention"/"companion_interjection": spoken only
 *   when Zariff actually warns, revises, stops, or acknowledges a live
 *   correction. Routine CONTINUE reviews stay silent.
 *
 * Consumes notification payloads directly (not a re-fetch-on-notify
 * pattern) since latency matters for a live interruption.
 *
 * When `kind === "action_preview"` (the "simulate before executing" gate
 * for autonomous background tasks — see pilot.agents.narrator's
 * on_action_preview), the payload also carries a `preview` object:
 * a real screenshot, an optional highlighted target bbox, and — for
 * browser actions — a real measured DOM diff summary. Never a generated
 * image; see SECURITY.md's Pre-Execution Target Assessment section.
 */

export interface ActionPreviewBbox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ActionPreviewPayload {
  screenshot_base64: string | null;
  bbox: ActionPreviewBbox | null;
  target_label: string | null;
  caption: string;
  dom_diff: { summary?: string; change_score?: number } | null;
}

export interface InterruptState {
  active: boolean;
  planId: string;
  reason: string;
  kind: string;
  timeoutSeconds: number;
  preview: ActionPreviewPayload | null;
}

const DEFAULT_STATE: InterruptState = {
  active: false,
  planId: "",
  reason: "",
  kind: "",
  timeoutSeconds: 120,
  preview: null,
};

function createNarration() {
  const store = writable<InterruptState>({ ...DEFAULT_STATE });

  const notificationHandler = (method: string, params: unknown) => {
    const p = (params ?? {}) as Record<string, unknown>;

    if (method === "execution_narration") {
      const text = String(p.text ?? "");
      if (text) {
        companion.speak({
          channel: "task_narration",
          text,
          taskId: String(p.task_id ?? p.plan_id ?? ""),
          dedupeKey: [
            "narration",
            String(p.task_id ?? p.plan_id ?? ""),
            String(p.phase ?? ""),
            String(p.action_type ?? ""),
            String(p.target ?? ""),
          ].join(":"),
        });
      }
      return;
    }

    if (method === "execution_interrupt") {
      const reason = String(p.reason ?? "");
      store.set({
        active: true,
        planId: String(p.plan_id ?? ""),
        reason,
        kind: String(p.kind ?? ""),
        timeoutSeconds: Number(p.timeout_seconds ?? 120),
        preview: (p.preview as ActionPreviewPayload | undefined) ?? null,
      });
      if (reason) {
        companion.speak({
          channel: "approval_risk",
          text: reason,
          taskId: String(p.task_id ?? ""),
          dedupeKey: `interrupt:${String(p.plan_id ?? "")}`,
        });
      }
      return;
    }

    if (method === "companion_plan_intervention") {
      const decision = String(p.decision ?? "").toUpperCase();
      if (!["WARN", "REVISE", "STOP"].includes(decision)) return;
      const reason = String(p.reason ?? "").trim();
      if (!reason) return;
      const speech =
        decision === "REVISE"
          ? "I found a problem with the plan, so I am correcting it before it runs."
          : decision === "STOP"
            ? "I found a serious problem, so I stopped this task."
            : "I need to flag a concern before I continue.";
      companion.speak({
        channel: decision === "STOP" ? "emergency_stop" : "approval_risk",
        text: speech,
        taskId: String(p.task_id ?? ""),
        dedupeKey: `plan-intervention:${String(p.plan_id ?? "")}:${decision}`,
      });
      return;
    }

    if (method === "companion_interjection") {
      const mode = String(p.mode ?? "").toLowerCase();
      companion.speak({
        channel: mode === "stop" ? "emergency_stop" : "user_speech",
        text: mode === "stop" ? "Stopping now." : "I heard your correction. I am revising the task now.",
        taskId: String(p.task_id ?? ""),
        dedupeKey: `interjection:${String(p.task_id ?? "")}:${mode}`,
      });
      return;
    }

    if (method === "execution_interrupt_timeout" || method === "execution_interrupt_denied") {
      const planId = String(p.plan_id ?? "");
      if (get(store).planId === planId) {
        store.set({ ...DEFAULT_STATE });
      }
      return;
    }

    // A terminal task notification is authoritative. Clear any interrupt UI
    // left behind by a daemon restart, timeout race, or late target assessment
    // so a completed command never leaves Zariff visibly "paused".
    if (["task_complete", "task_failed", "task_cancelled", "task_aborted"].includes(method)) {
      store.set({ ...DEFAULT_STATE });
    }
  };

  onNotification(notificationHandler);
  // Vite keeps the daemon client module alive during browser hot reloads.
  // Remove this store's old callback before installing the next copy, or one
  // intervention can be spoken multiple times in local testing.
  const hot = (
    import.meta as ImportMeta & {
      hot?: { dispose(callback: () => void): void };
    }
  ).hot;
  if (hot) {
    hot.dispose(() => offNotification(notificationHandler));
  }

  async function respond(confirmed: boolean) {
    const current = get(store);
    if (!current.planId) return;
    store.set({ ...DEFAULT_STATE });
    try {
      await call("confirm", { plan_id: current.planId, confirmed });
    } catch {
      /* best-effort -- the backend times out its own wait either way */
    }
  }

  return {
    subscribe: store.subscribe,
    respond,
  };
}

export const narration = createNarration();
