export type ExecuteMessageType = "result" | "error" | "system";

export interface ClassifiedExecuteResponse {
  status: string;
  messageType: ExecuteMessageType;
  text: string;
}

export interface NormalizableActionResult {
  action_type: string;
  target: string;
  success: boolean;
  output: string;
  error: string | null;
}

const CODE_EXECUTION_FAILURE_OUTPUT = /^\s*(?:An unexpected error occurred:|Traceback \(most recent call last\):)/i;

/**
 * Repair action results written by older daemons that swallowed Python
 * exceptions into stdout and incorrectly persisted them as successful.
 */
export function normalizeActionResult<T extends NormalizableActionResult>(result: T): T {
  if (result.success && result.action_type === "code_execute" && CODE_EXECUTION_FAILURE_OUTPUT.test(result.output)) {
    return {
      ...result,
      success: false,
      output: "",
      error: result.error || result.output.trim(),
    };
  }
  return result;
}

/**
 * Convert the daemon's terminal response into one explicit UI state.
 *
 * This is intentionally allow-list based. A new or malformed daemon status
 * must show as an error instead of silently falling through to a green result.
 */
export function classifyExecuteResponse(result: Record<string, unknown>): ClassifiedExecuteResponse {
  const status = String(result.status ?? "");
  const text = String(result.message ?? result.explanation ?? "").trim();
  const rawFollowUp = result.companion_follow_up;
  const followUp =
    rawFollowUp && typeof rawFollowUp === "object"
      ? (rawFollowUp as { message?: unknown; suggestions?: unknown })
      : null;
  const followUpMessage = String(followUp?.message ?? "").trim();
  const followUpSuggestions = Array.isArray(followUp?.suggestions)
    ? followUp.suggestions
        .map((item) => String(item).trim())
        .filter(Boolean)
        .slice(0, 3)
    : [];
  const companionText =
    followUpMessage && followUpSuggestions.length > 0
      ? `${followUpMessage}\n\nPossible next steps:\n${followUpSuggestions.map((idea) => `- ${idea}`).join("\n")}`
      : "";
  const successText = [text || "Task completed successfully.", companionText].filter(Boolean).join("\n\n");

  switch (status) {
    case "success":
      return {
        status,
        messageType: "result",
        text: successText,
      };
    case "partial_failure":
      return {
        status,
        messageType: "error",
        text: text || "Task completed with errors.",
      };
    case "blocked_by_critic":
      return {
        status,
        messageType: "error",
        text: text || "The safety review blocked this plan before execution.",
      };
    case "blocked_by_companion":
      return {
        status,
        messageType: "error",
        text: text || "The interactive companion stopped a misaligned plan before execution.",
      };
    case "cancelled":
      return {
        status,
        messageType: "system",
        text: text || "Task cancelled.",
      };
    case "error":
      return {
        status,
        messageType: "error",
        text: text || "The task failed.",
      };
    default:
      return {
        status: status || "unknown",
        messageType: "error",
        text: `Unexpected daemon response${status ? ` status: ${status}` : ""}. The task was not reported as successful.`,
      };
  }
}
