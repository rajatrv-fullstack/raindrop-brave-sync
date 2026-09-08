#!/usr/bin/env python3
"""
Apply staged Raindrop promotions into Brave's bookmark tree.

Ownership is by MARKER, not by folder: every node this script creates carries
meta_info={"raindrop_sync": "v1"}. It may only ever add or remove MARKED nodes.
Unmarked nodes -- everything the user curated by hand -- are untouchable.

Per user decision (2026-09-05) this does NOT wait for Brave to be closed. Brave
re-serializes its in-memory tree over the file ~2.5s after any bookmark change and
at quit, which can silently drop a write made while it runs. That is not a data-loss
risk to the user's bookmarks (Brave's memory copy is authoritative and intact); it
only costs our pending additions. So we write anyway and re-apply on the next tick
until the marked nodes persist. Both writers are atomic, so the file is never torn.

Exit codes: 0 ok / nothing to do · 1 hard failure (backup restored) · 2 preflight abort
"""
import json, hashlib, uuid, time, os, sys, shutil, tempfile, subprocess
from datetime import datetime, timezone

HOME     = os.path.expanduser("~")
PROFILE  = os.environ.get("RAINDROP_SYNC_PROFILE",
           f"{HOME}/Library/Application Support/BraveSoftware/Brave-Browser/Default")
BOOKMARKS= f"{PROFILE}/Bookmarks"
BAK      = f"{PROFILE}/Bookmarks.bak"
ROOT     = os.environ.get("RAINDROP_SYNC_ROOT", f"{HOME}/.raindrop-sync")
DESIRED  = f"{ROOT}/desired.json"
BACKUPS  = f"{ROOT}/backups"
APPLIED  = f"{ROOT}/applied.json"
MARKER   = {"raindrop_sync": "v1"}
NS       = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
KEEP     = 10

def log(m): print(f"{datetime.now(timezone.utc).isoformat()} {m}", flush=True)

def checksum(doc):
    m = hashlib.md5()
    u8  = lambda s: m.update(s.encode("utf-8"))
    u16 = lambda s: m.update(s.encode("utf-16-le", "surrogatepass"))
    def node(n):
        u8(n["id"]); u16(n.get("name", "")); u8(n["type"])
        if n["type"] == "url": u8(n.get("url", ""))
        else:
            for c in n.get("children", []): node(c)
    for k in ("bookmark_bar", "other", "synced"): node(doc["roots"][k])
    return m.hexdigest()

def webkit_now(): return str(int(time.time() * 1_000_000) + 11_644_473_600_000_000)
def marked(n):    return (n.get("meta_info") or {}).get("raindrop_sync") == "v1"

def walk(n, fn, path=""):
    p = f"{path}/{n['name']}" if path else n["name"]
    fn(n, p)
    for c in n.get("children", []): walk(c, fn, p)

def preflight():
    if not os.path.exists(BOOKMARKS):
        log("ABORT: Bookmarks missing. Refusing to create one."); sys.exit(2)
    for bad in ("AccountBookmarks", "EncryptedBookmarks2", "EncryptedAccountBookmarks2"):
        if os.path.exists(f"{PROFILE}/{bad}"):
            log(f"ABORT: {bad} present (account/encrypted storage out of scope)."); sys.exit(2)
    doc = json.load(open(BOOKMARKS))
    if doc.get("version") != 1:
        log(f"ABORT: unexpected version {doc.get('version')}"); sys.exit(2)
    for r in ("bookmark_bar", "other", "synced"):
        if r not in doc.get("roots", {}):
            log(f"ABORT: missing root {r}"); sys.exit(2)
    if "sync_metadata" in doc:
        log("ABORT: Brave Sync is enabled. External nodes would corrupt sync metadata "
            "and upload to every device. Disable bookmark sync or remove this job."); sys.exit(2)
    if checksum(doc) != doc.get("checksum"):
        log("ABORT: stored checksum does not match. Something else is editing this file."); sys.exit(2)
    return doc

def backup():
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = f"{BACKUPS}/Bookmarks.{stamp}"
    shutil.copy2(BOOKMARKS, dst)
    if os.path.exists(BAK): shutil.copy2(BAK, f"{BACKUPS}/Bookmarks.bak.{stamp}")
    json.load(open(dst))                       # prove the backup parses
    olds = sorted(f for f in os.listdir(BACKUPS) if f.startswith("Bookmarks."))
    for f in olds[:-KEEP * 2]: os.remove(f"{BACKUPS}/{f}")
    return dst

def atomic_write(doc):
    fd, tmp = tempfile.mkstemp(dir=PROFILE, prefix=".bm-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False)
            f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, BOOKMARKS)
        dfd = os.open(PROFILE, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
    except Exception:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

def find_folder(bar, parts):
    """Resolve a '/'-separated path under the bookmark bar, creating marked folders as needed.

    The path is relative to the bar. A leading "Bookmarks Bar" is tolerated and stripped --
    without this, the bar's own name is treated as a child and a duplicate nested tree is
    built alongside the user's real folders.
    """
    parts = list(parts)
    while parts and parts[0] in (bar.get("name"), "Bookmarks Bar", ""):
        parts.pop(0)
    node, created = bar, []
    for depth, part in enumerate(parts):
        nxt = next((c for c in node.get("children", [])
                    if c["type"] == "folder" and c["name"] == part), None)
        if nxt is None:
            sofar = "/".join(parts[:depth + 1])
            nxt = {"children": [], "date_added": webkit_now(), "date_modified": webkit_now(),
                   "guid": str(uuid.uuid5(NS, "folder:" + sofar)),
                   "id": None, "meta_info": dict(MARKER), "name": part, "type": "folder"}
            node.setdefault("children", []).append(nxt)
            created.append(sofar)
        node = nxt
    return node, created

def main():
    if not os.path.exists(DESIRED):
        log("no desired.json; nothing to do"); return 0
    desired = json.load(open(DESIRED))
    if not desired:
        log("desired.json empty; nothing to do"); return 0

    doc = preflight()
    bar = doc["roots"]["bookmark_bar"]

    # index existing state
    ids, guids, by_guid = set(), set(), {}
    def idx(n, p):
        ids.add(int(n["id"])) if n.get("id") else None
        if n.get("guid"): guids.add(n["guid"]); by_guid[n["guid"]] = n
    for r in ("bookmark_bar", "other", "synced"): walk(doc["roots"][r], idx)

    # Existing URLs anywhere in the tree. The extension (which applies through
    # chrome.bookmarks) cannot write our meta_info marker, so a GUID-only check would not
    # see its work and we would happily create a duplicate. Match on URL as well.
    urls = set()
    for r in ("bookmark_bar", "other", "synced"):
        walk(doc["roots"][r], lambda n, p: urls.add(n["url"]) if n.get("url") else None)

    wanted = {str(uuid.uuid5(NS, "url:" + str(d["raindrop_id"]))): d for d in desired}
    already = {g for g, d in wanted.items()
               if (g in by_guid and marked(by_guid[g])) or d["url"] in urls}
    todo = {g: d for g, d in wanted.items() if g not in already}
    if not todo:
        log(f"all {len(wanted)} promotions already present and marked; no write needed")
        return 0

    log(f"{len(todo)} to apply ({len(already)} already present)")
    bk = backup(); log(f"backup: {bk}")

    next_id = max(ids) + 1 if ids else 1
    created_folders = []
    for guid, d in todo.items():
        folder, created = find_folder(bar, d["folder_path"].split("/"))
        created_folders += created
        node = {"date_added": webkit_now(), "date_last_used": "0", "guid": guid,
                "id": None, "meta_info": dict(MARKER), "name": d["name"],
                "type": "url", "url": d["url"]}
        folder.setdefault("children", []).append(node)

    # assign ids to every node still missing one, in tree order
    def assign(n, p):
        nonlocal next_id
        if n.get("id") is None:
            n["id"] = str(next_id); next_id += 1
    for r in ("bookmark_bar", "other", "synced"): walk(doc["roots"][r], assign)

    doc["checksum"] = checksum(doc)
    try:
        atomic_write(doc)
    except Exception as e:
        log(f"WRITE FAILED: {e}; restoring backup"); shutil.copy2(bk, BOOKMARKS); return 1

    # verify from disk
    try:
        v = json.load(open(BOOKMARKS))
        assert checksum(v) == v["checksum"], "checksum mismatch after write"
        vids, vguids = [], []
        for r in ("bookmark_bar", "other", "synced"):
            walk(v["roots"][r], lambda n, p: (vids.append(n["id"]), vguids.append(n.get("guid"))))
        assert len(vids) == len(set(vids)), "duplicate ids"
        gs = [g for g in vguids if g]
        assert len(gs) == len(set(gs)), "duplicate guids"
        vurls = set()
        for r in ("bookmark_bar", "other", "synced"):
            walk(v["roots"][r], lambda n, p: vurls.add(n["url"]) if n.get("url") else None)
        gset = set(gs)
        landed = sum(1 for g, d in wanted.items() if g in gset or d["url"] in vurls)
        assert landed == len(wanted), f"only {landed}/{len(wanted)} landed"
    except Exception as e:
        log(f"VERIFY FAILED: {e}; restoring backup"); shutil.copy2(bk, BOOKMARKS); return 1

    json.dump({"applied_guids": sorted(wanted), "at": datetime.now(timezone.utc).isoformat()},
              open(APPLIED, "w"), indent=1)
    for f in created_folders: log(f"created folder: {f}")
    log(f"OK: {len(todo)} applied, {len(wanted)} total marked nodes present")
    if subprocess.run(["pgrep", "-x", "Brave Browser"], capture_output=True).returncode == 0:
        log("NOTE: Brave is running. It may overwrite this from memory; the next tick re-applies.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
