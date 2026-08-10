import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(),
}));
vi.mock("./invoke", () => ({
  invoke: vi.fn(),
}));

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  send(raw: string) {
    const request = JSON.parse(raw);
    if (request.method === "auth") {
      queueMicrotask(() =>
        this.onmessage?.({
          data: JSON.stringify({
            jsonrpc: "2.0",
            id: request.id,
            result: { status: "authenticated" },
          }),
        }),
      );
    }
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

describe("daemon websocket interruption", () => {
  beforeEach(() => {
    vi.resetModules();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        text: async () => "local-auth-token",
      }),
    );
  });

  it("rejects in-flight calls immediately when the socket closes", async () => {
    const daemon = await import("./daemon");
    await expect(daemon.connect()).resolves.toBe(true);

    const inFlight = daemon.call("execute", { input: "long task" });
    FakeWebSocket.instances[0].close();

    await expect(inFlight).rejects.toThrow("Daemon connection was interrupted");
    daemon.disconnect();
  });

  it("publishes authenticated and disconnected connection states", async () => {
    const daemon = await import("./daemon");
    const states: boolean[] = [];
    daemon.onConnectionState((connected) => states.push(connected));

    await daemon.connect();
    FakeWebSocket.instances[0].close();

    expect(states).toEqual([true, false]);
    daemon.disconnect();
  });
});
