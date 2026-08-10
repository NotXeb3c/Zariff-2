import { describe, expect, it, vi } from "vitest";
import { acquireCurrentMicrophone } from "./audioCapture";

describe("acquireCurrentMicrophone", () => {
  it("discards a stream when its pending request is no longer current", async () => {
    let resolveStream!: (stream: MediaStream) => void;
    const requestStream = vi.fn(
      () =>
        new Promise<MediaStream>((resolve) => {
          resolveStream = resolve;
        }),
    );
    const stop = vi.fn();
    const stream = {
      getTracks: () => [{ stop }],
    } as unknown as MediaStream;
    let current = true;

    const pending = acquireCurrentMicrophone(requestStream, () => current);
    current = false;
    resolveStream(stream);

    await expect(pending).resolves.toBeNull();
    expect(stop).toHaveBeenCalledOnce();
  });

  it("returns the stream while its request is current", async () => {
    const stream = { getTracks: vi.fn() } as unknown as MediaStream;

    await expect(
      acquireCurrentMicrophone(
        async () => stream,
        () => true,
      ),
    ).resolves.toBe(stream);
  });
});
