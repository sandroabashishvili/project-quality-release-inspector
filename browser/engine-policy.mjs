export function engineLaunchSeverity(details) {
  return String(details).includes("Host system is missing dependencies") ? "warning" : "error";
}
