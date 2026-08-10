<script lang="ts">
  import { onMount } from "svelte";
  import { _ } from "svelte-i18n";
  import { call } from "../api/daemon";

  let enabled = $state(false);
  let narrateSteps = $state(true);
  let interruptOnRisk = $state(true);
  let proactiveReviewEnabled = $state(true);
  let liveCorrectionsEnabled = $state(true);
  let followUpEnabled = $state(true);
  let confirmTimeoutSeconds = $state(120);
  let advisoryTimeoutSeconds = $state(5);
  let loading = $state(true);
  let saving = $state(false);
  let saved = $state(false);
  let learnedPatternCount = $state(0);
  let suggestionShownCount = $state(0);
  let suggestionAcceptedCount = $state(0);
  let suggestionDismissedCount = $state(0);
  let resettingLearning = $state(false);

  onMount(() => {
    void loadStatus();
    void loadLearningStatus();
  });

  async function loadStatus() {
    try {
      const result = (await call("narration_status")) as {
        enabled: boolean;
        narrate_steps: boolean;
        interrupt_on_risk: boolean;
        proactive_review_enabled: boolean;
        live_corrections_enabled: boolean;
        follow_up_enabled: boolean;
        confirm_timeout_seconds: number;
        advisory_timeout_seconds: number;
      };
      enabled = result.enabled ?? false;
      narrateSteps = result.narrate_steps ?? true;
      interruptOnRisk = result.interrupt_on_risk ?? true;
      proactiveReviewEnabled = result.proactive_review_enabled ?? true;
      liveCorrectionsEnabled = result.live_corrections_enabled ?? true;
      followUpEnabled = result.follow_up_enabled ?? true;
      confirmTimeoutSeconds = result.confirm_timeout_seconds ?? 120;
      advisoryTimeoutSeconds = result.advisory_timeout_seconds ?? 5;
    } catch {
      /* daemon unreachable -- keep last known state */
    } finally {
      loading = false;
    }
  }

  async function save() {
    saving = true;
    saved = false;
    try {
      await call("narration_config_update", {
        enabled,
        narrate_steps: narrateSteps,
        interrupt_on_risk: interruptOnRisk,
        proactive_review_enabled: proactiveReviewEnabled,
        live_corrections_enabled: liveCorrectionsEnabled,
        follow_up_enabled: followUpEnabled,
        confirm_timeout_seconds: confirmTimeoutSeconds,
        advisory_timeout_seconds: advisoryTimeoutSeconds,
      });
      saved = true;
      setTimeout(() => (saved = false), 2500);
    } finally {
      saving = false;
    }
  }

  async function loadLearningStatus() {
    try {
      const result = (await call("proactive_learning_status")) as {
        patterns?: Record<string, { shown?: number; accepted?: number; dismissed?: number }>;
      };
      const patterns = Object.values(result.patterns ?? {});
      learnedPatternCount = patterns.length;
      suggestionShownCount = patterns.reduce((sum, pattern) => sum + Number(pattern.shown ?? 0), 0);
      suggestionAcceptedCount = patterns.reduce((sum, pattern) => sum + Number(pattern.accepted ?? 0), 0);
      suggestionDismissedCount = patterns.reduce((sum, pattern) => sum + Number(pattern.dismissed ?? 0), 0);
    } catch {
      /* daemon unreachable -- leave learning counters unchanged */
    }
  }

  async function resetLearning() {
    resettingLearning = true;
    try {
      await call("online_learning_reset");
      await loadLearningStatus();
    } finally {
      resettingLearning = false;
    }
  }
</script>

<div class="narration-panel">
  <div class="narration-header">
    <h3>{$_("settings.narration")}</h3>
    <button
      class="toggle"
      class:active={enabled}
      onclick={() => (enabled = !enabled)}
      aria-label="Toggle Live Execution Narrator"
      title="Toggle Live Execution Narrator"
    >
      <span class="toggle-knob"></span>
    </button>
  </div>

  <p class="narration-note">{$_("settings.narration_desc")}</p>

  {#if loading}
    <div class="empty">Loading...</div>
  {:else}
    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_narrate_steps")}</span>
        <span class="setting-desc">{$_("settings.narration_narrate_steps_desc")}</span>
      </div>
      <button
        class="toggle toggle-sm"
        class:active={narrateSteps}
        onclick={() => (narrateSteps = !narrateSteps)}
        aria-label="Toggle step narration"
      >
        <span class="toggle-knob"></span>
      </button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_interrupt_on_risk")}</span>
        <span class="setting-desc">{$_("settings.narration_interrupt_on_risk_desc")}</span>
      </div>
      <button
        class="toggle toggle-sm"
        class:active={interruptOnRisk}
        onclick={() => (interruptOnRisk = !interruptOnRisk)}
        aria-label="Toggle risk interrupts"
      >
        <span class="toggle-knob"></span>
      </button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_proactive_review")}</span>
        <span class="setting-desc">{$_("settings.narration_proactive_review_desc")}</span>
      </div>
      <button
        class="toggle toggle-sm"
        class:active={proactiveReviewEnabled}
        onclick={() => (proactiveReviewEnabled = !proactiveReviewEnabled)}
        aria-label="Toggle proactive companion review"
      >
        <span class="toggle-knob"></span>
      </button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_live_corrections")}</span>
        <span class="setting-desc">{$_("settings.narration_live_corrections_desc")}</span>
      </div>
      <button
        class="toggle toggle-sm"
        class:active={liveCorrectionsEnabled}
        onclick={() => (liveCorrectionsEnabled = !liveCorrectionsEnabled)}
        aria-label="Toggle live task corrections"
      >
        <span class="toggle-knob"></span>
      </button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_follow_up")}</span>
        <span class="setting-desc">{$_("settings.narration_follow_up_desc")}</span>
      </div>
      <button
        class="toggle toggle-sm"
        class:active={followUpEnabled}
        onclick={() => (followUpEnabled = !followUpEnabled)}
        aria-label="Toggle grounded companion follow-ups"
      >
        <span class="toggle-knob"></span>
      </button>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_timeout")}</span>
        <span class="setting-desc">{$_("settings.narration_timeout_desc")}</span>
      </div>
      <input
        type="number"
        class="input-sm"
        value={confirmTimeoutSeconds}
        onchange={(e) => (confirmTimeoutSeconds = Number((e.target as HTMLInputElement).value))}
        min="10"
        max="600"
        step="10"
      />
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.narration_advisory_timeout")}</span>
        <span class="setting-desc">{$_("settings.narration_advisory_timeout_desc")}</span>
      </div>
      <input
        type="number"
        class="input-sm"
        value={advisoryTimeoutSeconds}
        onchange={(e) => (advisoryTimeoutSeconds = Number((e.target as HTMLInputElement).value))}
        min="1"
        max="30"
        step="1"
      />
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <span class="setting-label">{$_("settings.adaptive_learning")}</span>
        <span class="setting-desc">
          {$_("settings.adaptive_learning_desc", {
            values: {
              patterns: learnedPatternCount,
              shown: suggestionShownCount,
              accepted: suggestionAcceptedCount,
              dismissed: suggestionDismissedCount,
            },
          })}
        </span>
      </div>
      <button class="btn-reset" onclick={resetLearning} disabled={resettingLearning || learnedPatternCount === 0}>
        {resettingLearning ? $_("settings.resetting") : $_("settings.reset_learning")}
      </button>
    </div>

    <div class="narration-actions">
      <button class="btn-save" onclick={save} disabled={saving}>
        {saving ? "Saving..." : saved ? "✓ Saved" : $_("settings.save")}
      </button>
    </div>
  {/if}
</div>

<style>
  .narration-panel {
    display: flex;
    flex-direction: column;
  }

  .narration-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: var(--bg-tertiary);
    border-bottom: 1px solid var(--border);
  }

  h3 {
    font-size: 14px;
    font-weight: 600;
    margin: 0;
  }

  .narration-note {
    margin: 10px 14px 0;
    padding: 10px 12px;
    font-size: 11px;
    line-height: 1.4;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    border-radius: var(--radius-sm);
  }

  .empty {
    padding: 20px;
    text-align: center;
    color: var(--text-muted);
    font-size: 13px;
  }

  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }

  .setting-info {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 2px;
  }

  .setting-label {
    font-size: 13px;
    font-weight: 500;
  }

  .setting-desc {
    font-size: 11px;
    color: var(--text-muted);
  }

  .toggle {
    position: relative;
    width: 40px;
    height: 22px;
    flex-shrink: 0;
    cursor: pointer;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 11px;
    transition: all 0.2s;
  }

  .toggle.active {
    background: var(--accent);
    border-color: var(--accent);
  }

  .toggle-knob {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle.active .toggle-knob {
    transform: translateX(18px);
  }

  .toggle-sm {
    transform: scale(0.8);
  }

  .input-sm {
    width: 80px;
    padding: 5px 8px;
    font-size: 13px;
    color: var(--text-primary);
    text-align: right;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .narration-actions {
    display: flex;
    justify-content: flex-end;
    padding: 10px 14px 12px;
  }

  .btn-save {
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 600;
    color: white;
    white-space: nowrap;
    background: var(--accent);
    border-radius: var(--radius-sm);
    transition: all 0.15s;
  }

  .btn-save:hover:not(:disabled) {
    background: var(--accent-hover);
  }

  .btn-save:disabled {
    cursor: not-allowed;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
  }

  .btn-reset {
    flex-shrink: 0;
    padding: 5px 10px;
    font-size: 11px;
    color: var(--text-secondary);
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .btn-reset:hover:not(:disabled) {
    color: var(--text-primary);
    border-color: var(--accent);
  }

  .btn-reset:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
</style>
