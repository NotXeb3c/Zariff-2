/**
 * Visual regression tests — Settings Panel
 *
 * Covers all visible sections of SettingsPanel.svelte:
 *   - Appearance (theme toggle)
 *   - Security (root access, dry run, snapshot toggles)
 *   - Usage (token/cost display)
 *   - Screen Vision
 *   - Model (provider, mode, ollama model)
 *   - Cloud API (provider buttons, API key input)
 *   - Restrictions
 *   - Debug
 *
 * Also tests the light-mode variant to catch theme-switching regressions.
 */

import { test, expect } from "@playwright/test";
import { gotoApp, clickTab, freezeAnimations } from "./helpers";

test.describe("Settings Panel", () => {
  test.beforeEach(async ({ page }) => {
    await gotoApp(page);
    await clickTab(page, "Settings");
    await freezeAnimations(page);
    // Wait for the settings panel to be fully rendered
    await page.waitForSelector(".settings-panel", { timeout: 5_000 });
  });

  test("full settings panel matches baseline (dark mode)", async ({ page }) => {
    const panel = page.locator(".settings-panel");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveScreenshot("settings-full-dark.png", {
      fullPage: false,
    });
  });

  test("appearance section matches baseline", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Appearance" });
    await expect(section).toBeVisible();
    await expect(section).toHaveScreenshot("settings-appearance-section.png");
  });

  test("security section matches baseline", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Security" });
    await expect(section).toBeVisible();
    await expect(section).toHaveScreenshot("settings-security-section.png");
  });

  test("model section matches baseline", async ({ page }) => {
    const section = page.locator(".settings-group").filter({
      has: page.getByRole("heading", { name: "Model", exact: true }),
    });
    await expect(section).toBeVisible();
    await expect(section).toHaveScreenshot("settings-model-section.png");
  });

  test("cloud API section matches baseline", async ({ page }) => {
    const section = page.locator(".settings-group").filter({
      has: page.getByRole("heading", { name: "Cloud API (Fast)", exact: true }),
    });
    await expect(section).toBeVisible();
    await expect(section).toHaveScreenshot("settings-cloud-section.png");
  });

  test("toggle active state renders correctly", async ({ page }) => {
    // Click the Root Access toggle and verify the active CSS class is applied
    const rootToggle = page.locator(".setting-row").filter({ hasText: "Root Access" }).locator(".toggle");

    await expect(rootToggle).toBeVisible();
    // Capture inactive state
    await expect(rootToggle).toHaveScreenshot("settings-toggle-inactive.png");

    // Click to activate
    await rootToggle.click();
    await page.waitForTimeout(100);
    await expect(rootToggle).toHaveScreenshot("settings-toggle-active.png");
  });

  test("light mode settings panel matches baseline", async ({ page }) => {
    // Click the Light Mode toggle to switch themes
    const themeToggle = page.locator(".setting-row").filter({ hasText: "Light Mode" }).locator(".toggle");

    await themeToggle.click();
    await page.waitForTimeout(200); // allow theme transition

    const panel = page.locator(".settings-panel");
    await expect(panel).toHaveScreenshot("settings-full-light.png");
  });

  test("restrictions section matches baseline", async ({ page }) => {
    const section = page.locator(".settings-group").filter({
      has: page.getByRole("heading", { name: "Restrictions", exact: true }),
    });
    await expect(section).toBeVisible();
    await expect(section).toHaveScreenshot("settings-restrictions-section.png");
  });

  test("hybrid world model exposes runtime status and saves its toggle", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Hybrid World Model" });
    await section.scrollIntoViewIfNeeded();

    await expect(section).toContainText("Loaded");
    await expect(section).toContainText("36,000 real samples");
    await expect(section).toContainText("5,400 held-out samples");
    await expect(section).toContainText("Calibration");
    await expect(section).toContainText("Active");
    await expect(section).toContainText("risk-mlp-v3-calibrated");
    await expect(section).toContainText("80% risk");
    await expect(section).toContainText("learned_calibrated + rule");
    await expect(section).toContainText("77% model confidence");

    const runtimeCard = section.locator(".status-card").filter({ hasText: "Runtime" });
    await expect(runtimeCard).toContainText("Enabled");
    await section.getByRole("button", { name: "Toggle Learned Risk World Model" }).click();
    await expect(runtimeCard).toContainText("Enabled");
    await section.getByRole("button", { name: "Save" }).click();
    await expect(runtimeCard).toContainText("Disabled");

    await expect
      .poll(() => page.evaluate(() => (window as any).__risk_gate_update__))
      .toMatchObject({
        method: "risk_gate_config_update",
        params: { enabled: false },
      });
  });

  test("gesture workflow bindings validate, save, and reload", async ({ page }) => {
    let section = page.locator(".settings-group").filter({ hasText: "Gesture Workflow Bindings" });
    await section.scrollIntoViewIfNeeded();

    await expect(section).toContainText(
      "Saved changes apply immediately, including while the camera is already running.",
    );
    await section.getByRole("button", { name: "Add Binding" }).click();
    await expect(section).toContainText("Every binding needs a workflow goal.");
    await expect(section.getByRole("button", { name: "Save" })).toBeDisabled();

    await section.getByRole("combobox", { name: "Gesture for binding 1" }).selectOption("swipe_up");
    await section.getByRole("textbox", { name: "Workflow goal for binding 1" }).fill("run my daily briefing");
    await section.getByRole("button", { name: "Toggle gesture workflow bindings" }).click();
    await section.getByRole("button", { name: "Save" }).click();

    await expect
      .poll(() => page.evaluate(() => (window as any).__gesture_workflow_update__))
      .toMatchObject({
        method: "gesture_workflow_bindings_update",
        params: {
          enabled: true,
          bindings: [
            {
              gesture_name: "swipe_up",
              goal_template: "run my daily briefing",
              enabled: true,
            },
          ],
        },
      });

    await clickTab(page, "Activity");
    await clickTab(page, "Settings");
    section = page.locator(".settings-group").filter({ hasText: "Gesture Workflow Bindings" });
    await section.scrollIntoViewIfNeeded();

    await expect(section.getByRole("button", { name: "Toggle gesture workflow bindings" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(section.getByRole("combobox", { name: "Gesture for binding 1" })).toHaveValue("swipe_up");
    await expect(section.getByRole("textbox", { name: "Workflow goal for binding 1" })).toHaveValue(
      "run my daily briefing",
    );
  });

  test("voice workflow panel submits a durable workflow goal", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Active Voice/Gesture Workflows" });
    await section.scrollIntoViewIfNeeded();

    await expect(section).toContainText("No active workflows right now.");
    await section.getByPlaceholder("Describe the multi-step goal").fill("open github and review notifications");
    await section.getByRole("button", { name: "Start Workflow" }).click();

    await expect
      .poll(() => page.evaluate(() => (window as any).__workflow_submit__))
      .toMatchObject({
        method: "voice_gesture_workflow_submit",
        params: {
          goal: "open github and review notifications",
          invocation_source: "voice",
        },
      });
  });

  test("autonomous healing shows live monitors and saves selected metrics", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Autonomous Healing" });
    await section.scrollIntoViewIfNeeded();

    await expect(section).toContainText("CPU > 80% · 24% · 12 checks");
    await expect(section).toContainText("RAM > 85% · 58% · 8 checks");
    await expect(section).toContainText("Disk > 90% · 71% · 2 checks");
    await section.getByRole("button", { name: "Toggle disk monitoring" }).click();
    await section.getByRole("button", { name: "Save" }).click();

    await expect
      .poll(() => page.evaluate(() => (window as any).__self_healing_update__))
      .toMatchObject({
        method: "self_healing_config_update",
        params: {
          enabled: true,
          auto_execute_max_tier: 1,
          watched_metrics: ["cpu", "memory"],
        },
      });
  });

  test("specialist mesh shows expanded agents and complete action coverage", async ({ page }) => {
    const section = page.locator(".settings-group").filter({ hasText: "Specialist Agent Mesh" });
    await section.scrollIntoViewIfNeeded();

    await expect(section).toContainText("21");
    await expect(section).toContainText("156 / 156");
    await expect(section).toContainText("Complete action coverage");
    await expect(section).toContainText("FileOperationsAgent");
    await expect(section).toContainText("GitAgent");
    await expect(section).toContainText("VisionAgent");
    await expect(section).not.toContainText("actions uncovered");
  });

  test("full window with settings tab active matches baseline", async ({ page }) => {
    await expect(page.locator(".window")).toHaveScreenshot("settings-full-window.png");
  });
});

test.describe("Hybrid World Model compatibility", () => {
  test("an outdated daemon never looks like an empty fallback model", async ({ page }) => {
    await gotoApp(page, { riskGateStatusMissing: true });
    await clickTab(page, "Settings");

    const section = page.locator(".settings-group").filter({ hasText: "Hybrid World Model" });
    await section.scrollIntoViewIfNeeded();

    await expect(section.getByRole("alert")).toContainText(
      "This app is connected to an older Zariff daemon. Restart the daemon, then retry.",
    );
    await expect(section).not.toContainText("Rule fallback only");
    await expect(section).not.toContainText("0 real samples");
    await expect(section.getByRole("button", { name: "Toggle Learned Risk World Model" })).toBeDisabled();
  });
});
