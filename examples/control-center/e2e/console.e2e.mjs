/* Browser tests for the mobile-first Control Center console.
 *
 * Run directly:  node examples/control-center/e2e/console.e2e.mjs
 * Or through pytest, which skips when Playwright or its browser is absent:
 *   python -m pytest tests/test_control_center_console.py
 *
 * Offline by construction: the page is a single file loaded over file://, the
 * store is a stub injected before the console's module runs, and the fixtures
 * are the documents the Forge CLI itself produced in examples/control-center/.
 */

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Resolved through CommonJS: ESM import ignores NODE_PATH, and Playwright is
// commonly installed outside the repository (a toolchain directory, a global
// prefix) rather than as a dependency of this stdlib-only project.
const { chromium } = createRequire(import.meta.url)("playwright");

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const PAGE = "file://" + resolve(ROOT, "control-center.html");

const SNAPSHOT = JSON.parse(readFileSync(resolve(ROOT, "snapshot.json"), "utf8"));
const TELEMETRY = JSON.parse(readFileSync(resolve(ROOT, "telemetry.json"), "utf8"));


/* ---------- performance budgets ----------
 * Documented in docs/control-center.md. They are generous enough to survive a
 * loaded CI runner and tight enough that a full-page re-render on every
 * delivery - the behaviour this slice removes - cannot pass them.
 */
const BUDGETS = {
  initialRenderMs: 1000,     // 500-unit snapshot, validated, verified, rendered
  longTaskMs: 400,           // no single task may block the main thread longer
  burstMs: 6000,             // 100 live deliveries, applied
  perDeliveryMs: 60,         // mean latency of one delivery in that burst
  burstInsertsPerUpdate: 40, // DOM elements a one-unit change may insert
};

/* ---------- the harness ---------- */
const cases = [];
const test = (name, fn) => cases.push([name, fn]);

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const DESTINATIONS_INDEX = [[0], [1], [2], [3], [4]];

const PHONES = [
  ["iphone-se", 360, 780],
  ["iphone-14", 390, 844],
  ["android", 412, 915],
];

async function openConsole(browser, { width = 390, height = 844, snapshot = SNAPSHOT, reducedMotion } = {}) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: width < 1024,
    reducedMotion,
  });
  const page = await context.newPage();
  const problems = [];
  page.on("pageerror", (error) => problems.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(message.text());
  });
  await page.addInitScript({ path: resolve(HERE, "store-stub.js") });
  await page.goto(PAGE);
  if (snapshot) {
    await page.evaluate((document_) => {
      window.__store.deliver("snapshots/current", {
        document: document_, loadedAt: "2026-10-29T09:00:00Z", loadedBy: "fixture",
      });
    }, snapshot);
  }
  await page.waitForFunction(() => window.__forgeConsole && window.__forgeConsole.renders > 0);
  await page.waitForTimeout(60);
  return { context, page, problems };
}

/* ================= phone layout ================= */

for (const [label, width, height] of PHONES) {
  test(`phone layout has no horizontal scroll at ${label} (${width}x${height})`, async (browser) => {
    const { context, page, problems } = await openConsole(browser, { width, height });
    try {
      assertEqual(await page.evaluate(() => window.__forgeConsole.layout), "phone", "layout mode");
      const overflow = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        win: window.innerWidth,
        widest: Math.max(...[...document.querySelectorAll("body *")].map(
          (node) => node.getBoundingClientRect().right)),
      }));
      assert(overflow.doc <= overflow.win + 1,
        `page scrolls horizontally: scrollWidth ${overflow.doc} > innerWidth ${overflow.win}`);
      assert(overflow.widest <= overflow.win + 1,
        `an element reaches ${overflow.widest}px, past the ${overflow.win}px viewport`);
      // The primary view is the console's own cards, not a table.
      const primary = await page.evaluate(() => ({
        cards: document.querySelectorAll("#view .ucard").length,
        tables: document.querySelectorAll("#view table.dense").length,
      }));
      assert(primary.tables === 0, "a dense table is showing as the phone's primary view");
      assertEqual(problems.length, 0, "page errors: " + problems.join(" | "));
    } finally { await context.close(); }
  });
}

test("the bottom bar reaches all five destinations", async (browser) => {
  const { context, page, problems } = await openConsole(browser);
  try {
    const ids = await page.evaluate(() =>
      [...document.querySelectorAll("#bottom-nav .dest")].map((node) => node.textContent.trim()));
    assertEqual(ids.length, 5, "destination count");
    for (const label of ["Projects", "Backlog", "Agents", "Cost", "Effectiveness"]) {
      assert(ids.some((text) => text.includes(label)), `no destination for ${label}: ${ids.join(", ")}`);
    }
    for (let index = 0; index < 5; index += 1) {
      await page.locator("#bottom-nav .dest").nth(index).click();
      await page.waitForTimeout(40);
      const current = await page.evaluate(() => ({
        dest: window.__forgeConsole.dest,
        marked: document.querySelectorAll('#bottom-nav .dest[aria-current="page"]').length,
        sections: document.querySelectorAll("#view > *").length,
      }));
      assertEqual(current.marked, 1, "exactly one destination is marked current");
      assert(current.sections > 0, `destination ${current.dest} rendered nothing`);
    }
    assertEqual(problems.length, 0, "page errors: " + problems.join(" | "));
  } finally { await context.close(); }
});

test("every touch target is at least 44 by 44 CSS pixels", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    const small = await page.evaluate(() => {
      const selector = "#bottom-nav .dest, #view .ucard, #view button.btn";
      return [...document.querySelectorAll(selector)]
        .filter((node) => node.offsetParent !== null)
        .map((node) => {
          const box = node.getBoundingClientRect();
          return { text: (node.textContent || "").trim().slice(0, 40), w: Math.round(box.width), h: Math.round(box.height) };
        })
        .filter((entry) => entry.w < 44 || entry.h < 44);
    });
    assertEqual(small.length, 0, "targets under 44px: " + JSON.stringify(small));
  } finally { await context.close(); }
});

/* ================= cards, charts, drill-down ================= */

test("a backlog card shows stage, blocker, runner, elapsed time and progress", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    const cards = page.locator("#view .ucard");
    assert(await cards.count() > 0, "the backlog destination rendered no work-unit card");
    const text = await cards.first().innerText();
    for (const needle of ["stage ", "in state ", "model ", "worker ", "blocker: "]) {
      assert(text.includes(needle), `card is missing "${needle}": ${JSON.stringify(text)}`);
    }
    const progress = await cards.first().locator(".prog-text").innerText();
    assert(/stage \d+ of \d+|off the lifecycle rail|stage unknown/.test(progress),
      `progress indicator reads ${JSON.stringify(progress)}`);
    // A missing value is "unknown", never a zero.
    assert(!/\bmodel 0\b|\bworker 0\b/.test(text), "a missing value rendered as zero");
  } finally { await context.close(); }
});

test("tapping a card opens a drill-down with every declared section", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    await page.locator("#view .ucard button").first().click();
    await page.waitForTimeout(60);
    const detail = page.locator("#view .detail").first();
    assert(await detail.count() > 0, "no drill-down opened");
    const text = await detail.innerText();
    for (const heading of [
      "Lifecycle timeline", "Stage durations", "Worker and model",
      "Tokens and cost: estimate versus actual", "Dependencies", "Artifacts",
    ]) {
      assert(text.includes(heading), `drill-down is missing "${heading}"`);
    }
    assert(await page.evaluate(() => Boolean(window.__forgeConsole.detail)), "detail state was not recorded");
  } finally { await context.close(); }
});

test("charts are accessible images with keyboard-reachable tooltips", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(2).click();
    await page.waitForTimeout(60);
    const charts = await page.evaluate(() =>
      [...document.querySelectorAll('#view svg[role="img"]')].map((node) => node.getAttribute("aria-label")));
    assert(charts.length > 0, "the Agents destination drew no chart");
    for (const label of charts) {
      assert(label && label.length > 8, `a chart has no usable aria-label: ${JSON.stringify(label)}`);
    }
    // Segments are focusable and open their tooltip on focus, not only hover.
    const segment = page.locator("#view svg .seg").first();
    assert(await segment.count() > 0, "no chart segment is focusable");
    await segment.focus();
    await page.waitForTimeout(40);
    assertEqual(await page.locator(".tip").count(), 1, "focus did not open the tooltip");
    const tip = await page.locator(".tip").innerText();
    assert(/\d/.test(tip), `tooltip carries no figure: ${JSON.stringify(tip)}`);
  } finally { await context.close(); }
});

test("a view with no source document reads unavailable, never zero", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    for (const [index, destination] of [[3, "cost"], [4, "effectiveness"]]) {
      await page.locator("#bottom-nav .dest").nth(index).click();
      await page.waitForTimeout(60);
      const text = await page.locator("#view").innerText();
      assert(text.includes("unavailable"), `${destination} did not report unavailable`);
      assert(/sdlcctl (usage-report|effectiveness-report)/.test(text),
        `${destination} did not name the command that produces its document`);
      assert(await page.locator('#view [role="img"]').count() > 0,
        `${destination} unavailable state is not an accessible image`);
    }
  } finally { await context.close(); }
});

test("a loaded telemetry bundle fills the cost and agent views", async (browser) => {
  const { context, page, problems } = await openConsole(browser);
  try {
    const problem = await page.evaluate((bundle) => window.__forgeConsole.loadTelemetry(bundle), TELEMETRY);
    assertEqual(problem, "", "the fixture bundle was refused");
    await page.locator("#bottom-nav .dest").nth(3).click();
    await page.waitForTimeout(60);
    const cost = await page.locator("#view").innerText();
    assert(cost.includes("Tokens by model"), "no tokens-by-model chart");
    assert(cost.includes("claude-opus-5"), "the model rollup's own labels are missing");
    assert(cost.includes("per day"), "no daily trend");
    await page.locator("#bottom-nav .dest").nth(2).click();
    await page.waitForTimeout(60);
    const agents = await page.locator("#view").innerText();
    assert(agents.includes("Queue and parallelism"), "no parallelism view");
    assertEqual(problems.length, 0, "page errors: " + problems.join(" | "));
  } finally { await context.close(); }
});

test("state is readable without colour, and every control has a name", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    const audit = { nameless: [], duplicateIds: [], headings: [], colourOnly: [] };
    for (const [index] of DESTINATIONS_INDEX) {
      await page.locator("#bottom-nav .dest").nth(index).click();
      await page.waitForTimeout(60);
      const found = await page.evaluate(() => {
        const name = (node) => (node.getAttribute("aria-label")
          || node.getAttribute("title") || (node.textContent || "")).trim();
        const nameless = [...document.querySelectorAll("button, a[href], [role=button]")]
          .filter((node) => node.offsetParent !== null && name(node).length === 0)
          .map((node) => node.outerHTML.slice(0, 90));
        const ids = [...document.querySelectorAll("[id]")].map((node) => node.id);
        const duplicateIds = ids.filter((id, position) => ids.indexOf(id) !== position);
        // Every health or state indicator carries its own words: a chip whose
        // only content is a coloured dot would be colour-only.
        const colourOnly = [...document.querySelectorAll(".chip, .prog-fill")]
          .filter((node) => node.offsetParent !== null)
          .filter((node) => {
            if (node.classList.contains("prog-fill")) {
              const label = node.closest(".prog")?.querySelector(".prog-text");
              return !(label && label.textContent.trim().length > 0);
            }
            return (node.textContent || "").replace(/\s+/g, "").length === 0;
          })
          .map((node) => node.outerHTML.slice(0, 90));
        const levels = [...document.querySelectorAll("#view h2, #view h3, #view h4")]
          .map((node) => Number(node.tagName.slice(1)));
        const headings = [];
        for (let position = 1; position < levels.length; position += 1) {
          if (levels[position] - levels[position - 1] > 1) {
            headings.push(levels[position - 1] + " -> " + levels[position]);
          }
        }
        return { nameless, duplicateIds, headings, colourOnly };
      });
      for (const key of Object.keys(audit)) audit[key].push(...found[key]);
    }
    assertEqual(audit.nameless.length, 0, "controls with no accessible name: " + JSON.stringify(audit.nameless));
    assertEqual(audit.duplicateIds.length, 0, "duplicate element ids: " + JSON.stringify(audit.duplicateIds));
    assertEqual(audit.colourOnly.length, 0, "state conveyed by colour alone: " + JSON.stringify(audit.colourOnly));
    assertEqual(audit.headings.length, 0, "heading levels skip: " + JSON.stringify(audit.headings));
  } finally { await context.close(); }
});

/* ================= live updates ================= */

test("a duplicate delivery changes nothing in the DOM", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    await page.locator("#view .ucard button").first().click();
    await page.waitForTimeout(60);
    const before = await page.evaluate(() => ({
      detail: window.__forgeConsole.detail,
      nodes: document.querySelectorAll("#view *").length,
    }));
    await page.evaluate((document_) => {
      window.__store.deliver("snapshots/current", {
        document: document_, loadedAt: "2026-10-29T09:00:00Z", loadedBy: "fixture",
      });
    }, SNAPSHOT);
    await page.waitForTimeout(120);
    const after = await page.evaluate(() => ({
      detail: window.__forgeConsole.detail,
      nodes: document.querySelectorAll("#view *").length,
      stats: window.__forgeConsole.patchStats,
    }));
    assertEqual(after.detail, before.detail, "the open drill-down was lost on a duplicate delivery");
    assertEqual(after.nodes, before.nodes, "a duplicate delivery changed the node count");
    assertEqual(after.stats.inserted, 0, "a duplicate delivery inserted DOM nodes");
    assert(after.stats.kept > 0, "nothing was kept, so the view was rebuilt");
  } finally { await context.close(); }
});

test("a one-unit change leaves the rest of the view, the scroll and the open drill-down alone", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    // Open the FIRST card's drill-down, then change the LAST unit: the change
    // is below the fold, so nothing legitimately reflows above the viewport
    // and the scroll offset must not move at all.
    await page.locator("#view .ucard button").first().click();
    await page.waitForTimeout(60);
    await page.evaluate(() => window.scrollTo({ top: 240 }));
    await page.waitForTimeout(40);
    const before = await page.evaluate(() => ({
      y: window.scrollY,
      detail: window.__forgeConsole.detail,
      openCard: document.querySelector("#view .ucard .detail")?.parentElement?.dataset.key || "",
      cards: document.querySelectorAll("#view .ucard").length,
    }));
    assert(before.openCard.length > 0, "no drill-down was open to preserve");
    await page.evaluate(async (document_) => {
      const next = structuredClone(document_);
      const project = next.projects.find((entry) => (entry.waiting || []).length)
        || next.projects.find((entry) => (entry.active || []).length);
      const list = (project.waiting || []).length ? project.waiting : project.active;
      list[list.length - 1].activity = "patched by the live-update test";
      window.__store.deliver("snapshots/current", {
        document: await window.__reseal(next), loadedAt: "2026-10-29T09:01:00Z", loadedBy: "fixture",
      });
    }, SNAPSHOT);
    await page.waitForTimeout(200);
    const after = await page.evaluate(() => ({
      y: window.scrollY,
      detail: window.__forgeConsole.detail,
      openCard: document.querySelector("#view .ucard .detail")?.parentElement?.dataset.key || "",
      cards: document.querySelectorAll("#view .ucard").length,
      stats: window.__forgeConsole.patchStats,
      error: document.querySelector("#view .banner.bad")?.innerText || "",
      patched: document.querySelector("#view").innerText.includes("patched by the live-update test"),
    }));
    assertEqual(after.error, "", "the delivery was refused rather than applied");
    assert(after.patched, "the changed unit never reached the view");
    assertEqual(after.detail, before.detail, "the open drill-down state was lost");
    assertEqual(after.openCard, before.openCard, "the drill-down moved to another card");
    assertEqual(after.cards, before.cards, "the card count changed");
    assertEqual(after.y, before.y, `scroll moved from ${before.y} to ${after.y}`);
    assert(after.stats.kept >= after.cards - 2,
      `only ${after.stats.kept} of ${after.cards} sections were kept, so the view was rebuilt`);
  } finally { await context.close(); }
});

test("a stopped feed keeps the last-good view and reloads a full snapshot", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    const before = await page.evaluate(() => ({
      units: window.__forgeConsole.units,
      getCalls: window.__store.calls.get,
    }));
    assert(before.units > 0, "the fixture snapshot carried no work unit");
    await page.evaluate(() => window.__store.fail("snapshots/current", { message: "listener terminated" }));
    await page.waitForTimeout(250);
    const after = await page.evaluate(() => ({
      degraded: window.__forgeConsole.degraded,
      lastGoodAt: window.__forgeConsole.lastGoodAt,
      units: window.__forgeConsole.units,
      getCalls: window.__store.calls.get,
      text: document.querySelector("#view").innerText,
    }));
    assert(after.getCalls > before.getCalls, "no full snapshot was re-read after the feed stopped");
    assertEqual(after.units, before.units, "the last-good view was discarded");
    assert(after.text.includes("last good") || after.text.includes("Showing the last"),
      "the degraded view is not labelled as last-good");
    assert(after.lastGoodAt.length > 0, "no last-good instant was recorded");
  } finally { await context.close(); }
});

/* ================= motion, credentials, performance ================= */

test("reduced motion disables every transition", async (browser) => {
  const { context, page } = await openConsole(browser, { reducedMotion: "reduce" });
  try {
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    const durations = await page.evaluate(() =>
      [...document.querySelectorAll("#view .ucard, #view .card")]
        .map((node) => getComputedStyle(node).transitionDuration));
    assert(durations.length > 0, "nothing was rendered to check");
    for (const duration of durations) {
      assert(/^0s(, 0s)*$/.test(duration), `a transition still runs: ${duration}`);
    }
  } finally { await context.close(); }
});

test("a credential-shaped value is refused, not rendered", async (browser) => {
  const { context, page } = await openConsole(browser);
  try {
    const refusal = await page.evaluate(() => window.__forgeConsole.loadTelemetry({
      schemaVersion: 1,
      byModel: { schemaVersion: 1, groupBy: ["model"], buckets: [{ key: { model: "ghp_" + "a".repeat(36) } }] },
    }));
    assert(refusal.includes("credentials"), `a credential-shaped value was accepted: ${refusal}`);
    assertEqual(await page.evaluate(() => window.__forgeConsole.telemetryLoaded), false,
      "the refused bundle was loaded anyway");
    const fieldRefusal = await page.evaluate(() => window.__forgeConsole.loadTelemetry({
      schemaVersion: 1, capacity: { apiToken: "x" },
    }));
    assert(fieldRefusal.includes("credential field"), `a secret-shaped key was accepted: ${fieldRefusal}`);
  } finally { await context.close(); }
});

test("a 500-unit snapshot renders and absorbs a 100-change burst within budget", async (browser) => {
  // A snapshot with 500 units, built from the fixture's own unit shape.
  const big = structuredClone(SNAPSHOT);
  const template = big.projects.flatMap((project) => project.active || []).find(Boolean)
    || big.projects.flatMap((project) => project.waiting || []).find(Boolean);
  if (!template) throw new Error("the fixture snapshot carries no unit to clone");
  const project = big.projects.find((entry) => (entry.active || []).length) || big.projects[0];
  project.active = [];
  project.attention = [];
  project.waiting = [];
  for (let index = 0; index < 500; index += 1) {
    const unit = structuredClone(template);
    unit.workUnitId = "wu-load-" + index;
    unit.activity = "load case " + index;
    unit.dependsOn = [];
    project.active.push(unit);
  }
  // The console refuses a snapshot whose state census disagrees with its
  // visible units, so the counts are rebuilt the way the producer builds
  // them. The assertion on `initial.error` below is what keeps this honest:
  // if the rule ever changes, the case fails rather than quietly measuring a
  // refusal.
  const CANONICAL_STAGE = {
    intake: "intake", triaged: "intake", specified: "specification", planned: "planning",
    "approval-pending": "planning", approved: "planning", dispatched: "dispatch",
    implementing: "implementation", verifying: "deterministic-verification",
    reviewing: "independent-review", "repair-needed": "repair", repairing: "repair",
    "re-reviewing": "re-review", "ready-for-human-merge": "merge", merged: "merge",
    deploying: "deploy", "deployment-verifying": "deployment-verification", live: "live",
    "rolling-back": "rollback", "rolled-back": "rollback", blocked: "blocked",
    failed: "failed", cancelled: "cancelled", superseded: "superseded",
  };
  const byState = {};
  for (const unit of project.active) byState[unit.state] = (byState[unit.state] || 0) + 1;
  const byStage = {};
  for (const [stateName, count] of Object.entries(byState)) {
    const stage = CANONICAL_STAGE[stateName];
    byStage[stage] = (byStage[stage] || 0) + count;
  }
  project.counts = { ...project.counts, byState, byStage, workUnits: project.active.length };
  const { context, page, problems } = await openConsole(browser, { snapshot: null });
  try {
    // The 500 cards live on the Backlog destination: select it BEFORE the
    // delivery so the measurement covers rendering them, not a wait for a
    // view that was never asked for.
    await page.locator("#bottom-nav .dest").nth(1).click();
    await page.waitForTimeout(40);
    await page.evaluate(() => {
      window.__longTasks = [];
      try {
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) window.__longTasks.push(entry.duration);
        }).observe({ entryTypes: ["longtask"] });
      } catch { /* longtask is not observable everywhere */ }
    });
    const initial = await page.evaluate(async (document_) => {
      const sealed = await window.__reseal(document_);
      const started = performance.now();
      window.__store.deliver("snapshots/current", {
        document: sealed, loadedAt: "2026-10-29T09:00:00Z", loadedBy: "load",
      });
      // The console verifies the digest asynchronously; wait for the view.
      for (let tick = 0; tick < 400; tick += 1) {
        if (document.querySelectorAll("#view .ucard").length >= 50) break;
        await new Promise((resolve_) => setTimeout(resolve_, 10));
      }
      return {
        ms: performance.now() - started,
        cards: document.querySelectorAll("#view .ucard").length,
        error: document.querySelector("#view .banner.bad")?.innerText || "",
        bounded: document.querySelector("#view").innerText,
      };
    }, big);
    assertEqual(initial.error, "", "the load snapshot was refused");
    // The console renders a bounded page of an attention-first list and says
    // how much it is withholding: a phone that lays out 500 cards spends its
    // frame budget on rows nobody scrolled to, and every later delivery pays
    // that cost again.
    assertEqual(initial.cards, 50, "the bounded page size changed");
    assert(initial.bounded.includes("Showing 50 of 500"),
      `the bounded list does not say what it withholds: ${JSON.stringify(initial.bounded)}`);
    assert(initial.ms < BUDGETS.initialRenderMs,
      `initial render took ${Math.round(initial.ms)}ms, over the ${BUDGETS.initialRenderMs}ms budget`);
    const burst = await page.evaluate(async (document_) => {
      // Every delivery is sealed first, outside the timing window: the test
      // measures what the console does with a delivery, not how fast the
      // browser can hash one.
      const deliveries = [];
      for (let index = 0; index < 100; index += 1) {
        const next = structuredClone(document_);
        const target = next.projects.find((entry) => (entry.active || []).length > 400);
        target.active[index].activity = "burst " + index;
        deliveries.push(await window.__reseal(next));
        // Yield between seals: preparing the fixtures is the test's own work,
        // and doing it in one block would register as a long task the console
        // never caused.
        await new Promise((resolve_) => setTimeout(resolve_, 0));
      }
      // Only the console's own tasks count towards the long-task budget.
      window.__longTasks = [];
      const started = performance.now();
      let inserted = 0;
      for (const delivery of deliveries) {
        window.__store.deliver("snapshots/current", {
          document: delivery, loadedAt: "2026-10-29T09:02:00Z", loadedBy: "load",
        });
        await new Promise((resolve_) => setTimeout(resolve_, 0));
        inserted = Math.max(inserted, window.__forgeConsole.patchStats.inserted);
      }
      return { ms: performance.now() - started, inserted };
    }, big);
    assert(burst.inserted <= BUDGETS.burstInsertsPerUpdate,
      `one delivery in the burst inserted ${burst.inserted} nodes, over the `
      + `${BUDGETS.burstInsertsPerUpdate} budget: the view is being rebuilt`);
    assert(burst.ms < BUDGETS.burstMs,
      `a 100-change burst took ${Math.round(burst.ms)}ms, over the ${BUDGETS.burstMs}ms budget`);
    assert(burst.ms / 100 < BUDGETS.perDeliveryMs,
      `each delivery averaged ${Math.round(burst.ms / 100)}ms, over the ${BUDGETS.perDeliveryMs}ms budget`);
    // Phase timings say WHICH part regressed, not merely that something did.
    const phases = await page.evaluate(() => window.__forgeConsole.deliveryTimings);
    assert(phases.render < BUDGETS.perDeliveryMs,
      `rendering one delivery took ${Math.round(phases.render)}ms`);
    const longest = await page.evaluate(() => Math.max(0, ...(window.__longTasks || [])));
    assert(longest < BUDGETS.longTaskMs,
      `a task blocked the main thread for ${Math.round(longest)}ms, over the ${BUDGETS.longTaskMs}ms budget`);
    assertEqual(problems.length, 0, "page errors: " + problems.join(" | "));
    console.log(`      initial ${Math.round(initial.ms)}ms for ${initial.cards} cards · `
      + `burst ${Math.round(burst.ms)}ms (${Math.round(burst.ms / 100)}ms per delivery: `
      + `validate ${Math.round(phases.validate)} verify ${Math.round(phases.verify)} `
      + `adopt ${Math.round(phases.adopt)} render ${Math.round(phases.render)}) · `
      + `longest task ${Math.round(longest)}ms`);
  } finally { await context.close(); }
});

/* ================= desktop ================= */

test("the desktop view is a dense table beside the same charts", async (browser) => {
  const { context, page } = await openConsole(browser, { width: 1280, height: 900 });
  try {
    assertEqual(await page.evaluate(() => window.__forgeConsole.layout), "desktop", "layout mode");
    assertEqual(await page.locator("#bottom-nav .dest").count(), 5, "the nav is still built");
    assert(!(await page.locator("#bottom-nav").isVisible()), "the bottom bar shows on desktop");
    await page.locator('#tabs .tab:text("All units")').click();
    await page.waitForTimeout(80);
    assert(await page.locator("#view table.dense").count() > 0, "no dense table");
    assertEqual(await page.locator("#view .ucard").count(), 0, "phone cards leaked into the desktop view");
    await page.locator("#view table.dense tbody tr").first().click();
    await page.waitForTimeout(80);
    assert(await page.locator("#view .detail").count() > 0, "a desktop row opened no drill-down");
  } finally { await context.close(); }
});

/* ---------- runner ---------- */
const browser = await chromium.launch({ args: ["--no-sandbox"] });
let failed = 0;
try {
  for (const [name, fn] of cases) {
    try {
      await fn(browser);
      console.log("  ok   " + name);
    } catch (error) {
      failed += 1;
      console.log("  FAIL " + name + "\n       " + (error && error.message ? error.message : error));
    }
  }
} finally {
  await browser.close();
}
console.log(`\n${cases.length - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
