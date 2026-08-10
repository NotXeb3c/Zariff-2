import { describe, expect, it } from "vitest";
import { defaultGestureAction } from "./actionPolicy";

describe("defaultGestureAction", () => {
  it("keeps stop and pending-approval controls built in", () => {
    expect(defaultGestureAction("palm")).toBe("abort");
    expect(defaultGestureAction("middle_finger")).toBe("abort");
    expect(defaultGestureAction("thumbs_up")).toBe("confirm");
    expect(defaultGestureAction("palm_push")).toBe("confirm");
    expect(defaultGestureAction("thumbs_down")).toBe("deny");
    expect(defaultGestureAction("palm_pull")).toBe("deny");
  });

  it.each([
    "finger_gun",
    "vulcan",
    "crossed_fingers",
    "snap_ready",
    "devil_horns",
    "palm_down",
    "palm_up",
    "three_up",
    "four_up",
    "circular_cw",
    "circular_ccw",
    "two_finger_swipe_left",
    "two_finger_swipe_right",
  ])("does not let unbound %s create an OS task", (gesture) => {
    expect(defaultGestureAction(gesture)).toBe("unbound");
  });
});
