import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import AxeBuilder from "@axe-core/playwright";
import { chromium, firefox, webkit } from "@playwright/test";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";
import { baselineDecision } from "./visual-policy.mjs";


const inputPath = process.argv[2];
if (!inputPath) throw new Error("Usage: node run-browser-checks.mjs <input.json>");
const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const findings = [];
const add = (project, check, severity, message, details = "", page = "") => {
  findings.push({ project, check, severity, message, details, path: page });
};

const safeName = (value) => value.replace(/^\/+|\/+$/g, "").replace(/[^a-z0-9]+/gi, "-") || "home";
const defaultViewports = input.mode === "full"
  ? [
      { name: "mobile-small", width: 360, height: 800, isMobile: true, hasTouch: true },
      { name: "mobile-large", width: 430, height: 932, isMobile: true, hasTouch: true },
      { name: "tablet", width: 768, height: 1024, isMobile: false, hasTouch: true },
      { name: "desktop", width: 1440, height: 1000, isMobile: false, hasTouch: false },
    ]
  : [
      { name: "mobile", width: 390, height: 844, isMobile: true, hasTouch: true },
      { name: "desktop", width: 1440, height: 1000, isMobile: false, hasTouch: false },
    ];

function compareScreenshot(project, viewport, route, actualPath) {
  if (!project.visualPaths.includes(route)) return;
  const relative = path.join(project.id, viewport.name, `${safeName(route)}.png`);
  const baselinePath = path.join(input.baselinesDir, relative);
  const initialDecision = baselineDecision({ baselineExists: fs.existsSync(baselinePath), updateBaselines: input.updateBaselines });
  if (initialDecision === "updated") {
    fs.mkdirSync(path.dirname(baselinePath), { recursive: true });
    fs.copyFileSync(actualPath, baselinePath);
    add(project.id, "visual", "info", "Visual baseline updated by explicit approval", relative, route);
    return;
  }
  if (initialDecision === "missing") {
    add(project.id, "visual", "info", "Visual baseline is missing; review the screenshot and run --update-baselines to approve it", relative, route);
    return;
  }
  const actual = PNG.sync.read(fs.readFileSync(actualPath));
  const expected = PNG.sync.read(fs.readFileSync(baselinePath));
  if (actual.width !== expected.width || actual.height !== expected.height) {
    add(project.id, "visual", "warning", "Screenshot dimensions changed", `expected ${expected.width}x${expected.height}; actual ${actual.width}x${actual.height}`, route);
    return;
  }
  const diff = new PNG({ width: actual.width, height: actual.height });
  const changed = pixelmatch(actual.data, expected.data, diff.data, actual.width, actual.height, { threshold: 0.16 });
  const ratio = changed / (actual.width * actual.height);
  if (baselineDecision({ baselineExists: true, differenceRatio: ratio }) === "changed") {
    const diffPath = path.join(input.artifactsDir, "visual-diffs", relative);
    fs.mkdirSync(path.dirname(diffPath), { recursive: true });
    fs.writeFileSync(diffPath, PNG.sync.write(diff));
    add(project.id, "visual", "warning", `Visual difference ${(ratio * 100).toFixed(2)}% exceeds 1.5%`, diffPath, route);
  } else {
    add(project.id, "visual", "pass", `Visual comparison passed (${(ratio * 100).toFixed(2)}% difference)`, "", route);
  }
}

async function inspectPage(page, project, route, viewport) {
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  const baseOrigin = new URL(project.baseUrl).origin;
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("requestfailed", (request) => {
    try {
      if (new URL(request.url()).origin === baseOrigin) {
        failedRequests.push(`${request.url()} — ${request.failure()?.errorText || "failed"}`);
      }
    } catch {}
  });

  const url = new URL(route.replace(/^\//, ""), project.baseUrl).href;
  const response = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  if (!response || response.status() >= 400) {
    add(project.id, "browser", "error", `Page returned HTTP ${response?.status() ?? "no response"}`, url, route);
    return;
  }
  await page.waitForTimeout(150);

  const layout = await page.evaluate(() => {
    const root = document.documentElement;
    const overflow = [];
    for (const element of document.querySelectorAll("body *")) {
      const rect = element.getBoundingClientRect();
      if (rect.right > root.clientWidth + 2 || rect.left < -2) {
        const label = `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ""}${element.classList.length ? `.${[...element.classList].slice(0, 2).join(".")}` : ""}`;
        overflow.push(`${label}: left=${Math.round(rect.left)}, right=${Math.round(rect.right)}`);
        if (overflow.length >= 12) break;
      }
    }
    const badImages = [...document.images]
      .filter((image) => image.complete && image.naturalWidth === 0)
      .map((image) => image.currentSrc || image.src)
      .slice(0, 12);
    const wrappedSingleLineHeadings = [...document.querySelectorAll(".single-line-heading")]
      .filter((element) => {
        const style = getComputedStyle(element);
        const lineHeight = Number.parseFloat(style.lineHeight);
        return Number.isFinite(lineHeight) && element.getBoundingClientRect().height > lineHeight * 1.35;
      })
      .map((element) => element.textContent.trim())
      .slice(0, 12);
    const invalidTwoLineCopy = [...document.querySelectorAll(".two-line-copy, .two-line-heading")]
      .map((element) => {
        const style = getComputedStyle(element);
        const lineHeight = Number.parseFloat(style.lineHeight);
        const lines = Number.isFinite(lineHeight) ? Math.round(element.getBoundingClientRect().height / lineHeight) : 0;
        return { text: element.textContent.trim(), lines };
      })
      .filter((item) => item.lines !== 2)
      .slice(0, 12);
    return {
      horizontalOverflow: root.scrollWidth > root.clientWidth + 2,
      scrollWidth: root.scrollWidth,
      clientWidth: root.clientWidth,
      overflow,
      badImages,
      wrappedSingleLineHeadings,
      invalidTwoLineCopy,
      title: document.title,
      h1Count: document.querySelectorAll("h1").length,
    };
  });
  if (layout.horizontalOverflow) {
    add(project.id, "responsive", "error", `${viewport.name}: horizontal overflow ${layout.scrollWidth}px > ${layout.clientWidth}px`, layout.overflow.join("\n"), route);
  }
  if (layout.badImages.length) {
    add(project.id, "browser", "error", `${layout.badImages.length} image(s) failed to render`, layout.badImages.join("\n"), route);
  }
  if (!viewport.isMobile && layout.wrappedSingleLineHeadings.length) {
    add(project.id, "responsive", "error", `${viewport.name}: heading marked as single-line wrapped unexpectedly`, layout.wrappedSingleLineHeadings.join("\n"), route);
  }
  if (!viewport.isMobile && layout.invalidTwoLineCopy.length) {
    add(project.id, "responsive", "error", `${viewport.name}: content marked as two-line does not render in exactly two lines`, layout.invalidTwoLineCopy.map((item) => `${item.lines} lines — ${item.text}`).join("\n"), route);
  }
  if (!layout.title) add(project.id, "browser", "warning", "Rendered page has an empty title", "", route);
  if (layout.h1Count !== 1) add(project.id, "browser", "warning", `Rendered page has ${layout.h1Count} h1 elements`, "", route);

  if (project.themePolicy === "automatic" && route === "/" && !viewport.isMobile && viewport.width >= 1000) {
    const readPalette = () => page.evaluate(() => {
      const describe = (element) => {
        if (!element) return null;
        const style = getComputedStyle(element);
        return {
          color: style.color,
          backgroundColor: style.backgroundColor,
          backgroundImage: style.backgroundImage,
          colorScheme: style.colorScheme,
        };
      };
      return {
        root: describe(document.documentElement),
        body: describe(document.body),
        surface: describe(document.querySelector(".appbar, .topbar, .site-header, header, .panel, .card")),
        dataTheme: document.documentElement.dataset.theme || "",
      };
    });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.waitForTimeout(100);
    const lightPalette = await readPalette();
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    // Allow palette transitions to finish before evaluating nested surfaces.
    await page.waitForTimeout(320);
    const darkPalette = await readPalette();
    if (JSON.stringify(lightPalette) === JSON.stringify(darkPalette)) {
      add(project.id, "theme", "error", "Automatic light/dark mode does not change the rendered palette", JSON.stringify(lightPalette), route);
    } else {
      add(project.id, "theme", "pass", "Automatic light/dark mode changes the rendered palette", "", route);
    }

    const darkSurfaceLeaks = await page.evaluate(() => {
      const selectors = [
        ".panel", ".story-card", ".quick-link", ".route-card", ".stat-grid > div",
        ".meta-row > *", ".jobs-filter-bar input", ".jobs-filter-bar select",
        ".jobs-filter-reset", ".card", ".stat-card", ".kpi-card", ".risk-card"
      ];
      const leaks = [];
      for (const element of document.querySelectorAll(selectors.join(","))) {
        const rect = element.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        const color = getComputedStyle(element).backgroundColor;
        const match = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/);
        if (!match) continue;
        const red = Number(match[1]);
        const green = Number(match[2]);
        const blue = Number(match[3]);
        const alpha = match[4] === undefined ? 1 : Number(match[4]);
        if (red > 225 && green > 220 && blue > 205 && alpha >= 0.35) {
          const label = element.tagName.toLowerCase()
            + (element.id ? "#" + element.id : "")
            + (element.classList.length ? "." + [...element.classList].slice(0, 2).join(".") : "");
          leaks.push(label + ": " + color);
          if (leaks.length >= 12) break;
        }
      }
      return leaks;
    });
    if (darkSurfaceLeaks.length) {
      add(project.id, "theme", "error", "Dark mode leaves light nested surfaces visible", darkSurfaceLeaks.join("\n"), route);
    } else {
      add(project.id, "theme", "pass", "Dark mode nested surfaces use a consistent dark palette", "", route);
    }

    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    // Wait for theme-dependent CSS and any color transitions to settle before
    // accessibility checks. Scanning mid-transition can report false contrast
    // violations that are absent in both stable themes.
    await page.waitForTimeout(320);
  }

  if (["fixed-dark", "fixed-light"].includes(project.themePolicy) && route === "/" && !viewport.isMobile && viewport.width >= 1000) {
    const expectedScheme = project.themePolicy === "fixed-dark" ? "dark" : "light";
    const readFixedPalette = () => page.evaluate(() => {
      const root = getComputedStyle(document.documentElement);
      const body = getComputedStyle(document.body);
      return {
        colorScheme: root.colorScheme,
        rootColor: root.color,
        rootBackground: root.backgroundColor,
        bodyColor: body.color,
        bodyBackground: body.backgroundColor,
      };
    });
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.waitForTimeout(320);
    const underLightPreference = await readFixedPalette();
    await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
    await page.waitForTimeout(320);
    const underDarkPreference = await readFixedPalette();
    const schemeMatches = underLightPreference.colorScheme.split(/\s+/).includes(expectedScheme);
    const paletteIsFixed = JSON.stringify(underLightPreference) === JSON.stringify(underDarkPreference);
    if (!schemeMatches || !paletteIsFixed) {
      add(project.id, "theme", "error", `Declared ${project.themePolicy} policy does not remain fixed in the rendered page`, JSON.stringify({ underLightPreference, underDarkPreference }), route);
    } else {
      add(project.id, "theme", "pass", `Rendered page consistently uses its declared ${project.themePolicy} policy`, "", route);
    }
    await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
    await page.waitForTimeout(320);
  }

  if (viewport.isMobile) {
    for (const gridCheck of project.mobileGridChecks || []) {
      if (viewport.width < (gridCheck.minimum_viewport_width || 0)) continue;
      const grid = page.locator(gridCheck.selector).first();
      if (!(await grid.count()) || !(await grid.isVisible())) continue;
      const columns = await grid.evaluate((element) => {
        const value = getComputedStyle(element).gridTemplateColumns.trim();
        return value && value !== "none" ? value.split(/\s+/).length : 0;
      });
      const minimum = gridCheck.minimum_columns || 1;
      if (columns < minimum) {
        add(project.id, "responsive", "error", "Mobile content grid has too few columns", gridCheck.selector + ": " + columns + " < " + minimum, route);
      } else {
        add(project.id, "responsive", "pass", viewport.name + ": " + gridCheck.selector + " keeps " + columns + " columns", "", route);
      }
    }

    const stickyHeader = page.locator(".appbar, .topbar, .site-header, header").first();
    if (await stickyHeader.count() && await stickyHeader.isVisible()) {
      const stickyState = await stickyHeader.evaluate((element) => {
        const style = getComputedStyle(element);
        return { position: style.position, top: Number.parseFloat(style.top), height: element.getBoundingClientRect().height };
      });
      if (["sticky", "fixed"].includes(stickyState.position)) {
        await page.evaluate(() => window.scrollTo(0, Math.min(600, document.documentElement.scrollHeight)));
        await page.waitForTimeout(100);
        const renderedTop = await stickyHeader.evaluate((element) => element.getBoundingClientRect().top);
        if ((Number.isFinite(stickyState.top) && stickyState.top > 2) || renderedTop > 3) {
          add(project.id, "responsive", "error", "Sticky mobile header leaves an unintended gap above it", `css top=${stickyState.top}; rendered top=${renderedTop.toFixed(1)}px`, route);
        } else {
          add(project.id, "responsive", "pass", `${viewport.name}: sticky header remains flush with the viewport`, "", route);
        }
        if (stickyState.height > viewport.height * 0.38) {
          add(project.id, "responsive", "error", "Collapsed mobile header consumes too much of the viewport", `height=${stickyState.height.toFixed(1)}px; viewport=${viewport.height}px`, route);
        }
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(80);
      }
    }

    const toggle = page.locator("[data-mobile-menu-button], [data-menu-button], [data-nav-toggle], .nav-toggle, .menu-toggle").first();
    if (await toggle.count() && await toggle.isVisible()) {
      await toggle.click({ timeout: 3000 });
      await page.waitForTimeout(100);
      const state = await toggle.getAttribute("aria-expanded");
      const targetId = await toggle.getAttribute("aria-controls");
      const targetVisible = targetId ? await page.locator(`#${targetId}`).isVisible().catch(() => false) : true;
      if (state !== "true" || !targetVisible) {
        add(project.id, "interaction", "error", "Mobile menu did not open correctly", `aria-expanded=${state}; targetVisible=${targetVisible}`, route);
      } else {
        add(project.id, "interaction", "pass", `${viewport.name}: mobile menu opens`, "", route);
      }
      await toggle.click().catch(() => {});
      await page.waitForTimeout(80);
      const closedState = await toggle.getAttribute("aria-expanded");
      const closedVisible = targetId ? await page.locator(`#${targetId}`).isVisible().catch(() => false) : false;
      if (closedState !== "false" || closedVisible) {
        add(project.id, "interaction", "error", "Mobile menu did not close correctly", `aria-expanded=${closedState}; targetVisible=${closedVisible}`, route);
      }
    }
  }

  const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  const serious = axe.violations.filter((item) => ["serious", "critical"].includes(item.impact));
  const moderate = axe.violations.filter((item) => !["serious", "critical"].includes(item.impact));
  if (serious.length) {
    add(project.id, "accessibility", "error", `${serious.length} serious/critical accessibility rule violation(s)`, serious.map((item) => `${item.id}: ${item.help} (${item.nodes.length} nodes)\n${item.nodes.map((node) => `  ${node.target.join(" ")}`).join("\n")}`).join("\n"), route);
  }
  if (moderate.length) {
    add(project.id, "accessibility", "warning", `${moderate.length} minor/moderate accessibility rule violation(s)`, moderate.map((item) => `${item.id}: ${item.help} (${item.nodes.length} nodes)\n${item.nodes.map((node) => `  ${node.target.join(" ")}`).join("\n")}`).join("\n"), route);
  }
  if (!axe.violations.length) add(project.id, "accessibility", "pass", `${viewport.name}: automated accessibility scan passed`, "", route);

  if (consoleErrors.length) add(project.id, "console", "error", `${consoleErrors.length} browser console error(s)`, [...new Set(consoleErrors)].join("\n"), route);
  if (pageErrors.length) add(project.id, "javascript", input.offline ? "info" : "error", `${pageErrors.length} uncaught page error(s)${input.offline ? " (often caused by an unavailable external CDN in offline mode)" : ""}`, [...new Set(pageErrors)].join("\n"), route);
  if (failedRequests.length) add(project.id, "network", "error", `${failedRequests.length} same-origin request(s) failed`, [...new Set(failedRequests)].join("\n"), route);

  // Full-page screenshots must exercise lazy-loaded media as a real visitor
  // would. Otherwise an apparently valid comparison can contain blank image
  // placeholders simply because the page never entered the viewport.
  const fullHeight = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let offset = 0; offset < fullHeight; offset += Math.max(320, Math.floor(viewport.height * 0.75))) {
    await page.evaluate((top) => window.scrollTo(0, top), offset);
    await page.waitForTimeout(45);
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(120);

  const screenshotPath = path.join(input.artifactsDir, "screenshots", project.id, viewport.name, `${safeName(route)}.png`);
  fs.mkdirSync(path.dirname(screenshotPath), { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });
  compareScreenshot(project, viewport, route, screenshotPath);
  if (!layout.horizontalOverflow && !layout.badImages.length && !consoleErrors.length && (!pageErrors.length || input.offline) && !failedRequests.length) {
    add(project.id, "browser", "pass", `${viewport.name}: layout, images, console and local network passed`, "", route);
  }
}

async function inspectCrossBrowser(page, project, route, viewport, browserName) {
  const consoleErrors = [];
  const pageErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error" && !message.text().startsWith("Failed to load resource")) consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const url = new URL(route.replace(/^\//, ""), project.baseUrl).href;
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(300);
  if (!response || response.status() >= 400) {
    add(project.id, "cross-browser", "error", `${browserName}/${viewport.name}: HTTP ${response?.status() ?? "no response"}`, url, route);
    return;
  }
  const state = await page.evaluate(() => ({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
    badImages: [...document.images].filter((image) => image.complete && image.naturalWidth === 0).map((image) => image.currentSrc || image.src).slice(0, 8),
    title: document.title,
  }));
  const problems = [];
  if (state.overflow) problems.push(`horizontal overflow ${state.scrollWidth}px > ${state.clientWidth}px`);
  if (state.badImages.length) problems.push(`broken images: ${state.badImages.join(", ")}`);
  if (!state.title) problems.push("empty title");
  if (consoleErrors.length) problems.push(`console: ${[...new Set(consoleErrors)].join(" | ")}`);
  if (pageErrors.length) problems.push(`page errors: ${[...new Set(pageErrors)].join(" | ")}`);
  if (viewport.isMobile) {
    const toggle = page.locator("[data-mobile-menu-button], [data-menu-button], [data-nav-toggle], .nav-toggle, .menu-toggle").first();
    if (await toggle.count() && await toggle.isVisible()) {
      await toggle.click({ timeout: 3000 });
      await page.waitForTimeout(100);
      const expanded = await toggle.getAttribute("aria-expanded");
      const targetId = await toggle.getAttribute("aria-controls");
      const targetVisible = targetId ? await page.locator(`#${targetId}`).isVisible().catch(() => false) : true;
      if (expanded !== "true" || !targetVisible) problems.push(`mobile menu failed: aria-expanded=${expanded}, targetVisible=${targetVisible}`);
    }
  }
  add(
    project.id,
    "cross-browser",
    problems.length ? "error" : "pass",
    problems.length ? `${browserName}/${viewport.name}: compatibility smoke failed` : `${browserName}/${viewport.name}: compatibility smoke passed`,
    problems.join("\n"),
    route,
  );
}

const browser = await chromium.launch({ headless: true });
try {
  for (const project of input.projects) {
    const viewports = project.viewports?.length ? project.viewports : defaultViewports;
    for (const viewport of viewports) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        isMobile: viewport.isMobile,
        hasTouch: viewport.hasTouch,
        colorScheme: "light",
        locale: "de-DE",
        timezoneId: "Europe/Berlin",
        reducedMotion: "reduce",
      });
      const page = await context.newPage();
      if (project.login) {
        await page.goto(project.baseUrl, { waitUntil: "networkidle" });
        await page.locator(project.login.username_selector).fill(project.login.username);
        await page.locator(project.login.password_selector).fill(project.login.password);
        await Promise.all([
          page.waitForLoadState("networkidle"),
          page.locator(project.login.submit_selector).click(),
        ]);
      }
      for (const route of project.browserPaths) {
        try {
          await inspectPage(page, project, route, viewport);
        } catch (error) {
          add(project.id, "browser", "error", `${viewport.name}: browser check crashed`, error.stack || String(error), route);
        }
      }
      await context.close();
    }
  }
} finally {
  await browser.close();
}

if (input.mode === "full") {
  const engines = [["Firefox", firefox], ["WebKit", webkit]];
  const smokeViewports = [
    { name: "mobile", width: 390, height: 844, isMobile: true, hasTouch: true },
    { name: "desktop", width: 1440, height: 1000, isMobile: false, hasTouch: false },
  ];
  for (const [browserName, engine] of engines) {
    let smokeBrowser;
    try {
      smokeBrowser = await engine.launch({ headless: true });
      for (const project of input.projects) {
        for (const viewport of smokeViewports) {
          const context = await smokeBrowser.newContext({
            viewport: { width: viewport.width, height: viewport.height },
            colorScheme: "light",
            locale: "de-DE",
            timezoneId: "Europe/Berlin",
            reducedMotion: "reduce",
          });
          const page = await context.newPage();
          if (project.login) {
            await page.goto(project.baseUrl, { waitUntil: "domcontentloaded" });
            await page.locator(project.login.username_selector).fill(project.login.username);
            await page.locator(project.login.password_selector).fill(project.login.password);
            await page.locator(project.login.submit_selector).click();
            await page.waitForLoadState("domcontentloaded");
          }
          for (const route of project.crossBrowserPaths?.length ? project.crossBrowserPaths : ["/"]) {
            try {
              await inspectCrossBrowser(page, project, route, viewport, browserName);
            } catch (error) {
              add(project.id, "cross-browser", "error", `${browserName}/${viewport.name}: compatibility smoke crashed`, error.stack || String(error), route);
            }
          }
          await context.close();
        }
      }
    } catch (error) {
      add("quality-system", "cross-browser", "error", `${browserName} could not start`, error.stack || String(error));
    } finally {
      if (smokeBrowser) await smokeBrowser.close();
    }
  }
}

process.stdout.write(JSON.stringify({ findings }));
