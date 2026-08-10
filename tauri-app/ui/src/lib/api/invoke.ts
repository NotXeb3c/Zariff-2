import uiPackage from "../../../package.json";
import { invoke as tauriInvoke } from "@tauri-apps/api/core";

const HELIOX_VERSION = uiPackage.version;

export async function invoke<T = any>(command: string, args?: any): Promise<T> {
  // First check if native Tauri IPC bridge is present
  if (typeof window !== "undefined" && (window as any).__TAURI_INTERNALS__) {
    try {
      return await tauriInvoke<T>(command, args);
    } catch (e) {
      console.error(`Tauri native invoke error (${command}):`, e);
      throw e;
    }
  }

  // System-wide shortcuts are a native desktop capability. Keep browser
  // development truthful instead of returning an empty object and pretending
  // that a shortcut was registered.
  if (command === "get_hotkey") {
    return "Ctrl+Space" as unknown as T;
  }
  if (command === "set_hotkey") {
    throw new Error("Global hotkeys are only available in the Zariff desktop app.");
  }

  // Fallback for browser dev mode (npm run dev running in standard Chrome/Edge)
  try {
    const res = await fetch("/api/tauri_invoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command, args }),
    });
    if (res.ok) {
      return await res.json();
    }
  } catch (e) {
    console.error(`Dev server fallback error (${command}):`, e);
  }

  // Return safe defaults if dev server proxy returns error
  if (command === "get_system_stats") {
    return {
      cpu: 12,
      ram: 44,
      disk: 38,
      network_up: 84,
      network_down: 312,
      cpu_name: "Local CPU (Dev Mode)",
      total_ram: 16,
      disk_size: 512,
    } as unknown as T;
  }
  if (command === "get_temperature_stats") {
    return {
      cpu: 44,
      gpu: 40,
      motherboard: 36,
      ssd: 34,
      vrm: 33,
      battery: 29,
      power: 52,
      cpu_name: "Local CPU",
      cpu_threads: 16,
      battery_percent: 95,
    } as unknown as T;
  }
  if (command === "get_uptime") {
    return "4h 12m" as unknown as T;
  }
  if (command === "get_log_count") {
    return 128 as unknown as T;
  }
  if (command === "get_terminal_logs") {
    return [
      "[System] Zariff Agent Daemon Connected",
      "[Core] Direct interaction loop active on ws://127.0.0.1:8785",
      "[Monitor] System health metrics normal",
      "[Cognitive] Cognitive HUD loaded",
    ] as unknown as T;
  }
  if (command === "get_agent_activity") {
    return [
      { name: "System Agent", status: "Active", message: "Monitoring system health" },
      { name: "Code Agent", status: "Idle", message: "Waiting for a coding task" },
      { name: "Web Agent", status: "Idle", message: "Waiting for a browser task" },
      { name: "Monitor Agent", status: "Active", message: "Watching resource usage" },
      { name: "Communication Agent", status: "Idle", message: "Ready" },
    ] as unknown as T;
  }
  if (command === "get_rss_feed") {
    return [
      {
        title: `Zariff v${HELIOX_VERSION} Current Build`,
        url: "https://github.com/VyomKulshrestha/Heliox-OS/releases",
        source: "Current Build",
      },
    ] as unknown as T;
  }
  if (command === "get_status_metrics") {
    return { cpu: 12, ram: 44, latency_ms: 8, agents_active: 5 } as unknown as T;
  }
  if (command === "get_dashboard_status") {
    return {
      connected: true,
      agents: 5,
      cpu: "15%",
      memory: "44%",
      network_up: "140 KB/s",
      network_down: "520 KB/s",
    } as unknown as T;
  }
  if (command === "get_auth_token") {
    try {
      const res = await fetch("/api/auth_token");
      if (res.ok) return (await res.text()).trim() as unknown as T;
    } catch {
      // ignore
    }
    return ((import.meta as any).env?.VITE_DAEMON_TOKEN ?? "") as unknown as T;
  }
  return {} as unknown as T;
}
