import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

import {
  createLighthouseProfile,
  launcherOptions,
  removeLighthouseProfile,
  shutdownLighthouseChrome,
} from "../lighthouse-profile.mjs";


test("uses a Linux temporary profile without chrome-launcher WSL path conversion", () => {
  const profile = createLighthouseProfile();
  try {
    const options = launcherOptions("/linux/chromium", profile);
    assert.equal(options.userDataDir, false);
    assert.ok(options.chromeFlags.includes("--user-data-dir=" + profile));
    assert.ok(profile.startsWith("/tmp/"));
  } finally {
    removeLighthouseProfile(profile);
  }
  assert.equal(fs.existsSync(profile), false);
});


test("removes Chromium profile contents including dangling singleton links", () => {
  const profile = createLighthouseProfile();
  fs.writeFileSync(profile + "/Preferences", "{}");
  fs.symlinkSync("missing-cookie-target", profile + "/SingletonCookie");

  removeLighthouseProfile(profile);

  assert.equal(fs.existsSync(profile), false);
});


test("cleans profile when chrome kill returns no promise", async () => {
  const profile = createLighthouseProfile();
  await shutdownLighthouseChrome({ kill() {} }, profile);
  assert.equal(fs.existsSync(profile), false);
});


test("cleans profile when chrome kill throws", async () => {
  const profile = createLighthouseProfile();
  await shutdownLighthouseChrome({ kill() { throw new Error("shutdown failed"); } }, profile);
  assert.equal(fs.existsSync(profile), false);
});
