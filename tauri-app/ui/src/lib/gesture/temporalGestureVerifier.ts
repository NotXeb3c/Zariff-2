import type { Landmark } from "./spatialModel";

export interface TemporalGestureInput {
  mediaPipeHandPresent: boolean;
  landmarks: Landmark[] | null;
  worldLandmarks?: Landmark[] | null;
  candidate: string;
  timestampMs: number;
}

export interface TemporalGestureVerification {
  accepted: boolean;
  confidenceMultiplier: number;
  continuity: number;
  observedFrames: number;
  reason: "verified" | "no_hand" | "invalid_hand" | "warming_up" | "discontinuous_motion";
}

interface TemporalFrame {
  signature: number[];
  timestampMs: number;
}

const SIGNATURE_LANDMARKS = [0, 4, 5, 8, 9, 12, 13, 16, 17, 20];
const MIN_FRAMES = 3;
const MAX_FRAMES = 8;
const MAX_NORMALIZED_STEP = 0.9;

function finiteHand(landmarks: Landmark[] | null): landmarks is Landmark[] {
  return Boolean(
    landmarks &&
    landmarks.length >= 21 &&
    landmarks.every((point) => Number.isFinite(point.x) && Number.isFinite(point.y) && Number.isFinite(point.z ?? 0)),
  );
}

function distance(a: Landmark, b: Landmark): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  const dz = (a.z ?? 0) - (b.z ?? 0);
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

function signature(landmarks: Landmark[]): number[] {
  const wrist = landmarks[0];
  const scale = Math.max(distance(wrist, landmarks[9]), 1e-4);
  return SIGNATURE_LANDMARKS.flatMap((index) => {
    const point = landmarks[index];
    return [(point.x - wrist.x) / scale, (point.y - wrist.y) / scale, ((point.z ?? 0) - (wrist.z ?? 0)) / scale];
  });
}

function meanStep(left: number[], right: number[]): number {
  let squared = 0;
  for (let index = 0; index < left.length; index += 3) {
    const dx = left[index] - right[index];
    const dy = left[index + 1] - right[index + 1];
    const dz = left[index + 2] - right[index + 2];
    squared += dx * dx + dy * dy + dz * dz;
  }
  return Math.sqrt(squared / (left.length / 3));
}

/**
 * JEPA-style temporal support probe over privacy-preserving hand signatures.
 *
 * MediaPipe remains the hand-presence authority. This verifier can only
 * reduce or reject a gesture candidate; it never creates a candidate and
 * never increases confidence.
 */
export class TemporalGestureVerifier {
  private frames: TemporalFrame[] = [];

  observe(input: TemporalGestureInput): TemporalGestureVerification {
    if (!input.mediaPipeHandPresent) {
      this.reset();
      return this.result(false, 0, 0, "no_hand");
    }
    if (!finiteHand(input.landmarks)) {
      this.reset();
      return this.result(false, 0, 0, "invalid_hand");
    }

    // Prefer metric world landmarks when MediaPipe Tasks provides them.
    // The normalized hand remains the fallback for the legacy backend.
    const source = finiteHand(input.worldLandmarks ?? null) ? input.worldLandmarks! : input.landmarks;
    const next: TemporalFrame = {
      signature: signature(source),
      timestampMs: input.timestampMs,
    };
    const previous = this.frames.at(-1);
    let continuity = 1;
    if (previous) {
      const elapsed = input.timestampMs - previous.timestampMs;
      const step = meanStep(previous.signature, next.signature);
      if (elapsed <= 0 || elapsed > 500 || step > MAX_NORMALIZED_STEP) {
        this.frames = [next];
        return this.result(false, 0, 0, "discontinuous_motion");
      }
      continuity = Math.max(0, 1 - step / MAX_NORMALIZED_STEP);
    }

    this.frames.push(next);
    if (this.frames.length > MAX_FRAMES) this.frames.shift();
    if (this.frames.length < MIN_FRAMES) {
      return this.result(false, 0, continuity, "warming_up");
    }

    // Supporting evidence only: 0.65..1.0, never above the classifier's
    // original confidence.
    const multiplier = Math.min(1, 0.65 + continuity * 0.35);
    return this.result(true, multiplier, continuity, "verified");
  }

  reset(): void {
    this.frames = [];
  }

  private result(
    accepted: boolean,
    confidenceMultiplier: number,
    continuity: number,
    reason: TemporalGestureVerification["reason"],
  ): TemporalGestureVerification {
    return {
      accepted,
      confidenceMultiplier,
      continuity,
      observedFrames: this.frames.length,
      reason,
    };
  }
}
