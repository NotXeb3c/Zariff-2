export function getJarvisGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 6) {
    return "Good evening. Zariff is online.";
  }
  if (hour < 12) {
    return "Good morning. Zariff is ready.";
  }
  if (hour < 17) {
    return "Good afternoon. Zariff at your service.";
  }
  if (hour < 21) {
    return "Good evening. Zariff is ready.";
  }
  return "Burning the midnight oil?";
}
