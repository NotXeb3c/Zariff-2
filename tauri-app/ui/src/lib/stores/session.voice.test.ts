import { get, writable } from "svelte/store";
import { beforeEach, describe, expect, it, vi } from "vitest";

type NotificationHandler = (method: string, params: unknown) => void;
let notificationHandler: NotificationHandler | null = null;

const daemonMocks = vi.hoisted(() => ({
  call: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../api/daemon", () => ({
  call: daemonMocks.call,
  connect: vi.fn().mockResolvedValue(true),
  isConnected: vi.fn(() => true),
  onConnectionState: vi.fn(),
  onNotification: (handler: NotificationHandler) => {
    notificationHandler = handler;
  },
}));

vi.mock("./settings", () => ({
  settings: writable({ model: { cloud_model: "ollama" } }),
}));

vi.mock("./companion", () => ({
  companion: { speak: vi.fn() },
}));

vi.mock("@tauri-apps/plugin-notification", () => ({
  isPermissionGranted: vi.fn().mockResolvedValue(false),
  requestPermission: vi.fn().mockResolvedValue("denied"),
  sendNotification: vi.fn(),
}));

describe("voice command session notifications", () => {
  it("tracks the daemon's unified interaction state", async () => {
    const { session } = await import("./session");

    notificationHandler!("interaction_state", {
      interaction_id: "interaction-1",
      source: "voice",
      phase: "planning",
      message: "Planning the safest useful action",
      active: true,
      elapsed_ms: 42,
      sequence: 2,
    });

    expect(get(session)).toMatchObject({
      phase: "Planning the safest useful action",
      interaction: {
        interactionId: "interaction-1",
        source: "voice",
        phase: "planning",
        active: true,
        elapsedMs: 42,
        sequence: 2,
      },
    });
  });

  beforeEach(() => {
    localStorage.clear();
    notificationHandler = null;
    daemonMocks.call.mockClear();
    vi.resetModules();
  });

  it("renders the spoken command and its terminal result", async () => {
    const { session } = await import("./session");

    notificationHandler!("voice_command", {
      command: "show system information",
      status: "executing",
    });

    let state = get(session);
    expect(state.loading).toBe(true);
    expect(state.messages.at(-1)).toMatchObject({
      type: "user",
      text: "show system information",
    });

    notificationHandler!("voice_result", {
      command: "show system information",
      status: "success",
      result: "Windows 11, 32 GB RAM",
    });

    state = get(session);
    expect(state.loading).toBe(false);
    expect(state.currentPlan).toBeNull();
    expect(state.terminalStatus).toBe("success");
    expect(state.messages.at(-1)).toMatchObject({
      type: "result",
      text: "Windows 11, 32 GB RAM",
    });
  });

  it("maps a partial voice result to a visible failure state", async () => {
    const { session } = await import("./session");

    notificationHandler!("voice_result", {
      command: "inspect the system",
      status: "partial",
      result: "One action could not be verified.",
    });

    const state = get(session);
    expect(state.loading).toBe(false);
    expect(state.terminalStatus).toBe("partial_failure");
    expect(state.messages.at(-1)).toMatchObject({
      type: "error",
      text: "One action could not be verified.",
    });
  });

  it("adds optional companion ideas after the terminal result", async () => {
    const { session } = await import("./session");

    notificationHandler!("voice_result", {
      command: "show system information",
      status: "success",
      result: "Windows 11",
    });
    notificationHandler!("companion_follow_up", {
      message: "The system report is ready.",
      suggestions: ["Compare it with the app requirements", "Save a baseline"],
    });

    const state = get(session);
    expect(state.messages.at(-2)).toMatchObject({ type: "result", text: "Windows 11" });
    expect(state.messages.at(-1)).toMatchObject({
      type: "assistant",
      text: "The system report is ready.\n\nPossible next steps:\n- Compare it with the app requirements\n- Save a baseline",
    });
  });
});
