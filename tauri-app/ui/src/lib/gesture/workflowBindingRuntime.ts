export interface GestureWorkflowBinding {
  gesture_name: string;
  goal_template: string;
  enabled: boolean;
}

export interface GestureWorkflowPolicy {
  enabled: boolean;
  bindings: GestureWorkflowBinding[];
}

type DaemonCall = (method: string, params?: Record<string, unknown>) => Promise<unknown>;

export function activeGestureWorkflowBindings(policy: GestureWorkflowPolicy): Record<string, string> {
  if (!policy.enabled || !Array.isArray(policy.bindings)) return {};
  return Object.fromEntries(
    policy.bindings
      .filter(
        (binding) =>
          binding.enabled &&
          typeof binding.gesture_name === "string" &&
          typeof binding.goal_template === "string" &&
          binding.gesture_name.trim() &&
          binding.goal_template.trim(),
      )
      .map((binding) => [binding.gesture_name.trim(), binding.goal_template.trim()]),
  );
}

export async function submitGestureWorkflow(call: DaemonCall, gestureName: string, goal: string): Promise<string> {
  const result = (await call("voice_gesture_workflow_submit", {
    goal,
    invocation_source: "gesture",
  })) as { status?: string; message?: string };
  if (result.status !== "submitted") {
    throw new Error(result.message || "Gesture workflow was not started");
  }
  return `Started ${gestureName.replace(/_/g, " ")} workflow`;
}

export async function controlGestureWorkflow(
  call: DaemonCall,
  intent: "continue" | "cancel",
  workflowId: string,
): Promise<string> {
  const method = intent === "continue" ? "voice_gesture_workflow_resume" : "voice_gesture_workflow_cancel";
  const result = (await call(method, { workflow_id: workflowId })) as {
    resumed?: boolean;
    cancelled?: boolean;
    message?: string;
  };
  const completed = intent === "continue" ? result.resumed : result.cancelled;
  if (!completed) {
    throw new Error(result.message || `Workflow was not ${intent === "continue" ? "resumed" : "cancelled"}`);
  }
  return intent === "continue" ? "Gesture workflow resumed" : "Gesture workflow cancelled";
}
