/**
 * Raindrop Sync - applies staged promotions into the RUNNING browser via chrome.bookmarks.
 *
 * Why this exists: Brave reads its Bookmarks file exactly once at startup and treats its
 * in-memory model as authoritative, so an external file write while Brave runs can be
 * silently discarded. Going through the bookmarks API writes to that model directly:
 * no file editing, no checksum, no race with Brave, visible immediately.
 *
 * Ownership: the API exposes no custom metadata field (unlike the file format's meta_info),
 * so ownership lives in the native host's ledger, keyed by raindrop_id. This worker never
 * deletes anything on its own; it only creates what is missing, and tears down a managed
 * folder only when the host names it in a reset.
 *
 * NOTE: several lifecycle events can fire within the same millisecond (worker load +
 * onInstalled). Without the lock below, each one independently sees the bookmark as absent
 * and creates it - which produced a real duplicate in testing. Every sync is serialized.
 */
const HOST = "com.raindrop_sync.host";
/**
 * Poll interval. Chromium clamps alarms to a 1-minute floor, so this is as close to instant
 * as the alarms API allows. Each tick spawns the native host for ~30ms and exits, so the
 * cost of the faster cadence is negligible. For genuinely instant application the host would
 * need a long-lived connectNative port to push on desired.json changes.
 */
const PERIOD_MINUTES = 1;
/**
 * When the host cannot be reached (not installed yet, manifest in the wrong directory,
 * python missing) the failure is persisted and alarm ticks are skipped for a growing
 * interval: 1, 2, 4, 8 minutes, then every 15. onInstalled and onStartup always try.
 */
const MIN_BACKOFF_MS = 60 * 1000;
const MAX_BACKOFF_MS = 15 * 60 * 1000;
const BACKOFF_KEYS = ["lastError", "at", "nextAttemptAt", "backoffMs"];

/**
 * Which browser this copy is running in. The native host keeps a separate applied-set per
 * client - without it, the first browser to sync would record the item and every other
 * browser would be told nothing is pending, silently never receiving it.
 * Brave exposes navigator.brave; its user-agent deliberately says "Chrome".
 */
const CLIENT = ("brave" in navigator) ? "brave"
             : /Edg\//.test(navigator.userAgent) ? "edge"
             : "chrome";

let chain = Promise.resolve();
/** Serialize all syncs through one promise chain; concurrent callers queue behind it. */
function serialize(fn) {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

async function bar() {
  const [root] = await chrome.bookmarks.getTree();
  const kids = root.children || [];
  return kids.find(c => c.folderType === "bookmarks-bar")
      || kids.find(c => c.id === "1")
      || kids[0];
}

/**
 * The folder rule shared with native_host.op_pending and apply_brave.find_folder: split on
 * "/", trim each segment, drop empty segments, strip ONE leading "Bookmarks Bar" (or the
 * bar's real title). An empty result means the item is rejected, never "the bar itself".
 */
function normalizeFolderPath(pathStr, barTitle) {
  if (typeof pathStr !== "string") return [];
  const parts = pathStr.split("/").map(p => p.trim()).filter(p => p);
  if (parts.length && (parts[0] === "Bookmarks Bar" || parts[0] === barTitle)) parts.shift();
  return parts;
}

/** Resolve normalised path segments under the bar, creating folders as needed. */
async function resolveFolder(start, parts) {
  let node = start;
  const created = [];
  for (const part of parts) {
    const children = await chrome.bookmarks.getChildren(node.id);
    let next = children.find(c => !c.url && c.title === part);
    if (!next) {
      next = await chrome.bookmarks.create({ parentId: node.id, title: part });
      created.push(part);
    }
    node = next;
  }
  return { folder: node, created };
}

/**
 * Remove a managed folder outright so it can be rebuilt. Only ever called for a folder the
 * host explicitly names AND that sits as a direct child of the bookmarks bar - never for the
 * user's own curated folders. Any child that is itself a folder the user created inside it
 * would be destroyed, so the host only names folders this tool owns end to end. The host
 * clears its ledger for everything under the folder in the same reply, so the items are
 * re-created below.
 */
async function resetFolder(start, name) {
  const children = await chrome.bookmarks.getChildren(start.id);
  const target = children.find(c => !c.url && c.title === name);
  if (!target) return false;
  await chrome.bookmarks.removeTree(target.id);
  console.log(`raindrop-sync: reset folder "${name}"`);
  return true;
}

/**
 * chrome.bookmarks.search({url}) matches on the canonical form Chromium stores, so a staged
 * "https://WWW.example.org" finds the node saved as "https://www.example.org/". Trust it.
 */
async function exists(url) {
  try {
    const hits = await chrome.bookmarks.search({ url });
    return hits.length > 0;
  } catch { return false; }
}

/** Persist a failed host call and push the next alarm attempt out, doubling up to the cap. */
async function hostUnreachable(e, what) {
  const msg = String(e?.message || e);
  const { backoffMs } = await chrome.storage.local.get("backoffMs");
  const wait = Math.min(backoffMs ? backoffMs * 2 : MIN_BACKOFF_MS, MAX_BACKOFF_MS);
  const at = Date.now();
  await chrome.storage.local.set({ lastError: msg, at, nextAttemptAt: at + wait, backoffMs: wait });
  console.warn(`raindrop-sync: native host unreachable (${what}): ${msg}; next attempt in ${wait / 60000} min`);
}

async function runSync(reason) {
  if (reason === "alarm") {
    const { nextAttemptAt } = await chrome.storage.local.get("nextAttemptAt");
    if (nextAttemptAt && Date.now() < nextAttemptAt) {
      console.log(`raindrop-sync: host backoff until ${new Date(nextAttemptAt).toISOString()}; tick skipped`);
      return;
    }
  }
  let pending;
  try {
    pending = await chrome.runtime.sendNativeMessage(HOST, { op: "pending", client: CLIENT });
  } catch (e) {
    await hostUnreachable(e, "pending");
    return;
  }
  // The host answered: whatever backoff was in force is over.
  await chrome.storage.local.remove(BACKOFF_KEYS);
  if (pending && pending.error) {
    console.warn(`raindrop-sync: host reported: ${pending.error}`);
    await chrome.storage.local.set({ lastError: String(pending.error), at: Date.now() });
  }

  const start = await bar();
  // A rebuild wipes the managed folder first, so the exists() check must not then skip
  // everything it just removed.
  const resets = (pending && pending.reset_folders) || [];
  for (const f of resets) { try { await resetFolder(start, f); } catch (e) { console.warn("reset failed", f, e); } }

  const items = (pending && pending.items) || [];
  if (!items.length) {
    await chrome.storage.local.set({ lastRun: { at: Date.now(), reason, applied: 0, reported: true } });
    return;
  }

  const results = [];
  for (const it of items) {
    try {
      // Validate before touching the tree: an item Chromium would refuse must not leave an
      // empty folder behind, and it is reported as rejected so the host retires it.
      try {
        new URL(it.url);
      } catch (e) {
        results.push({ raindrop_id: it.raindrop_id, status: "rejected", error: `invalid url: ${String(e?.message || e)}` });
        continue;
      }
      const parts = normalizeFolderPath(it.folder_path, start.title);
      if (!parts.length) {
        results.push({ raindrop_id: it.raindrop_id, status: "rejected", error: "empty folder_path" });
        continue;
      }
      if (await exists(it.url)) {                 // never duplicate, whoever created it
        results.push({ raindrop_id: it.raindrop_id, status: "already_present" });
        continue;
      }
      const { folder, created } = await resolveFolder(start, parts);
      const node = await chrome.bookmarks.create({
        parentId: folder.id, title: it.name, url: it.url
      });
      results.push({
        raindrop_id: it.raindrop_id, status: "created",
        bookmark_id: node.id, url: node.url, created_folders: created
      });
    } catch (e) {
      results.push({ raindrop_id: it.raindrop_id, status: "error", error: String(e?.message || e) });
    }
  }

  let ack = null;
  try {
    ack = await chrome.runtime.sendNativeMessage(HOST, { op: "applied", client: CLIENT, results });
  } catch (e) {
    await hostUnreachable(e, "applied");
  }
  const reported = !!(ack && ack.ok === true);
  if (!reported && ack) {
    const err = String(ack.error || "applied not acknowledged");
    console.warn(`raindrop-sync: host did not record results: ${err}`);
    await chrome.storage.local.set({ lastError: err, at: Date.now() });
  }
  const made = results.filter(r => r.status === "created").length;
  await chrome.storage.local.set({ lastRun: { at: Date.now(), reason, applied: made, reported, results } });
  console.log(`raindrop-sync[${CLIENT}]: ${made} created of ${items.length} pending (${reason}${reported ? "" : ", NOT recorded by host"})`);
}

const sync = reason => serialize(() => runSync(reason));

// Re-arm on every worker start, not just onInstalled: creating an alarm with an existing
// name replaces it, so this is idempotent, and it guarantees a changed PERIOD_MINUTES takes
// effect even if onInstalled does not fire. No sync here: MV3 re-evaluates this file on
// every wake, so a top-level sync would run a second full pass on every alarm tick.
chrome.alarms.create("sync", { periodInMinutes: PERIOD_MINUTES });

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("sync", { periodInMinutes: PERIOD_MINUTES });
  sync("installed");
});
chrome.runtime.onStartup.addListener(() => sync("startup"));
chrome.alarms.onAlarm.addListener(a => { if (a.name === "sync") sync("alarm"); });
