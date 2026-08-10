import { describe, expect, it } from "vitest";
import type { Landmark } from "./spatialModel";
import { TemporalGestureVerifier } from "./temporalGestureVerifier";

function hand(offset = 0): Landmark[] {
  return Array.from({ length: 21 }, (_, index) => ({
    x: offset + (index % 5) * 0.02,
    y: Math.floor(index / 5) * 0.025,
    z: (index % 3) * 0.004,
  }));
}

describe("TemporalGestureVerifier", () => {
  it("cannot verify anything without MediaPipe hand presence", () => {
    const verifier = new TemporalGestureVerifier();

    const result = verifier.observe({
      mediaPipeHandPresent: false,
      landmarks: hand(),
      candidate: "thumbs_up",
      timestampMs: 0,
    });

    expect(result.accepted).toBe(false);
    expect(result.reason).toBe("no_hand");
    expect(result.confidenceMultiplier).toBe(0);
  });

  it("rejects incomplete or non-finite hand geometry", () => {
    const verifier = new TemporalGestureVerifier();
    const invalid = hand();
    invalid[8] = { x: Number.NaN, y: 0, z: 0 };

    const result = verifier.observe({
      mediaPipeHandPresent: true,
      landmarks: invalid,
      candidate: "palm",
      timestampMs: 0,
    });

    expect(result.reason).toBe("invalid_hand");
  });

  it("accepts a continuous hand only after temporal warmup", () => {
    const verifier = new TemporalGestureVerifier();
    const results = [0, 33, 66].map((timestampMs, index) =>
      verifier.observe({
        mediaPipeHandPresent: true,
        landmarks: hand(index * 0.001),
        candidate: "thumbs_up",
        timestampMs,
      }),
    );

    expect(results.map((result) => result.accepted)).toEqual([false, false, true]);
    expect(results[2].confidenceMultiplier).toBeGreaterThan(0.65);
    expect(results[2].confidenceMultiplier).toBeLessThanOrEqual(1);
  });

  it("rejects an implausible landmark jump and starts warming up again", () => {
    const verifier = new TemporalGestureVerifier();
    verifier.observe({
      mediaPipeHandPresent: true,
      landmarks: hand(),
      candidate: "palm",
      timestampMs: 0,
    });

    const distorted = hand();
    distorted[8] = { x: 8, y: -8, z: 4 };
    const result = verifier.observe({
      mediaPipeHandPresent: true,
      landmarks: distorted,
      candidate: "palm",
      timestampMs: 33,
    });

    expect(result.accepted).toBe(false);
    expect(result.reason).toBe("discontinuous_motion");
    expect(result.observedFrames).toBe(1);
  });

  it("resets temporal evidence as soon as MediaPipe loses the hand", () => {
    const verifier = new TemporalGestureVerifier();
    for (const timestampMs of [0, 33, 66]) {
      verifier.observe({
        mediaPipeHandPresent: true,
        landmarks: hand(),
        candidate: "ok",
        timestampMs,
      });
    }

    verifier.observe({
      mediaPipeHandPresent: false,
      landmarks: null,
      candidate: "",
      timestampMs: 99,
    });
    const reacquired = verifier.observe({
      mediaPipeHandPresent: true,
      landmarks: hand(),
      candidate: "ok",
      timestampMs: 132,
    });

    expect(reacquired.accepted).toBe(false);
    expect(reacquired.reason).toBe("warming_up");
    expect(reacquired.observedFrames).toBe(1);
  });
});
