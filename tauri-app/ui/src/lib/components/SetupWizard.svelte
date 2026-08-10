<script lang="ts">
  import { settings } from "../stores/settings";
  import { call } from "../api/daemon";
  import { onMount } from "svelte";

  interface Props {
    oncomplete: () => void | Promise<void>;
  }

  let { oncomplete }: Props = $props();
  let finishing = $state(false);
  let finishError = $state("");
  let step = $state(0);

  let modelProvider = $state("ollama");
  let ollamaModel = $state("");
  let ollamaModels = $state<string[]>([]);
  let ollamaAvailable = $state(false);
  let loadingModels = $state(true);
  let cloudProvider = $state("");
  let cloudApiKey = $state("");
  let protectedFolders = $state("");
  let protectedPackages = $state("firefox, nautilus");

  const steps = ["Welcome", "Model", "Security", "Ready"];

  onMount(async () => {
    try {
      const result = (await call("list_ollama_models")) as { models: string[]; available: boolean };
      ollamaModels = result.models ?? [];
      ollamaAvailable = result.available ?? false;
      if (ollamaModels.length > 0) {
        ollamaModel = ollamaModels[0];
      }
    } catch {
      ollamaAvailable = false;
    } finally {
      loadingModels = false;
    }
  });

  async function finish() {
    if (finishing) return;
    finishing = true;
    finishError = "";

    try {
      // These now save to localStorage instantly (non-blocking daemon sync)
      await settings.updateSection("model", {
        provider: modelProvider,
        ollama_model: ollamaModel,
        cloud_provider: cloudProvider,
      });

      const folders = protectedFolders
        .split("\n")
        .map((f) => f.trim())
        .filter(Boolean);
      const packages = protectedPackages
        .split(",")
        .map((p) => p.trim())
        .filter(Boolean);

      await settings.updateSection("restrictions", {
        protected_folders: folders,
        protected_packages: packages,
      });

      // Cloud setup is incomplete until the daemon confirms that the key
      // reached a secure operating-system credential store.
      if (cloudApiKey && cloudProvider) {
        const result = await call<{ status: string; message?: string }>("store_api_key", {
          provider: cloudProvider,
          key: cloudApiKey,
        });
        if (result.status !== "ok") {
          throw new Error(result.message || "The API key could not be stored securely.");
        }
      }

      // Mark first run complete in localStorage
      localStorage.setItem("heliox_first_run_complete", "true");

      await oncomplete();
    } catch (error) {
      finishError = error instanceof Error ? error.message : "Setup could not be completed.";
    } finally {
      finishing = false;
    }
  }
</script>

<div class="wizard-overlay">
  <div class="wizard">
    <div class="wizard-header">
      <h1>Zariff Setup</h1>
      <div class="progress">
        {#each steps as s, i}
          <div class="progress-step" class:active={i === step} class:done={i < step}>
            <span class="step-num">{i + 1}</span>
            <span class="step-label">{s}</span>
          </div>
          {#if i < steps.length - 1}
            <div class="progress-line" class:filled={i < step}></div>
          {/if}
        {/each}
      </div>
    </div>

    <div class="wizard-body">
      {#if step === 0}
        <div class="wizard-step">
          <h2>Welcome to Zariff</h2>
          <p>
            Zariff is your AI system control agent. It lets you control your computer using natural language, voice,
            or gestures while keeping you in full control.
          </p>
          <p>This setup will configure a few essentials:</p>
          <ul>
            <li>Choose your AI model backend</li>
            <li>Set security boundaries</li>
            <li>Define protected folders and packages</li>
          </ul>
          <p class="note">You can change all of these later in Settings.</p>
        </div>
      {:else if step === 1}
        <div class="wizard-step">
          <h2>Model Configuration</h2>

          <div class="field" role="group" aria-labelledby="provider-label">
            <span id="provider-label" class="field-label">Primary Provider</span>
            <div class="radio-group">
              <label class="radio-option" class:selected={modelProvider === "ollama"}>
                <input type="radio" bind:group={modelProvider} value="ollama" />
                <div>
                  <strong>Ollama (Local)</strong>
                  <span>Private, runs on your GPU. Requires Ollama to be installed.</span>
                </div>
              </label>
              <label class="radio-option" class:selected={modelProvider === "cloud"}>
                <input type="radio" bind:group={modelProvider} value="cloud" />
                <div>
                  <strong>Cloud API</strong>
                  <span>Uses OpenAI, Claude, or Gemini. Requires API key.</span>
                </div>
              </label>
            </div>
          </div>

          {#if modelProvider === "ollama"}
            <div class="field">
              <label for="ollama-model">Ollama Model</label>
              {#if loadingModels}
                <div class="model-status">Detecting models...</div>
              {:else if ollamaModels.length > 0}
                <select id="ollama-model" bind:value={ollamaModel}>
                  {#each ollamaModels as m}
                    <option value={m}>{m}</option>
                  {/each}
                </select>
                <span class="hint"
                  >{ollamaModels.length} model{ollamaModels.length === 1 ? "" : "s"} detected from Ollama</span
                >
              {:else if ollamaAvailable}
                <input id="ollama-model-input" type="text" bind:value={ollamaModel} placeholder="llama3.1:8b" />
                <span class="hint warning"
                  >Ollama is running but no models found. Run <code>ollama pull qwen2.5:7b</code></span
                >
              {:else}
                <input id="ollama-model-input-2" type="text" bind:value={ollamaModel} placeholder="llama3.1:8b" />
                <span class="hint warning">Ollama is not running. Start it first, or choose Cloud.</span>
              {/if}
            </div>
          {:else}
            <div class="field">
              <label for="cloud-provider">Cloud Provider</label>
              <select id="cloud-provider" bind:value={cloudProvider}>
                <option value="">Select...</option>
                <option value="openai">OpenAI</option>
                <option value="claude">Anthropic (Claude)</option>
                <option value="gemini">Google (Gemini)</option>
              </select>
            </div>
            {#if cloudProvider}
              <div class="field">
                <label for="cloud-api-key">API Key</label>
                <input id="cloud-api-key" type="password" bind:value={cloudApiKey} placeholder="sk-..." />
                <span class="hint"
                  >Stored only in Windows Credential Manager, macOS Keychain, or Linux Secret Service.</span
                >
              </div>
            {/if}
          {/if}
        </div>
      {:else if step === 2}
        <div class="wizard-step">
          <h2>Security Boundaries</h2>

          <div class="field">
            <label for="protected-folders">Protected Folders</label>
            <textarea
              id="protected-folders"
              bind:value={protectedFolders}
              placeholder={"~/Documents/private\n~/ssh"}
              rows={4}></textarea>
            <span class="hint">One path per line. Zariff will never modify files in these folders.</span>
          </div>

          <div class="field">
            <label for="protected-packages">Protected Packages</label>
            <input
              id="protected-packages"
              type="text"
              bind:value={protectedPackages}
              placeholder="firefox, nautilus, gnome-shell"
            />
            <span class="hint">Comma-separated. Zariff will refuse to uninstall these.</span>
          </div>

          <div class="field">
            <label class="checkbox-label">
              <input type="checkbox" checked disabled />
              <span>Root access is <strong>OFF</strong> by default (enable in Settings when needed)</span>
            </label>
          </div>
        </div>
      {:else}
        <div class="wizard-step">
          <h2>All Set</h2>
          <p>Zariff is configured and ready to use.</p>
          <div class="summary">
            <div class="summary-item">
              <span class="summary-label">Provider</span>
              <span>{modelProvider === "ollama" ? `Ollama` : `Cloud (${cloudProvider})`}</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">Model</span>
              <span>{modelProvider === "ollama" ? ollamaModel : cloudProvider}</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">Protected Folders</span>
              <span>{protectedFolders.split("\n").filter(Boolean).length} configured</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">Protected Packages</span>
              <span>{protectedPackages.split(",").filter((p) => p.trim()).length} configured</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">Root Access</span>
              <span>Disabled</span>
            </div>
          </div>
          <p class="note">Press Super+J to toggle the Zariff window at any time.</p>
        </div>
      {/if}
    </div>

    <div class="wizard-footer">
      {#if finishError}
        <p class="finish-error" role="alert">{finishError}</p>
      {/if}
      {#if step > 0}
        <button class="btn-back" onclick={() => step--}>Back</button>
      {:else}
        <div></div>
      {/if}

      {#if step < steps.length - 1}
        <button class="btn-next" onclick={() => step++}>Continue</button>
      {:else}
        <button class="btn-finish" onclick={finish} disabled={finishing}>
          {finishing ? "Launching..." : "Launch Zariff"}
        </button>
      {/if}
    </div>
  </div>
</div>

<style>
  .wizard-overlay {
    position: fixed;
    inset: 0;
    background: var(--bg-primary);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .wizard {
    width: 100%;
    max-width: 560px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    background:
      linear-gradient(160deg, rgba(0, 229, 255, 0.05), transparent 45%),
      linear-gradient(320deg, rgba(255, 46, 166, 0.05), transparent 45%),
      var(--bg-secondary);
    border: 1px solid rgba(0, 229, 255, 0.25);
    border-radius: var(--radius-lg);
    box-shadow:
      var(--shadow),
      0 0 40px rgba(0, 229, 255, 0.12),
      0 0 80px rgba(160, 107, 255, 0.08);
    overflow: hidden;
  }

  .wizard-header {
    padding: 24px 28px 20px;
    border-bottom: 1px solid var(--border);
  }

  h1 {
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 16px;
    background: linear-gradient(90deg, #00e5ff, #a06bff);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    filter: drop-shadow(0 0 10px rgba(0, 229, 255, 0.3));
  }

  :global(html.light-mode) h1 {
    background: none;
    -webkit-background-clip: initial;
    background-clip: initial;
    color: var(--text-primary);
    filter: none;
  }

  .progress {
    display: flex;
    align-items: center;
    gap: 0;
  }

  .progress-step {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .step-num {
    width: 22px;
    height: 22px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    border-radius: 50%;
    background: var(--bg-tertiary);
    color: var(--text-muted);
    border: 1px solid var(--border);
  }

  .progress-step.active .step-num {
    background: var(--accent);
    color: #04121a;
    border-color: var(--accent);
    box-shadow: 0 0 12px rgba(0, 229, 255, 0.55);
  }

  .progress-step.done .step-num {
    background: var(--success);
    color: #04121a;
    border-color: var(--success);
    box-shadow: 0 0 10px rgba(0, 255, 156, 0.45);
  }

  .step-label {
    font-size: 11px;
    color: var(--text-muted);
  }

  .progress-step.active .step-label {
    color: var(--text-primary);
    font-weight: 500;
  }

  .progress-line {
    flex: 1;
    height: 1px;
    background: var(--border);
    margin: 0 8px;
  }

  .progress-line.filled {
    background: var(--success);
  }

  .wizard-body {
    flex: 1;
    overflow-y: auto;
    padding: 24px 28px;
  }

  .wizard-step h2 {
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 12px;
  }

  .wizard-step p {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.6;
    margin-bottom: 10px;
  }

  .wizard-step ul {
    padding-left: 20px;
    margin-bottom: 12px;
  }

  .wizard-step li {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.6;
  }

  .note {
    font-size: 12px;
    color: var(--text-muted);
    font-style: italic;
  }

  .field {
    margin-bottom: 16px;
  }

  .field label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }

  .field-label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }

  .field input[type="text"],
  .field input[type="password"],
  .field select,
  .field textarea {
    width: 100%;
    padding: 8px 12px;
    font-size: 13px;
    background: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-family: inherit;
  }

  .field textarea {
    resize: vertical;
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .field select {
    cursor: pointer;
  }

  .hint {
    display: block;
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 4px;
  }

  .hint code {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--accent);
  }

  .hint.warning {
    color: var(--warning);
  }

  .model-status {
    padding: 10px 12px;
    font-size: 13px;
    color: var(--text-muted);
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
  }

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .radio-option {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 12px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: border-color 0.15s;
  }

  .radio-option.selected {
    border-color: var(--accent);
    background: var(--accent-muted);
  }

  .radio-option input {
    margin-top: 2px;
  }

  .radio-option div {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .radio-option strong {
    font-size: 13px;
  }

  .radio-option span {
    font-size: 11px;
    color: var(--text-muted);
  }

  .checkbox-label {
    display: flex !important;
    align-items: center;
    gap: 8px;
    cursor: default;
  }

  .summary {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 4px 0;
    margin: 16px 0;
  }

  .summary-item {
    display: flex;
    justify-content: space-between;
    padding: 8px 14px;
    font-size: 13px;
  }

  .summary-label {
    color: var(--text-muted);
  }

  .wizard-footer {
    display: flex;
    align-items: center;
    gap: 12px;
    justify-content: space-between;
    padding: 16px 28px;
    border-top: 1px solid var(--border);
  }

  .finish-error {
    margin: 0 auto 0 0;
    max-width: 58%;
    color: var(--danger, #ef4444);
    font-size: 11px;
    line-height: 1.35;
  }

  .btn-back {
    padding: 8px 20px;
    font-size: 13px;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    border-radius: var(--radius-sm);
  }

  .btn-back:hover {
    background: var(--bg-hover);
  }

  .btn-next,
  .btn-finish {
    padding: 8px 24px;
    font-size: 13px;
    font-weight: 700;
    font-family: var(--font-mono);
    letter-spacing: 0.05em;
    color: #04121a;
    background: linear-gradient(135deg, var(--accent), #00b8d4);
    border-radius: var(--radius-sm);
    box-shadow: 0 0 12px rgba(0, 229, 255, 0.35);
    transition: background 0.15s, box-shadow 0.15s, transform 0.1s;
  }

  .btn-next:hover,
  .btn-finish:hover {
    background: linear-gradient(135deg, var(--accent-hover), #00d9f2);
    box-shadow: 0 0 18px rgba(0, 229, 255, 0.55);
  }

  .btn-next:active,
  .btn-finish:active {
    transform: translateY(1px);
  }
</style>
