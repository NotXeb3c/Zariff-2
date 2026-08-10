import { describe, expect, it, vi } from "vitest";
import { createCompanionCoordinator } from "./companion";

function harness() {
  let callbacks: { onStart?: () => void; onEnd?: () => void; onError?: () => void }[] = [];
  const adapter = {
    speak: vi.fn((_text: string, options: (typeof callbacks)[number]) => {
      callbacks.push(options);
      options.onStart?.();
    }),
    stop: vi.fn(),
  };
  let timestamp = 1000;
  const coordinator = createCompanionCoordinator(adapter, () => timestamp);
  return {
    adapter,
    coordinator,
    callbacks,
    advance(ms: number) {
      timestamp += ms;
    },
  };
}

describe("companion coordinator", () => {
  it("serializes equal and lower priority speech", () => {
    const test = harness();
    test.coordinator.speak({ channel: "task_narration", text: "Starting action" });
    test.coordinator.speak({ channel: "background_insight", text: "Background idea" });

    expect(test.adapter.speak).toHaveBeenCalledTimes(1);
    test.callbacks[0].onEnd?.();
    expect(test.adapter.speak).toHaveBeenCalledTimes(2);
    expect(test.adapter.speak.mock.calls[1][0]).toBe("Background idea");
  });

  it("preempts narration with a risk warning and drops stale lower-priority speech", () => {
    const test = harness();
    test.coordinator.speak({
      channel: "task_narration",
      text: "Starting action",
      taskId: "task-1",
    });
    test.coordinator.speak({
      channel: "background_insight",
      text: "Later idea",
    });
    test.coordinator.speak({
      channel: "approval_risk",
      text: "Approval is required",
    });

    expect(test.adapter.stop).toHaveBeenCalledOnce();
    expect(test.adapter.speak).toHaveBeenCalledTimes(2);
    expect(test.adapter.speak.mock.calls[1][0]).toBe("Approval is required");
    let state: any;
    test.coordinator.subscribe((value) => (state = value))();
    expect(state.queued).toBe(0);
    expect(state.preemptionCount).toBe(1);
  });

  it("suppresses duplicate messages from separate producers", () => {
    const test = harness();

    expect(
      test.coordinator.speak({
        channel: "approval_risk",
        text: "This action needs approval.",
        dedupeKey: "plan-1",
      }),
    ).toBe(true);
    expect(
      test.coordinator.speak({
        channel: "approval_risk",
        text: "A duplicated rendering of the warning.",
        dedupeKey: "plan-1",
      }),
    ).toBe(false);

    expect(test.adapter.speak).toHaveBeenCalledTimes(1);
  });

  it("gives human speech ownership and clears stale queued output", () => {
    const test = harness();
    test.coordinator.speak({ channel: "task_narration", text: "Working" });
    test.coordinator.speak({ channel: "background_insight", text: "Queued idea" });

    test.coordinator.humanSpeechStarted();

    expect(test.adapter.stop).toHaveBeenCalledOnce();
    let state: any;
    test.coordinator.subscribe((value) => (state = value))();
    expect(state.humanSpeaking).toBe(true);
    expect(state.queued).toBe(0);
    expect(state.activeChannel).toBe("");
  });

  it("allows an emergency stop warning to outrank human speech", () => {
    const test = harness();
    test.coordinator.humanSpeechStarted();

    test.coordinator.speak({
      channel: "emergency_stop",
      text: "Emergency stop activated.",
    });

    expect(test.adapter.speak).toHaveBeenCalledOnce();
    expect(test.adapter.speak.mock.calls[0][0]).toBe("Emergency stop activated.");
  });
});
