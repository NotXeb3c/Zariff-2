import { describe, expect, it } from "vitest";
import { classifyExecuteResponse, normalizeActionResult } from "./executeResponse";

describe("classifyExecuteResponse", () => {
  it("shows only explicit success as a result", () => {
    expect(classifyExecuteResponse({ status: "success", message: "Verified." })).toEqual({
      status: "success",
      messageType: "result",
      text: "Verified.",
    });
  });

  it("adds grounded companion ideas to a successful result", () => {
    expect(
      classifyExecuteResponse({
        status: "success",
        message: "Verified.",
        companion_follow_up: {
          message: "The report is ready.",
          suggestions: ["Compare it with yesterday", "Set a weekly check"],
        },
      }).text,
    ).toBe(
      "Verified.\n\nThe report is ready.\n\nPossible next steps:\n- Compare it with yesterday\n- Set a weekly check",
    );
  });

  it.each(["partial_failure", "blocked_by_critic", "blocked_by_companion", "error"])(
    "shows %s as an error",
    (status) => {
      expect(classifyExecuteResponse({ status, message: "Not successful." }).messageType).toBe("error");
    },
  );

  it("shows cancellation as a neutral system result", () => {
    expect(classifyExecuteResponse({ status: "cancelled", message: "Denied." }).messageType).toBe("system");
  });

  it("fails closed for unknown or missing statuses", () => {
    expect(classifyExecuteResponse({ status: "future_status" }).messageType).toBe("error");
    expect(classifyExecuteResponse({}).text).toContain("not reported as successful");
  });

  it("prefers the terminal message over the planning explanation", () => {
    expect(
      classifyExecuteResponse({
        status: "partial_failure",
        message: "Execution failed with exit code 125.",
        explanation: "Print a greeting.",
      }).text,
    ).toBe("Execution failed with exit code 125.");
  });
});

describe("normalizeActionResult", () => {
  it("repairs legacy code execution results that swallowed Python exceptions", () => {
    expect(
      normalizeActionResult({
        action_type: "code_execute",
        target: "report_os_version",
        success: true,
        output: "An unexpected error occurred: name 'PREV_OUTPUT' is not defined",
        error: null,
      }),
    ).toEqual({
      action_type: "code_execute",
      target: "report_os_version",
      success: false,
      output: "",
      error: "An unexpected error occurred: name 'PREV_OUTPUT' is not defined",
    });
  });

  it("does not reinterpret ordinary successful output", () => {
    const result = {
      action_type: "code_execute",
      target: "report_os_version",
      success: true,
      output: "version: 10.0.26220",
      error: null,
    };
    expect(normalizeActionResult(result)).toEqual(result);
  });
});
