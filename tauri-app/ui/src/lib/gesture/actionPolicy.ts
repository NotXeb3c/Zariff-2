export type DefaultGestureAction = "abort" | "confirm" | "deny" | "unbound";

/**
 * Built-in gesture actions are deliberately limited to guarded control
 * signals. Every other recognized pose/motion requires an explicit user
 * binding before it may create a task or change the operating system.
 */
export function defaultGestureAction(gesture: string): DefaultGestureAction {
  switch (gesture) {
    case "palm":
    case "middle_finger":
      return "abort";
    case "thumbs_up":
    case "palm_push":
      return "confirm";
    case "thumbs_down":
    case "palm_pull":
      return "deny";
    default:
      return "unbound";
  }
}
