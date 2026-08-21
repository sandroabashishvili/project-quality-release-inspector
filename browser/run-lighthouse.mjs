import fs from "node:fs";
import process from "node:process";

import { chromium } from "@playwright/test";
import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";

import {
  createLighthouseProfile,
  launcherOptions,
  shutdownLighthouseChrome,
} from "./lighthouse-profile.mjs";


const inputPath = process.argv[2];
if (!inputPath) throw new Error("Usage: node run-lighthouse.mjs <input.json>");
const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const results = [];
const lighthouseProfile = createLighthouseProfile();
let chrome;
try {
  chrome = await chromeLauncher.launch(
    launcherOptions(chromium.executablePath(), lighthouseProfile)
  );
  for (const target of input.targets) {
    try {
      const run = await lighthouse(target.url, {
        port: chrome.port,
        output: "json",
        logLevel: "error",
        onlyCategories: ["performance", "accessibility", "best-practices", "seo"],
        formFactor: "mobile",
        screenEmulation: { mobile: true, width: 390, height: 844, deviceScaleFactor: 2.75, disabled: false },
      });
      const lhr = run.lhr;
      const scores = Object.fromEntries(
        Object.entries(lhr.categories).map(([key, value]) => [key, Math.round((value.score || 0) * 100)])
      );
      const reportPath = `${input.artifactsDir}/lighthouse-${target.id}.json`;
      fs.writeFileSync(reportPath, JSON.stringify(lhr, null, 2));
      results.push({ id: target.id, url: target.url, scores, reportPath });
    } catch (error) {
      results.push({ id: target.id, url: target.url, error: error.stack || String(error) });
    }
  }
} finally {
  await shutdownLighthouseChrome(chrome, lighthouseProfile);
}
process.stdout.write(JSON.stringify({ results }));
