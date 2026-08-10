/**
 * Runs at most one asynchronous dispatch at a time while retaining only the
 * newest queued value.
 *
 * Camera frames arrive faster than the browser-mode cursor RPC can complete.
 * Sending every frame creates an ever-growing FIFO of stale cursor positions,
 * so the pointer appears frozen or trails the hand by seconds. This dispatcher
 * provides backpressure without adding latency: intermediate positions are
 * discarded and the newest position is sent as soon as the active call ends.
 */
export class LatestAsyncDispatcher<T> {
  private running = false;
  private generation = 0;
  private pending: { generation: number; value: T } | null = null;

  constructor(private readonly dispatch: (value: T) => Promise<void>) {}

  enqueue(value: T): void {
    this.pending = { generation: this.generation, value };
    if (!this.running) void this.drain();
  }

  /**
   * Drops queued work. An already-running call cannot be cancelled, but its
   * completion will not dispatch values queued before this reset.
   */
  reset(): void {
    this.generation++;
    this.pending = null;
  }

  private async drain(): Promise<void> {
    if (this.running) return;
    this.running = true;

    try {
      while (this.pending) {
        const next = this.pending;
        this.pending = null;
        if (next.generation !== this.generation) continue;

        try {
          await this.dispatch(next.value);
        } catch {
          // The owner reports transport errors. Drop the current backlog so a
          // later camera frame can perform a clean, throttled retry.
          this.pending = null;
          break;
        }
      }
    } finally {
      this.running = false;
      if (this.pending) void this.drain();
    }
  }
}
