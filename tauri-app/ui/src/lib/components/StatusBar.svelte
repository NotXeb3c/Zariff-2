<script lang="ts">
  import { invoke } from "../api/invoke";
  import { isConnected } from "../api/daemon";
  import { onMount } from "svelte";
  type DashboardStatus = {
    connected: boolean;
    agents: number;
    cpu: string;
    memory: string;
    network_up: string;
    network_down: string;
  };
  let status: DashboardStatus = {
    connected: false,
    agents: 0,
    cpu: "0%",
    memory: "0%",
    network_up: "0 KB/s",
    network_down: "0 KB/s",
  };
  async function loadStatus() {
    try {
      const res = await invoke("get_dashboard_status");
      if (res && typeof res === "object") {
        status = { ...res, connected: isConnected() };
      }
    } catch (err) {
      console.error("Dashboard status failed", err);
    }
  }
  onMount(() => {
    loadStatus();
    const interval = setInterval(loadStatus, 2000);
    return () => clearInterval(interval);
  });
</script>

<div class="status-wrapper">
  <div class="status-bar">
    <div class="scroll-content">
      <div class="status-item">
        <span class="label"> JSON-RPC </span>
        <div class:connected={status.connected} class="badge">
          ●
          {status.connected ? "Connected" : "Disconnected"}
        </div>
      </div>
      <div class="status-item">
        <span class="label"> Agents </span>
        <div class="badge active">
          +{status.agents} Active
        </div>
      </div>
      <div class="status-item">
        <span class="label"> CPU Load </span>
        <div class="badge cpu">
          {status.cpu}
        </div>
      </div>
      <div class="status-item">
        <span class="label"> Memory </span>
        <div class="badge memory">
          ● {status.memory}
        </div>
      </div>
      <div class="status-item network">
        <span class="label"> Network </span>
        <div class="network-stats">
          ↑ {status.network_up}
          ↓ {status.network_down}
        </div>
      </div>
      <div class="dashboard-title">Zariff Dashboard • Real-time Agentic System Monitor</div>
    </div>
  </div>
</div>

<style>
  .status-wrapper {
    width: 100%;
    overflow: hidden;
    border-radius: 24px;
    margin-top: 26px;
  }
  .status-bar {
    width: 100%;
    padding: 20px 28px;
    border-radius: 24px;
    background:
      linear-gradient(120deg, rgba(0, 229, 255, 0.06), transparent 40%),
      linear-gradient(300deg, rgba(255, 46, 166, 0.06), transparent 40%),
      #0a0e22;
    border: 1px solid rgba(0, 229, 255, 0.22);
    box-shadow:
      0 0 24px rgba(0, 229, 255, 0.12),
      0 0 60px rgba(160, 107, 255, 0.08),
      inset 0 1px 0 rgba(255, 255, 255, 0.04);
    color: white;
    overflow: hidden;
  }
  .scroll-content {
    display: flex;
    align-items: center;
    gap: 40px;
    width: max-content;
    animation: scrollStatus 14s linear infinite;
  }
  @keyframes scrollStatus {
    0% {
      transform: translateX(100%);
    }
    100% {
      transform: translateX(-100%);
    }
  }
  .status-item {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-shrink: 0;
  }
  .label {
    font-size: 15px;
    color: #cbd5e1;
    white-space: nowrap;
  }
  .badge {
    padding: 8px 16px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.06);
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
  }
  .badge.connected {
    background: rgba(0, 255, 170, 0.12);
    color: #00ffae;
    border: 1px solid rgba(0, 255, 170, 0.35);
  }
  .badge.active {
    background: rgba(0, 255, 170, 0.12);
    color: #00ffae;
  }
  .badge.cpu {
    background: rgba(0, 255, 170, 0.12);
    color: #00ffae;
  }
  .badge.memory {
    background: rgba(139, 92, 246, 0.14);
    color: #b07cff;
  }
  .network {
    gap: 12px;
  }
  .network-stats {
    color: #e2e8f0;
    font-size: 15px;
    white-space: nowrap;
  }
  .dashboard-title {
    color: #cbd5e1;
    font-size: 15px;
    white-space: nowrap;
    padding-right: 40px;
    background: linear-gradient(90deg, #00e5ff, #a06bff, #ff2ea6);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    font-weight: 700;
    letter-spacing: 0.04em;
    filter: drop-shadow(0 0 8px rgba(0, 229, 255, 0.35));
  }

  :global(html.light-mode) .dashboard-title {
    background: none;
    -webkit-background-clip: initial;
    background-clip: initial;
    color: var(--text-primary);
    filter: none;
  }
</style>
