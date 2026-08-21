import test from "node:test";
import assert from "node:assert/strict";
import { baselineDecision } from "../visual-policy.mjs";

test("missing baseline is reported and never auto-approved", () => {
  assert.equal(baselineDecision({ baselineExists: false, updateBaselines: false }), "missing");
});

test("baseline updates require explicit approval", () => {
  assert.equal(baselineDecision({ baselineExists: false, updateBaselines: true }), "updated");
});

test("visual threshold distinguishes pass and change", () => {
  assert.equal(baselineDecision({ baselineExists: true, differenceRatio: 0.01 }), "passed");
  assert.equal(baselineDecision({ baselineExists: true, differenceRatio: 0.02 }), "changed");
});
