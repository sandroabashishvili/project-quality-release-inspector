import assert from "node:assert/strict";
import test from "node:test";

import { engineLaunchSeverity } from "../engine-policy.mjs";

test("missing Linux host libraries are an environment warning", () => {
  assert.equal(engineLaunchSeverity("Host system is missing dependencies to run browsers"), "warning");
});

test("unexpected browser launch failures remain errors", () => {
  assert.equal(engineLaunchSeverity("browser executable is corrupt"), "error");
});
