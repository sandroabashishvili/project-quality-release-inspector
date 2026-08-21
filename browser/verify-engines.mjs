import { chromium, firefox, webkit } from "@playwright/test";

for (const [name, engine] of [["Chromium", chromium], ["Firefox", firefox], ["WebKit", webkit]]) {
  const browser = await engine.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.setContent("<!doctype html><html><head><title>Engine probe</title></head><body><main>OK</main></body></html>");
    if (await page.title() !== "Engine probe") throw new Error(`${name} did not render the probe page`);
    process.stdout.write(`${name}: OK\n`);
  } finally {
    await browser.close();
  }
}
