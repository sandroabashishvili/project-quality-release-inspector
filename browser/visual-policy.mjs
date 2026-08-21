export function baselineDecision({ baselineExists, updateBaselines, differenceRatio = 0, threshold = 0.015 }) {
  if (updateBaselines) return "updated";
  if (!baselineExists) return "missing";
  return differenceRatio > threshold ? "changed" : "passed";
}
