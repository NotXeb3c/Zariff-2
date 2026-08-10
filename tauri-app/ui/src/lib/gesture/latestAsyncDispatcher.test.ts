import { describe, expect, it, vi } from "vitest";

import { LatestAsyncDispatcher } from "./latestAsyncDispatcher";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

describe("LatestAsyncDispatcher", () => {
  it("keeps one request in flight and coalesces queued values to the latest", async () => {
    const first = deferred();
    const dispatch = vi.fn((value: number) => (value === 1 ? first.promise : Promise.resolve()));
    const dispatcher = new LatestAsyncDispatcher(dispatch);

    dispatcher.enqueue(1);
    dispatcher.enqueue(2);
    dispatcher.enqueue(3);

    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenNthCalledWith(1, 1);

    first.resolve();
    await flushPromises();

    expect(dispatch).toHaveBeenCalledTimes(2);
    expect(dispatch).toHaveBeenNthCalledWith(2, 3);
  });

  it("drops queued work after reset", async () => {
    const first = deferred();
    const dispatch = vi.fn(() => first.promise);
    const dispatcher = new LatestAsyncDispatcher(dispatch);

    dispatcher.enqueue("current");
    dispatcher.enqueue("stale");
    dispatcher.reset();
    first.resolve();
    await flushPromises();

    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledWith("current");
  });

  it("drops the backlog after a failed dispatch and accepts a later retry", async () => {
    const dispatch = vi
      .fn<(value: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);
    const dispatcher = new LatestAsyncDispatcher(dispatch);

    dispatcher.enqueue("failed");
    dispatcher.enqueue("stale");
    await flushPromises();
    dispatcher.enqueue("retry");
    await flushPromises();

    expect(dispatch).toHaveBeenCalledTimes(2);
    expect(dispatch).toHaveBeenNthCalledWith(1, "failed");
    expect(dispatch).toHaveBeenNthCalledWith(2, "retry");
  });
});
