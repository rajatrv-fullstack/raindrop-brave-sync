/**
 * Raindrop Sync - applies staged promotions into the RUNNING browser via chrome.bookmarks.
 *
 * Why this exists: Brave reads its Bookmarks file exactly once at startup and treats its
 * in-memory model as authoritative, so an external file write while Brave runs can be
 * silently discarded. Going through the bookmarks API writes to that model directly - * no file editing, no checksum, no race with Brave, visible immediately.
 *
 * Ownership: the API exposes no custom metadata field (unlike the file format's meta_info),
 * so ownership lives in the native host's ledger, keyed by raindrop_id. This worker never
 * deletes anything; it only creates what is missing.
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

/** Resolve a "/"-separated path under the bar, creating folders as needed. */
async function resolveFolder(pathStr) {
  const start = await bar();
  let node = start;
  const created = [];
  const parts = pathStr.split("/").filter(p => p && p !== start.title && p !== "Bookmarks Bar");
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
 * would be destroyed, so the host only names folders this tool owns end to end.
 */
async function resetFolder(name) {
  const start = await bar();
  const children = await chrome.bookmarks.getChildren(start.id);
  const target = children.find(c => !c.url && c.title === name);
  if (!target) return false;
  await chrome.bookmarks.removeTree(target.id);
  console.log(`raindrop-sync: reset folder "${name}"`);
  return true;
}

async function exists(url) {
  try {
    const hits = await chrome.bookmarks.search({ url });
    return hits.some(h => h.url === url);
  } catch { return false; }
}

async function runSync(reason) {
  let pending;
  try {
    pending = await chrome.runtime.sendNativeMessage(HOST, { op: "pending", client: CLIENT });
  } catch (e) {
    console.warn("raindrop-sync: native host unreachable:", String(e?.message || e));
    return;
  }
  // A rebuild wipes the managed folder first, so the exists() check must not then skip
  // everything it just removed.
  const resets = (pending && pending.reset_folders) || [];
  for (const f of resets) { try { await resetFolder(f); } catch (e) { console.warn("reset failed", f, e); } }

  const items = (pending && pending.items) || [];
  if (!items.length) {
    await chrome.storage.local.set({ lastRun: { at: Date.now(), reason, applied: 0 } });
    return;
  }

  const results = [];
  for (const it of items) {
    try {
      if (await exists(it.url)) {                 // never duplicate, whoever created it
        results.push({ raindrop_id: it.raindrop_id, status: "already_present" });
        continue;
      }
      const { folder, created } = await resolveFolder(it.folder_path);
      const node = await chrome.bookmarks.create({
        parentId: folder.id, title: it.name, url: it.url
      });
      results.push({
        raindrop_id: it.raindrop_id, status: "created",
        bookmark_id: node.id, created_folders: created
      });
    } catch (e) {
      results.push({ raindrop_id: it.raindrop_id, status: "error", error: String(e?.message || e) });
    }
  }

  try {
    await chrome.runtime.sendNativeMessage(HOST, { op: "applied", client: CLIENT, results });
  } catch (e) {
    console.warn("raindrop-sync: could not report results:", String(e?.message || e));
  }
  const made = results.filter(r => r.status === "created").length;
  await chrome.storage.local.set({ lastRun: { at: Date.now(), reason, applied: made, results } });
  console.log(`raindrop-sync[${CLIENT}]: ${made} created of ${items.length} pending (${reason})`);
}

const sync = reason => serialize(() => runSync(reason));

// Re-arm on every worker start, not just onInstalled: creating an alarm with an existing
// name replaces it, so this is idempotent, and it guarantees a changed PERIOD_MINUTES takes
// effect even if onInstalled does not fire.
chrome.alarms.create("sync", { periodInMinutes: PERIOD_MINUTES });
sync("load");

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("sync", { periodInMinutes: PERIOD_MINUTES });
  sync("installed");
});
chrome.runtime.onStartup.addListener(() => sync("startup"));
chrome.alarms.onAlarm.addListener(a => { if (a.name === "sync") sync("alarm"); });
