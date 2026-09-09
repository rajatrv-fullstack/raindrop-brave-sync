#!/usr/bin/env node
// Harness for extension/sw.js: runs the real service worker under a stub "chrome" and checks
// the contract it shares with the native host. No dependencies; exit code is the result.
//
//   node tests/js/run.mjs
//
// The stub bookmarks model canonicalises URLs the way Chromium does for the cases the rule
// covers (lower-case scheme and host, "/" for an empty path) and refuses an unparseable one
// with "Invalid URL.", so the worker is exercised against the browser behaviour that produced
// the audit findings (EXT-02 duplicates, EXT-03 endless retries, EXT-05 double syncs, EXT-06
// swallowed failures, EXT-07 folder-path drift).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const SW = readFileSync(join(here, "..", "..", "extension", "sw.js"), "utf8");
const CASES = JSON.parse(readFileSync(join(here, "folder_paths.json"), "utf8")).cases;

let failures = 0, checks = 0;
function assert(cond, msg) {
  checks += 1;
  if (!cond) { failures += 1; console.log(`  FAIL  ${msg}`); } else console.log(`  ok    ${msg}`);
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function canon(url) {
  const u = new URL(url);          // throws like chrome.bookmarks.create does on junk
  return u.href;
}

/** A fresh worker instance with its own bookmarks model, host script and clock. */
function boot({ hostReplies, existing = [] } = {}) {
  let now = 1_700_000_000_000;
  const nodes = new Map();
  let nextId = 10;
  const add = (n) => { nodes.set(n.id, n); return n; };
  const root = add({ id: "0", title: "", children: [] });
  add({ id: "1", parentId: "0", title: "Bookmarks Bar", folderType: "bookmarks-bar" });
  add({ id: "2", parentId: "0", title: "Other Bookmarks", folderType: "other" });
  for (const [title, url, parent] of existing) add({ id: String(nextId++), parentId: parent || "1", title, url: canon(url) });

  const children = (id) => [...nodes.values()].filter(n => n.parentId === id);
  const tree = (n) => ({ ...n, children: n.url ? undefined : children(n.id).map(tree) });
  const sent = [];
  const listeners = { installed: [], startup: [], alarm: [] };
  const alarms = [];
  const store = new Map();
  const hostCalls = [];

  const chrome = {
    bookmarks: {
      getTree: async () => [tree(root)],
      getChildren: async (id) => children(id).map(n => ({ ...n })),
      create: async ({ parentId, title, url }) => {
        if (!nodes.has(parentId)) throw new Error("Can't find parent bookmark for id.");
        const n = { id: String(nextId++), parentId, title };
        if (url !== undefined) { try { n.url = canon(url); } catch { throw new Error("Invalid URL."); } }
        return { ...add(n) };
      },
      search: async ({ url }) => {
        let c; try { c = canon(url); } catch { return []; }
        return [...nodes.values()].filter(n => n.url === c).map(n => ({ ...n }));
      },
      removeTree: async (id) => {
        const rm = (i) => { for (const c of children(i)) rm(c.id); nodes.delete(i); };
        if (!nodes.has(id)) throw new Error("Can't find bookmark for id.");
        rm(id);
      },
    },
    runtime: {
      sendNativeMessage: async (host, msg) => {
        hostCalls.push({ host, msg });
        const r = hostReplies(msg, hostCalls.length);
        if (r instanceof Error) throw r;
        return r;
      },
      onInstalled: { addListener: f => listeners.installed.push(f) },
      onStartup: { addListener: f => listeners.startup.push(f) },
    },
    alarms: {
      create: (name, opts) => alarms.push({ name, ...opts }),
      onAlarm: { addListener: f => listeners.alarm.push(f) },
    },
    storage: {
      local: {
        get: async (keys) => {
          const ks = typeof keys === "string" ? [keys] : keys;
          const out = {}; for (const k of ks) if (store.has(k)) out[k] = store.get(k); return out;
        },
        set: async (obj) => { for (const [k, v] of Object.entries(obj)) store.set(k, v); },
        remove: async (keys) => { for (const k of (typeof keys === "string" ? [keys] : keys)) store.delete(k); },
      },
    },
  };
  class FakeDate extends Date {
    constructor(...a) { super(...(a.length ? a : [now])); }
    static now() { return now; }
  }
  const sandbox = {
    chrome, navigator: { brave: {}, userAgent: "Mozilla/5.0 Chrome/152" }, URL, Date: FakeDate,
    console: { log: () => {}, warn: () => {}, error: () => {} }, sent,
  };
  vm.createContext(sandbox);
  vm.runInContext(SW, sandbox, { filename: "sw.js" });
  // The listeners do not return the sync promise, so after firing one, drain the microtask
  // queue: every await inside runSync resolves before a macrotask gets to run.
  const settle = () => new Promise(r => setImmediate(r));
  const fire = async (kind) => {
    for (const f of listeners[kind]) f(kind === "alarm" ? { name: "sync" } : undefined);
    for (let i = 0; i < 5; i++) await settle();
  };
  return {
    sandbox, hostCalls, alarms, store, nodes, children, fire,
    tick: async (ms) => { now += ms; },
    paths: () => {
      const out = {};
      const walk = (id, p) => { for (const c of children(id)) { const q = p + "/" + c.title; if (c.url) (out[c.url] ||= []).push(q); else walk(c.id, q); } };
      walk("1", "");
      return out;
    },
  };
}

const applied = (calls) => calls.filter(c => c.msg.op === "applied").map(c => c.msg.results);

console.log("sw.js contract");

// --- 1. no sync at load; the alarm is re-armed; onInstalled syncs once ---------------------------
{
  const w = boot({ hostReplies: () => ({ items: [], reset_folders: [] }) });
  assert(w.hostCalls.length === 0, "loading the worker sends nothing to the host (no top-level sync)");
  assert(w.alarms.some(a => a.name === "sync" && a.periodInMinutes === 1), "the 1-minute alarm is armed at load");
  await w.fire("installed");
  assert(w.hostCalls.filter(c => c.msg.op === "pending").length === 1, "onInstalled runs exactly one pending call");
  assert(w.hostCalls[0].host === "com.raindrop_sync.host" && w.hostCalls[0].msg.client === "brave", "host name and client are right");
}

// --- 2. create, reject, already_present; folder rule; canonical url in the result -----------------
{
  const items = [
    { raindrop_id: 1, name: "One", url: "https://one.example", folder_path: "Raindrop/Research" },
    { raindrop_id: 2, name: "Two", url: "https://two.example/x", folder_path: "Bookmarks Bar/Raindrop/Research" },
    { raindrop_id: 3, name: "Bad", url: "not a url", folder_path: "Raindrop/Junkfolder" },
    { raindrop_id: 4, name: "Dup", url: "https://WWW.Example.org", folder_path: "Raindrop/Sites" },
    { raindrop_id: 5, name: "Empty", url: "https://five.example/", folder_path: " / " },
  ];
  const w = boot({
    existing: [["Existing", "https://www.example.org/"]],
    hostReplies: (m) => m.op === "pending" ? { items, reset_folders: [] } : { ok: true, recorded: 5 },
  });
  await w.fire("alarm");
  const [results] = applied(w.hostCalls);
  const by = Object.fromEntries(results.map(r => [r.raindrop_id, r]));
  assert(by[1].status === "created" && by[1].url === "https://one.example/", "created result carries the browser's canonical url");
  assert(by[2].status === "created", "a leading 'Bookmarks Bar' segment is stripped, not nested");
  assert(by[3].status === "rejected" && /invalid url/.test(by[3].error), "an unparseable url is rejected");
  assert(by[4].status === "already_present", "a non-canonical staged url matches the stored canonical node");
  assert(by[5].status === "rejected" && by[5].error === "empty folder_path", "an empty folder path is rejected");
  const paths = w.paths();
  assert(eq(paths["https://one.example/"], ["/Raindrop/Research/One"]) && eq(paths["https://two.example/x"], ["/Raindrop/Research/Two"]), "both items landed under Raindrop/Research");
  assert(eq(paths["https://www.example.org/"], ["/Existing"]), "no duplicate of the existing node");
  const titles = [...w.nodes.values()].map(n => n.title);
  assert(!titles.includes("Junkfolder"), "a rejected item leaves no empty folder behind");
  assert(w.store.get("lastRun").reported === true && w.store.get("lastRun").applied === 2, "lastRun records 2 created and reported");
}

// --- 3. host unreachable: persisted error, doubling backoff, ticks skipped, recovery ---------------
{
  let up = false;
  const w = boot({ hostReplies: () => up ? { items: [], reset_folders: [] } : new Error("Specified native messaging host not found.") });
  await w.fire("alarm");
  assert(w.store.get("backoffMs") === 60_000 && /host not found/.test(w.store.get("lastError")), "first failure persists lastError and a 1 minute backoff");
  const before = w.hostCalls.length;
  await w.tick(30_000); await w.fire("alarm");
  assert(w.hostCalls.length === before, "a tick inside the backoff window does not spawn the host");
  await w.tick(31_000); await w.fire("alarm");
  assert(w.hostCalls.length === before + 1 && w.store.get("backoffMs") === 120_000, "after the window it retries and the backoff doubles");
  for (let i = 0; i < 6; i++) { await w.tick(16 * 60_000); await w.fire("alarm"); }
  assert(w.store.get("backoffMs") === 15 * 60_000, "the backoff caps at 15 minutes");
  up = true; await w.tick(16 * 60_000); await w.fire("alarm");
  assert(!w.store.has("backoffMs") && !w.store.has("nextAttemptAt"), "a successful call clears the backoff");
  await w.fire("installed");
  assert(w.hostCalls.filter(c => c.msg.op === "pending").length >= 2, "onInstalled always tries, backoff or not");
}

// --- 4. ok:false ack and pending.error are recorded, never reported as success -------------------
{
  const w = boot({
    hostReplies: (m) => m.op === "pending"
      ? { items: [{ raindrop_id: 9, name: "N", url: "https://n.example/", folder_path: "Raindrop/A" }], reset_folders: [], error: "desired unreadable" }
      : { ok: false, error: "results must be a list" },
  });
  await w.fire("alarm");
  assert(w.store.get("lastRun").reported === false, "an ok:false ack is not reported as recorded");
  assert(w.store.get("lastError") === "results must be a list", "the ack's error text is persisted");
}

// --- 5. reset: the named folder is removed and rebuilt from the same reply ------------------------
{
  const items = [
    { raindrop_id: 1, name: "One", url: "https://one.example/", folder_path: "Raindrop/A" },
    { raindrop_id: 2, name: "Two", url: "https://two.example/", folder_path: "Raindrop/A" },
  ];
  const w = boot({ hostReplies: (m) => m.op === "pending" ? { items, reset_folders: ["Raindrop"] } : { ok: true } });
  // Seed a stale managed tree with an item that is no longer staged.
  const rd = await w.sandbox.chrome.bookmarks.create({ parentId: "1", title: "Raindrop" });
  const old = await w.sandbox.chrome.bookmarks.create({ parentId: rd.id, title: "Old" });
  await w.sandbox.chrome.bookmarks.create({ parentId: old.id, title: "Stale", url: "https://stale.example/" });
  await w.sandbox.chrome.bookmarks.create({ parentId: "1", title: "Hand Curated" });
  await w.fire("alarm");
  const paths = w.paths();
  assert(!paths["https://stale.example/"], "the stale managed subtree is gone");
  assert(eq(paths["https://one.example/"], ["/Raindrop/A/One"]) && eq(paths["https://two.example/"], ["/Raindrop/A/Two"]), "the folder is rebuilt with the staged items");
  assert([...w.nodes.values()].some(n => n.title === "Hand Curated"), "a sibling folder the user made is untouched");
}

// --- 6. the folder rule matches the shared table -----------------------------------------------
{
  const w = boot({ hostReplies: () => ({ items: [] }) });
  const norm = w.sandbox.normalizeFolderPath;
  let agree = 0;
  for (const c of CASES) if (eq(norm(c.path, "Bookmarks Bar"), c.parts || [])) agree += 1;
  assert(agree === CASES.length, `normalizeFolderPath agrees with folder_paths.json on ${agree}/${CASES.length} cases`);
}

console.log(`\n${checks - failures} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
