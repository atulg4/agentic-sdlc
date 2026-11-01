/* A fake Control Center store, injected into the page before the console's
 * own module runs (Playwright's addInitScript with a path, so this file is
 * plain script source and needs no escaping).
 *
 * The console talks to one narrow surface — window.claude.use("db") and a
 * document's onSnapshot/get — so the whole live-delivery contract can be
 * exercised without a server: ordered deliveries, duplicate deliveries,
 * out-of-order deliveries, a terminated listener, and the full-snapshot read
 * the console is supposed to make by itself when a feed stops.
 */

(() => {
  const listeners = new Map();
  const docs = new Map();
  const calls = { get: 0, onSnapshot: 0 };

  const snap = (path) => ({
    get exists() { return docs.has(path); },
    data: () => docs.get(path) || null,
  });

  const collection = () => {
    const query = {
      where: () => query,
      orderBy: () => query,
      limit: () => query,
      onSnapshot: (next) => { try { next({ docs: [] }); } catch { /* ignored */ } return () => {}; },
    };
    return query;
  };

  window.__store = {
    calls,
    // Deliver a document to every listener on its path, the way a live store
    // would.
    deliver(path, body) {
      docs.set(path, body);
      for (const entry of listeners.get(path) || []) {
        try { entry.next(snap(path)); } catch (error) { window.__store.lastThrow = error; }
      }
    },
    // Terminate a listener the way a dropped subscription does.
    fail(path, error) {
      for (const entry of listeners.get(path) || []) {
        try { entry.error(error); } catch (err) { window.__store.lastThrow = err; }
      }
    },
    set(path, body) { docs.set(path, body); },
  };

  const db = {
    collection,
    doc: (path) => ({
      onSnapshot: (next, error) => {
        calls.onSnapshot += 1;
        const bucket = listeners.get(path) || [];
        bucket.push({ next, error: error || (() => {}) });
        listeners.set(path, bucket);
        if (docs.has(path)) { try { next(snap(path)); } catch { /* ignored */ } }
        return () => {};
      },
      get: async () => { calls.get += 1; return snap(path); },
      set: async (body) => { window.__store.deliver(path, body); },
    }),
  };

  window.claude = {
    use: (name) => (name === "db" ? Promise.resolve(db) : Promise.resolve(null)),
  };

  /* Re-digest a document a test edited, with the producer's own rule: content
   * minus the run metadata. The console verifies every delivery, so an edited
   * document without a fresh digest is (correctly) refused and a test would be
   * measuring that refusal rather than an update. */
  const encode = (entry) => {
    if (entry === null || typeof entry === "number" || typeof entry === "boolean") {
      return JSON.stringify(entry);
    }
    if (typeof entry === "string") {
      // Mirrors the console's canonicalJson, which mirrors Python's
      // ensure_ascii=True: every non-ASCII code unit is escaped.
      return JSON.stringify(entry).replace(/[\u007f-￿]/g,
        (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
    }
    if (Array.isArray(entry)) return "[" + entry.map(encode).join(",") + "]";
    return "{" + Object.keys(entry).sort()
      .map((key) => encode(key) + ":" + encode(entry[key])).join(",") + "}";
  };

  window.__reseal = async (document_) => {
    const clone = JSON.parse(JSON.stringify(document_));
    delete clone.snapshotSha256;
    delete clone.generatedAt;
    for (const project of clone.projects || []) {
      for (const incident of project.incidents || []) delete incident.observedAt;
    }
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(encode(clone)));
    document_.snapshotSha256 = [...new Uint8Array(bytes)]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    return document_;
  };
})();
