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

Profile: RAINDROP_SYNC_PROFILE, else "profile" in $ROOT/config.json (written by
bootstrap), else Brave-Browser/Default.

Exit codes: 0 ok / nothing to do · 1 hard failure (backup restored) · 2 preflight abort
"""
import json, hashlib, uuid, time, os, sys, shutil, tempfile, re
import subprocess  # nosec B404 - fixed argv only, see is_brave_running()
from datetime import datetime, timezone

def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

HOME     = os.path.expanduser("~")
ROOT     = os.environ.get("RAINDROP_SYNC_ROOT", f"{HOME}/.raindrop-sync")

def resolve_profile(root, env=None):
    """The Brave profile directory: env, then config.json, then the stock Default path."""
    env = os.environ if env is None else env
    p = env.get("RAINDROP_SYNC_PROFILE")
    if p:
        return p
    try:
        cfg = read_json(f"{root}/config.json")
        p = cfg.get("profile") if isinstance(cfg, dict) else None
        if isinstance(p, str) and p:
            return p
    except (OSError, ValueError):
        pass  # no config.json, or not JSON: an install older than 1.1.0, so use the stock path
    return f"{HOME}/Library/Application Support/BraveSoftware/Brave-Browser/Default"

PROFILE  = resolve_profile(ROOT)
BOOKMARKS= f"{PROFILE}/Bookmarks"
BAK      = f"{PROFILE}/Bookmarks.bak"
DESIRED  = f"{ROOT}/desired.json"
BACKUPS  = f"{ROOT}/backups"
APPLIED  = f"{ROOT}/applied.json"
MARKER   = {"raindrop_sync": "v1"}
NS       = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
KEEP     = 10
MAX_LOG  = 1_048_576          # rotate a launchd log to <name>.1 once it passes 1 MB
BRAVE_PROCESSES = ("Brave Browser", "Brave Browser Beta", "Brave Browser Nightly")

def log(m): print(f"{datetime.now(timezone.utc).isoformat()} {m}", flush=True)

def rotate_logs():
    """Rotate the launchd-owned logs. launchd opens StandardOutPath/StandardErrorPath itself,
    so this process cannot reopen them: the rename takes effect on the next run, whose fresh
    file starts empty, and this run's remaining output lands in the .1 copy."""
    for name in ("apply.out.log", "apply.err.log"):
        p = f"{ROOT}/log/{name}"
        try:
            if os.path.getsize(p) > MAX_LOG:
                os.replace(p, p + ".1")
                log(f"rotated {name} to {name}.1; the fresh log starts on the next run")
        except OSError:
            pass  # the log does not exist yet (first run, or not under launchd): nothing to rotate

def checksum(doc):
    m = hashlib.md5(usedforsecurity=False)  # Chromium's file checksum, not a security use
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

def is_brave_running():
    # Fixed argv, absolute path, no shell: nothing here is derived from input.
    for name in BRAVE_PROCESSES:
        if subprocess.run(["/usr/bin/pgrep", "-x", name],  # nosec B603
                          capture_output=True, check=False).returncode == 0:
            return True
    return False

# --- shared rules ---------------------------------------------------------------------------
# canon() and norm_folder_path() are duplicated verbatim in native_host.py: the two files are
# installed separately and share no module. tests/ asserts the two copies agree.

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(.*)$", re.S)

def _split_netloc(netloc):
    """(userinfo-with-@, host, port-with-colon) for the authority part of a URL."""
    userinfo, at, hostport = netloc.rpartition("@")
    if hostport.startswith("["):
        end = hostport.find("]")
        if end == -1:
            return userinfo + at, "", hostport
        return userinfo + at, hostport[:end + 1], hostport[end + 1:]
    host, colon, port = hostport.partition(":")
    return userinfo + at, host, colon + port

def canon(url):
    """The comparison form of a URL, or None when the item must be rejected.

    Lower-cases the scheme and the host, turns an empty path into "/", and leaves everything
    else as written (query, fragment, port, userinfo, percent-encoding). Surrounding
    whitespace and embedded tab/CR/LF are dropped first, as every URL parser does. None when
    the value is not a string, has no scheme, or has no host and is not a file: URL.
    """
    if not isinstance(url, str):
        return None
    s = "".join(c for c in url.strip() if c not in "\t\r\n")
    m = _SCHEME.match(s)
    if not m:
        return None
    scheme, tail = m.group(1).lower(), m.group(2)
    authority = tail.startswith("//")
    if authority:
        end = len(tail)
        for ch in "/?#":
            k = tail.find(ch, 2)
            if k != -1:
                end = min(end, k)
        netloc, rest = tail[2:end], tail[end:]
    else:
        netloc, rest = "", tail
    userinfo, host, port = _split_netloc(netloc)
    if not host and scheme != "file":
        return None
    if rest == "" or rest[0] in "?#":
        rest = "/" + rest
    return scheme + ":" + ("//" + userinfo + host.lower() + port if authority else "") + rest

def norm_folder_path(path):
    """The folder rule shared by the host, the file writer and the extension.

    Split on "/", trim each segment, drop empty segments, strip ONE leading "Bookmarks Bar"
    segment. Returns the remaining segments, or None when nothing remains (the item is then
    rejected) or the value is not a string or list of strings.
    """
    if isinstance(path, str):
        path = path.split("/")
    elif not isinstance(path, list):
        return None
    parts = [p.strip() for p in path if isinstance(p, str)]
    parts = [p for p in parts if p]
    if parts and parts[0] == "Bookmarks Bar":
        parts.pop(0)
    return parts or None

# --- the writer -----------------------------------------------------------------------------

def preflight():
    if not os.path.exists(BOOKMARKS):
        log(f"ABORT: Bookmarks missing at {BOOKMARKS}. Refusing to create one."); sys.exit(2)
    for bad in ("AccountBookmarks", "EncryptedBookmarks2", "EncryptedAccountBookmarks2"):
        if os.path.exists(f"{PROFILE}/{bad}"):
            log(f"ABORT: {bad} present (account/encrypted storage out of scope)."); sys.exit(2)
    try:
        doc = read_json(BOOKMARKS)
    except (OSError, ValueError) as e:
        log(f"ABORT: Bookmarks is not readable JSON ({e}). Refusing to touch it."); sys.exit(2)
    if not isinstance(doc, dict) or not isinstance(doc.get("roots"), dict):
        log("ABORT: Bookmarks has no roots object. Refusing to touch it."); sys.exit(2)
    if doc.get("version") != 1:
        log(f"ABORT: unexpected version {doc.get('version')}"); sys.exit(2)
    for r in ("bookmark_bar", "other", "synced"):
        if r not in doc.get("roots", {}):
            log(f"ABORT: missing root {r}"); sys.exit(2)
    if "sync_metadata" in doc:
        log("ABORT: Brave Sync is enabled. External nodes would corrupt sync metadata "
            "and upload to every device. Disable bookmark sync or remove this job."); sys.exit(2)
    try:
        computed = checksum(doc)
    except (KeyError, TypeError, AttributeError, UnicodeEncodeError) as e:
        # UnicodeEncodeError: an unpaired surrogate in an id or URL. Chromium stores those as
        # valid UTF-8, so the file was not written by the browser. Found by fuzz/fuzz_bookmarks.py.
        # A node without id/name/type, or children that are not a list: the tree is not one
        # Chromium wrote. Writing anything back would risk the fresh-profile failure (finding 6).
        log(f"ABORT: bookmark tree is malformed ({e.__class__.__name__}: {e}). Refusing to touch it."); sys.exit(2)
    if computed != doc.get("checksum"):
        log("ABORT: stored checksum does not match. Something else is editing this file."); sys.exit(2)
    return doc

def backup():
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = f"{BACKUPS}/Bookmarks.{stamp}"
    shutil.copy2(BOOKMARKS, dst)
    if os.path.exists(BAK): shutil.copy2(BAK, f"{BACKUPS}/Bookmarks.bak.{stamp}")
    read_json(dst)                       # prove the backup parses
    # Prune per prefix. "Bookmarks.bak.<stamp>" sorts after every "Bookmarks.<stamp>", so a
    # single sorted list would discard primary copies first and keep only .bak copies.
    for prefix, other in (("Bookmarks.bak.", None), ("Bookmarks.", "Bookmarks.bak.")):
        names = sorted(f for f in os.listdir(BACKUPS)
                       if f.startswith(prefix) and not (other and f.startswith(other)))
        for f in names[:-KEEP]: os.remove(f"{BACKUPS}/{f}")
    return dst

def atomic_write(doc):
    fd, tmp = tempfile.mkstemp(dir=PROFILE, prefix=".bm-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                json.dump(doc, f, ensure_ascii=False)
            except UnicodeEncodeError:
                # A title with an unpaired surrogate (Chromium tolerates these; checksum() hashes
                # them with surrogatepass). Raw UTF-8 cannot carry it, so fall back to JSON
                # escapes: valid UTF-8 on disk, and Chromium's reader maps \udXXX to U+FFFD.
                f.seek(0); f.truncate()
                json.dump(doc, f, ensure_ascii=True)
            f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, BOOKMARKS)
        dfd = os.open(PROFILE, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
    except Exception:
        if os.path.exists(tmp): os.unlink(tmp)
        raise

def bar_relative(parts, bar_name=None):
    """norm_folder_path() that also tolerates the bar's real (possibly localised) name as the
    leading segment. Exactly one leading segment is ever stripped."""
    if isinstance(parts, str):
        parts = parts.split("/")
    if not isinstance(parts, list):
        return None
    parts = [p.strip() for p in parts if isinstance(p, str)]
    parts = [p for p in parts if p]
    if parts and bar_name and bar_name != "Bookmarks Bar" and parts[0] == bar_name:
        return parts[1:] or None
    return norm_folder_path(parts)

def find_folder(bar, parts):
    """Resolve a folder path under the bookmark bar, creating marked folders as needed.

    `parts` is a "/"-separated string or a list of segments, relative to the bar, normalised
    with the shared folder rule. The bar's own name is tolerated as the leading segment the
    same way "Bookmarks Bar" is -- without this, the bar's name is treated as a child and a
    duplicate nested tree is built alongside the user's real folders.
    """
    parts = bar_relative(parts, bar.get("name"))
    if parts is None:
        raise ValueError("folder_path resolves to the bar itself")
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

def usable(d, bar_name):
    """None when a staged entry can be applied, else the one-line reason it is skipped."""
    if not isinstance(d, dict) or "raindrop_id" not in d:
        return "not a staged entry"
    if canon(d.get("url")) is None:
        return f"invalid url {d.get('url')!r}"
    if bar_relative(d.get("folder_path"), bar_name) is None:
        return f"empty folder_path {d.get('folder_path')!r}"
    return None

def main():
    rotate_logs()
    if not os.path.exists(DESIRED):
        log("no desired.json; nothing to do"); return 0
    desired = read_json(DESIRED)
    if not desired:
        log("desired.json empty; nothing to do"); return 0

    doc = preflight()
    bar = doc["roots"]["bookmark_bar"]

    # An entry the browser would refuse is skipped, once per run, and never blocks the rest.
    kept = []
    for d in desired if isinstance(desired, list) else []:
        why = usable(d, bar.get("name"))
        if why:
            log(f"skipping {d.get('raindrop_id') if isinstance(d, dict) else '?'}: {why}")
        else:
            kept.append(d)
    desired = kept
    if not desired:
        log("desired.json has no usable entries; nothing to do"); return 0

    # index existing state
    ids, guids, by_guid = set(), set(), {}
    def idx(n, p):
        ids.add(int(n["id"])) if n.get("id") else None
        if n.get("guid"): guids.add(n["guid"]); by_guid[n["guid"]] = n
    for r in ("bookmark_bar", "other", "synced"): walk(doc["roots"][r], idx)

    # Existing URLs anywhere in the tree, in comparison form. The extension (which applies
    # through chrome.bookmarks) cannot write our meta_info marker, so a GUID-only check would
    # not see its work and we would happily create a duplicate. Match on URL as well, and
    # canonically: Brave stores "https://www.example.org/" for a staged "https://WWW.example.org".
    urls = set()
    def collect(n, p):
        c = canon(n.get("url")) if n.get("url") else None
        if c: urls.add(c)
    for r in ("bookmark_bar", "other", "synced"): walk(doc["roots"][r], collect)

    wanted = {str(uuid.uuid5(NS, "url:" + str(d["raindrop_id"]))): d for d in desired}
    already = {g for g, d in wanted.items()
               if (g in by_guid and marked(by_guid[g])) or canon(d["url"]) in urls}
    todo = {g: d for g, d in wanted.items() if g not in already}
    if not todo:
        log(f"all {len(wanted)} promotions already present and marked; no write needed")
        return 0

    log(f"{len(todo)} to apply ({len(already)} already present)")
    bk = backup(); log(f"backup: {bk}")

    next_id = max(ids) + 1 if ids else 1
    created_folders = []
    for guid, d in todo.items():
        folder, created = find_folder(bar, d["folder_path"])
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
        v = read_json(BOOKMARKS)
        if checksum(v) != v["checksum"]:
            raise RuntimeError("checksum mismatch after write")
        vids, vguids = [], []
        for r in ("bookmark_bar", "other", "synced"):
            walk(v["roots"][r], lambda n, p: (vids.append(n["id"]), vguids.append(n.get("guid"))))
        if len(vids) != len(set(vids)):
            raise RuntimeError("duplicate ids")
        gs = [g for g in vguids if g]
        if len(gs) != len(set(gs)):
            raise RuntimeError("duplicate guids")
        vurls = set()
        def vcollect(n, p):
            c = canon(n.get("url")) if n.get("url") else None
            if c: vurls.add(c)
        for r in ("bookmark_bar", "other", "synced"): walk(v["roots"][r], vcollect)
        gset = set(gs)
        landed = sum(1 for g, d in wanted.items() if g in gset or canon(d["url"]) in vurls)
        if landed != len(wanted):
            raise RuntimeError(f"only {landed}/{len(wanted)} landed")
    except Exception as e:
        log(f"VERIFY FAILED: {e}; restoring backup"); shutil.copy2(bk, BOOKMARKS); return 1

    with open(APPLIED, "w", encoding="utf-8") as f:
        json.dump({"applied_guids": sorted(wanted), "at": datetime.now(timezone.utc).isoformat()}, f, indent=1)
    for f in created_folders: log(f"created folder: {f}")
    log(f"OK: {len(todo)} applied, {len(wanted)} total marked nodes present")
    if is_brave_running():
        log("NOTE: Brave is running. It may overwrite this from memory; the next tick re-applies.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
