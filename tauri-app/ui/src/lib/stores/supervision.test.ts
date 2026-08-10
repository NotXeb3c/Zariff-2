import { beforeEach, describe, expect, it, vi } from "vitest";

type NotificationHandler = (method: string, params: unknown) => void;
let capturedHandler: NotificationHandler | null = null;

vi.mock("../api/daemon", () => ({
  onNotification: (handler: NotificationHandler) => {
    capturedHandler = handler;
  },
}));

vi.mock("./companion", () => ({
  companion: {
    speak: vi.fn(),
  },
}));

describe("supervision store", () => {
  beforeEach(() => {
    vi.resetModules();
    capturedHandler = null;
  });

  it("shows and speaks a risk warning without exposing matched content", async () => {
    const { supervision } = await import("./supervision");
    const { companion } = await import("./companion");

    capturedHandler!("supervision_risk_warning", {
      pattern: "destructive_sql",
      source: "ocr",
      message: "Heads up — this looks like it might be: destructive sql.",
    });

    let state: any;
    supervision.subscribe((value) => (state = value))();
    expect(state).toMatchObject({
      active: true,
      kind: "risk",
      pattern: "destructive_sql",
    });
    expect(companion.speak).toHaveBeenCalledWith({
      channel: "approval_risk",
      text: "Heads up — this looks like it might be: destructive sql.",
      dedupeKey: "supervision-risk:destructive_sql",
    });
  });

  it("shows cognitive coaching and dismisses it locally", async () => {
    const { supervision } = await import("./supervision");

    capturedHandler!("supervision_cognitive_checkin", {
      message: "Want to take a short break?",
      stress_level: 0.9,
    });

    let state: any;
    const unsubscribe = supervision.subscribe((value) => (state = value));
    expect(state).toMatchObject({
      active: true,
      kind: "cognitive",
      message: "Want to take a short break?",
    });

    supervision.dismiss();
    expect(state.active).toBe(false);
    expect(state.kind).toBe("");
    unsubscribe();
  });
});
