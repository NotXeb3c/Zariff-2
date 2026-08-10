import { describe, expect, it, vi } from "vitest";
import { activeGestureWorkflowBindings, controlGestureWorkflow, submitGestureWorkflow } from "./workflowBindingRuntime";

describe("activeGestureWorkflowBindings", () => {
  it("keeps only enabled, complete bindings when the feature is enabled", () => {
    expect(
      activeGestureWorkflowBindings({
        enabled: true,
        bindings: [
          {
            gesture_name: " swipe_up ",
            goal_template: " run my briefing ",
            enabled: true,
          },
          {
            gesture_name: "palm",
            goal_template: "cancel everything",
            enabled: false,
          },
          { gesture_name: "fist", goal_template: " ", enabled: true },
        ],
      }),
    ).toEqual({ swipe_up: "run my briefing" });
  });

  it("returns no bindings when the feature is disabled", () => {
    expect(
      activeGestureWorkflowBindings({
        enabled: false,
        bindings: [{ gesture_name: "swipe_up", goal_template: "run", enabled: true }],
      }),
    ).toEqual({});
  });
});

describe("submitGestureWorkflow", () => {
  it("submits with the gesture gateway source", async () => {
    const call = vi.fn().mockResolvedValue({ status: "submitted" });

    await expect(submitGestureWorkflow(call, "swipe_up", "run my briefing")).resolves.toBe("Started swipe up workflow");
    expect(call).toHaveBeenCalledWith("voice_gesture_workflow_submit", {
      goal: "run my briefing",
      invocation_source: "gesture",
    });
  });

  it("surfaces a daemon rejection", async () => {
    const call = vi.fn().mockResolvedValue({ status: "error", message: "engine unavailable" });

    await expect(submitGestureWorkflow(call, "swipe_up", "run my briefing")).rejects.toThrow("engine unavailable");
  });
});

describe("controlGestureWorkflow", () => {
  it("cancels the selected gesture workflow", async () => {
    const call = vi.fn().mockResolvedValue({ cancelled: true });

    await expect(controlGestureWorkflow(call, "cancel", "wf_1")).resolves.toBe("Gesture workflow cancelled");
    expect(call).toHaveBeenCalledWith("voice_gesture_workflow_cancel", {
      workflow_id: "wf_1",
    });
  });

  it("requires the daemon to confirm the requested transition", async () => {
    const call = vi.fn().mockResolvedValue({ resumed: false });

    await expect(controlGestureWorkflow(call, "continue", "wf_1")).rejects.toThrow("Workflow was not resumed");
  });
});
