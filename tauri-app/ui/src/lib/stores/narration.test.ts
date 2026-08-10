import { describe, it, expect, vi, beforeEach } from "vitest";

// narration.ts talks to the real WebSocket daemon client and browser TTS —
// mock both so this test exercises only the store's own notification
// parsing/state logic, capturing the handler it registers via
// onNotification() so tests can invoke it directly to simulate a
// WebSocket push arriving.
type NotificationHandler = (method: string, params: unknown) => void;
let capturedHandler: NotificationHandler | null = null;

vi.mock("../api/daemon", () => ({
  onNotification: (handler: NotificationHandler) => {
    capturedHandler = handler;
  },
  offNotification: vi.fn(),
  call: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("./companion", () => ({
  companion: {
    speak: vi.fn(),
  },
}));

describe("narration store", () => {
  beforeEach(() => {
    vi.resetModules();
    capturedHandler = null;
  });

  it("defaults to an inactive state with no preview", async () => {
    const { narration } = await import("./narration");
    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.active).toBe(false);
    expect(state.preview).toBeNull();
  });

  it("speaks ambient execution narration", async () => {
    const { narration: _narration } = await import("./narration");
    const { companion } = await import("./companion");

    capturedHandler!("execution_narration", { text: "Starting: notify (Narrator Test)" });

    expect(companion.speak).toHaveBeenCalledWith({
      channel: "task_narration",
      text: "Starting: notify (Narrator Test)",
      taskId: "",
      dedupeKey: "narration::::",
    });
  });

  it("speaks only material companion interventions", async () => {
    const { narration: _narration } = await import("./narration");
    const { companion } = await import("./companion");

    capturedHandler!("companion_plan_intervention", {
      decision: "REVISE",
      reason: "The proposed code step is unnecessary.",
    });

    expect(companion.speak).toHaveBeenCalledWith({
      channel: "approval_risk",
      text: "I found a problem with the plan, so I am correcting it before it runs.",
      taskId: "",
      dedupeKey: "plan-intervention::REVISE",
    });

    vi.mocked(companion.speak).mockClear();
    capturedHandler!("companion_plan_intervention", {
      decision: "CONTINUE",
      reason: "The plan is aligned.",
    });
    expect(companion.speak).not.toHaveBeenCalled();
  });

  it("speaks acknowledgement of a live user correction", async () => {
    const { narration: _narration } = await import("./narration");
    const { companion } = await import("./companion");

    capturedHandler!("companion_interjection", {
      mode: "correct",
      message: "Correction received. Stopping the current step and revising the plan.",
    });

    expect(companion.speak).toHaveBeenCalledWith({
      channel: "user_speech",
      text: "I heard your correction. I am revising the task now.",
      taskId: "",
      dedupeKey: "interjection::correct",
    });
  });

  it("parses a plain risk interrupt with no preview payload", async () => {
    const { narration } = await import("./narration");
    expect(capturedHandler).not.toBeNull();

    capturedHandler!("execution_interrupt", {
      plan_id: "p1",
      reason: "This plan was flagged as risky.",
      kind: "plan_risk",
      timeout_seconds: 120,
    });

    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.active).toBe(true);
    expect(state.kind).toBe("plan_risk");
    expect(state.preview).toBeNull();
  });

  it("parses an action_preview interrupt's preview payload", async () => {
    const { narration } = await import("./narration");

    capturedHandler!("execution_interrupt", {
      plan_id: "p2",
      reason: "About to click: Save",
      kind: "action_preview",
      timeout_seconds: 120,
      preview: {
        screenshot_base64: "abc123",
        bbox: { x: 10, y: 20, w: 80, h: 30 },
        target_label: "Save button",
        caption: "About to click: Save",
        dom_diff: { summary: "change_score=0.40 | +2 nodes added", change_score: 0.4 },
      },
    });

    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.kind).toBe("action_preview");
    expect(state.preview).not.toBeNull();
    expect(state.preview.screenshot_base64).toBe("abc123");
    expect(state.preview.bbox).toEqual({ x: 10, y: 20, w: 80, h: 30 });
    expect(state.preview.dom_diff.summary).toContain("change_score=0.40");
  });

  it("resets preview to null on the next non-preview interrupt", async () => {
    const { narration } = await import("./narration");

    capturedHandler!("execution_interrupt", {
      plan_id: "p3",
      reason: "x",
      kind: "action_preview",
      preview: { screenshot_base64: "abc", bbox: null, target_label: null, caption: "x", dom_diff: null },
    });
    capturedHandler!("execution_interrupt", {
      plan_id: "p4",
      reason: "y",
      kind: "target_assessment",
    });

    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.kind).toBe("target_assessment");
    expect(state.preview).toBeNull();
  });

  it("clears active state on execution_interrupt_denied for the matching plan_id", async () => {
    const { narration } = await import("./narration");

    capturedHandler!("execution_interrupt", { plan_id: "p5", reason: "x", kind: "action_preview" });
    capturedHandler!("execution_interrupt_denied", { plan_id: "p5" });

    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.active).toBe(false);
    expect(state.preview).toBeNull();
  });

  it("clears stale interrupt UI when the task reaches a terminal state", async () => {
    const { narration } = await import("./narration");

    capturedHandler!("execution_interrupt", {
      plan_id: "stale-interrupt",
      reason: "Target is ambiguous",
      kind: "target_assessment",
    });
    capturedHandler!("task_complete", { status: "success" });

    let state: any;
    narration.subscribe((s) => (state = s))();
    expect(state.active).toBe(false);
    expect(state.planId).toBe("");
  });

  it("sends the interrupt decision through the shared confirm RPC", async () => {
    const { narration } = await import("./narration");
    const { call } = await import("../api/daemon");
    capturedHandler!("execution_interrupt", {
      plan_id: "interrupt_123",
      reason: "Target is missing",
      kind: "target_assessment",
    });

    await narration.respond(false);

    expect(call).toHaveBeenCalledWith("confirm", {
      plan_id: "interrupt_123",
      confirmed: false,
    });
  });
});
