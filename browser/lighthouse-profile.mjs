import fs from "node:fs";
import os from "node:os";
import path from "node:path";


export function createLighthouseProfile() {
  const tempRoot = process.platform === "linux" ? "/tmp" : os.tmpdir();
  return fs.mkdtempSync(path.join(tempRoot, "sandro-quality-lighthouse-"));
}


export function launcherOptions(chromePath, profilePath) {
  return {
    chromePath,
    // chrome-launcher converts custom paths to UNC paths when it detects WSL.
    // Our Chromium binary is Linux-native, so provide the Linux path ourselves.
    userDataDir: false,
    chromeFlags: [
      "--headless",
      "--no-sandbox",
      "--disable-gpu",
      "--user-data-dir=" + profilePath,
    ],
  };
}


export function removeLighthouseProfile(profilePath) {
  fs.rmSync(profilePath, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
}


export async function shutdownLighthouseChrome(chrome, profilePath) {
  try {
    if (chrome) {
      await chrome.kill();
    }
  } catch {
    // Profile cleanup remains mandatory if browser shutdown reports an error.
  } finally {
    removeLighthouseProfile(profilePath);
  }
}
