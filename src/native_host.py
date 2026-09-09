#!/usr/bin/env python3
"""
Native messaging host for the Raindrop Sync Brave extension.

Protocol: 4-byte native-order length prefix + UTF-8 JSON, on stdin/stdout.
NOTHING may be written to stdout except protocol messages - a stray print corrupts the
stream and Brave silently drops the connection. All diagnostics go to the log file.

Ops:
  {"op":"ping"}     -> {"ok":true,"version":...}
  {"op":"pending","client":"brave"}  -> {"items":[{raindrop_id,name,url,folder_path}, ...],
                                         "reset_folders":[...]}
  {"op":"applied","client":"brave","results":[...]} -> {"ok":true,"recorded":N}

Command line: "native_host.py --init-db" creates state.db with the full ledger schema and
exits 0 without touching stdout (bootstrap calls it; the schema is also created lazily).

Every browser running this extension keeps its OWN applied-set, keyed by `client`. Without
that, whichever browser synced first would record the item and every other browser would be
told there is nothing pending -- and would silently never receive it.

Ledger statuses: created, already_present, error, rejected. The first two and rejected are
terminal: the item is never offered to that client again. error is retried on the next tick.
"""
import sys, json, struct, sqlite3, os, re
from datetime import datetime, timezone

def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

VERSION = "1.1.0"
ROOT    = os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync"))
DESIRED = f"{ROOT}/desired.json"
DB      = f"{ROOT}/state.db"
LOG     = f"{ROOT}/log/native_host.log"
MAX_LOG = 1_048_576          # rotate the log to <name>.1 once it passes 1 MB
TERMINAL = ("created", "already_present", "rejected")

# The ledger schema. Phase A (the classifier) owns bookmarks/meta/runs; this host owns
# extension_applied. Created by --init-db and lazily by db(), so a fresh root always works.
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bookmarks(
        raindrop_id INTEGER PRIMARY KEY, title TEXT, link TEXT, content_hash TEXT,
        collection TEXT, tags TEXT, promoted INTEGER DEFAULT 0, taxonomy_version INTEGER,
        classified_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_hash ON bookmarks(content_hash)",
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
    """CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, classified_count INTEGER,
        promoted_count INTEGER, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS extension_applied(
        client TEXT NOT NULL, raindrop_id INTEGER NOT NULL,
        bookmark_id TEXT, url TEXT, status TEXT, applied_at TEXT,
        PRIMARY KEY (client, raindrop_id))""",
)

def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) > MAX_LOG:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass  # no log yet, or a rename race with another host process: nothing to rotate
        with open(LOG, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except OSError as e:
        # The log is best-effort; stderr is safe in a native host (only stdout is protocol).
        sys.stderr.write(f"native_host: log write failed: {e}\n")

def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    for stmt in SCHEMA:
        con.execute(stmt)
    con.commit()
    return con

# --- shared rules ---------------------------------------------------------------------------
# canon() and norm_folder_path() are duplicated verbatim in apply_brave.py: the two files are
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

# --- protocol -------------------------------------------------------------------------------

def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("@I", raw)
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))

def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()

def norm_client(c):
    c = c.strip().lower() if isinstance(c, str) else ""
    return c if c in ("brave", "chrome", "edge", "vivaldi", "opera") else "unknown"

def is_rid(v):
    """A usable raindrop_id: a JSON integer that fits SQLite's 64-bit INTEGER."""
    return isinstance(v, int) and not isinstance(v, bool) and -2**63 <= v < 2**63

def op_pending(client):
    if not os.path.exists(DESIRED):
        return {"items": []}
    try:
        desired = read_json(DESIRED)
    except Exception as e:
        log(f"desired.json unreadable: {e}")
        return {"items": [], "error": "desired unreadable"}
    if not isinstance(desired, list):
        log("desired.json is not a list")
        return {"items": [], "error": "desired.json is not a list"}

    con = db()
    rows = {rid: status for rid, status in con.execute(
        "SELECT raindrop_id, status FROM extension_applied WHERE client=?", (client,))}

    # Validate and normalise every staged entry before anything is handed out, so both
    # appliers see one folder rule and an item Chromium would refuse never reaches them.
    valid, malformed = [], 0
    now = datetime.now(timezone.utc).isoformat()
    for d in desired:
        rid = d.get("raindrop_id") if isinstance(d, dict) else None
        if not is_rid(rid):
            malformed += 1
            continue
        parts = norm_folder_path(d.get("folder_path"))
        reason = None
        if canon(d.get("url")) is None:
            reason = f"invalid url {d.get('url')!r}"
        elif parts is None:
            reason = f"empty folder_path {d.get('folder_path')!r}"
        if reason:
            # Recorded and logged once per client; a rejected row is terminal.
            if rows.get(rid) not in TERMINAL:
                con.execute("INSERT OR REPLACE INTO extension_applied VALUES(?,?,?,?,?,?)",
                            (client, rid, "", d.get("url") if isinstance(d.get("url"), str) else "",
                             "rejected", now))
                rows[rid] = "rejected"
                log(f"[{client}] rejected {rid}: {reason}")
            continue
        item = dict(d)
        item["folder_path"] = "/".join(parts)
        valid.append(item)

    # A reset file asks the extension to tear down and rebuild a managed folder, e.g. after
    # the folder layout changes. Everything this client applied under that folder is about
    # to be removed from its tree, so those ledger rows are dropped here and the same reply
    # offers the items again. Consumed once, by the applied that follows.
    resets = []
    rp = f"{ROOT}/reset_folders.json"
    if os.path.exists(rp):
        try:
            resets = read_json(rp)
        except Exception as e:
            log(f"reset_folders.json unreadable: {e}")
        if not (isinstance(resets, list) and all(isinstance(r, str) for r in resets)):
            log("reset_folders.json ignored: not a list of folder names")
            resets = []
    if resets:
        affected = [it["raindrop_id"] for it in valid
                    if it["folder_path"].split("/")[0] in resets
                    and rows.get(it["raindrop_id"]) not in (None, "rejected")]
        if affected:
            con.executemany("DELETE FROM extension_applied WHERE client=? AND raindrop_id=?",
                            [(client, rid) for rid in affected])
            for rid in affected:
                rows.pop(rid, None)
            log(f"[{client}] reset {resets}: cleared {len(affected)} ledger rows")
    con.commit(); con.close()

    items = [it for it in valid if rows.get(it["raindrop_id"]) not in TERMINAL]
    if not items:
        resets = []
    log(f"pending[{client}]: {len(items)} of {len(desired)} staged"
        + (f", reset {resets}" if resets else "")
        + (f", {malformed} malformed entries skipped" if malformed else ""))
    return {"items": items, "reset_folders": resets}

def op_applied(client, results):
    con = db()
    now = datetime.now(timezone.utc).isoformat()
    by_id = {}
    try:
        by_id = {d["raindrop_id"]: d for d in read_json(DESIRED) if isinstance(d, dict)}
    except (OSError, ValueError, KeyError) as e:
        log(f"desired.json unreadable while recording results: {e}")
    n = 0
    for r in results or []:
        rid = r.get("raindrop_id")
        if not is_rid(rid):
            continue
        status = r.get("status")
        if status not in TERMINAL and status != "error":
            status = "unknown"
        # A created result reports the URL as the browser stored it (canonical form), which
        # is the value worth keeping; otherwise fall back to what was staged.
        url = r.get("url") if isinstance(r.get("url"), str) else (by_id.get(rid) or {}).get("url", "")
        con.execute("INSERT OR REPLACE INTO extension_applied VALUES(?,?,?,?,?,?)",
                    (client, rid, str(r.get("bookmark_id") or ""),
                     url if isinstance(url, str) else "", status, now))
        n += 1
        if status == "error":
            log(f"[{client}] extension error on {rid}: {r.get('error')}")
        elif status == "rejected":
            log(f"[{client}] extension rejected {rid}: {r.get('error')}")
        for f in (r.get("created_folders") or []):
            log(f"[{client}] created folder: {f}")
    con.commit(); con.close()
    # A reset request is consumed exactly once: the extension has now acted on it.
    rp = f"{ROOT}/reset_folders.json"
    if os.path.exists(rp):
        try: os.remove(rp); log("reset_folders.json cleared")
        except OSError as e: log(f"could not clear reset_folders.json: {e}")
    log(f"applied[{client}]: recorded {n} results")
    return {"ok": True, "recorded": n}

def handle(msg):
    """Dispatch one decoded message and return the reply dict.

    Contract (enforced by tests and the fuzz harness): for ANY JSON value, this returns a
    dict and never raises. A malformed message is answered with ok false, not a crash.
    """
    if not isinstance(msg, dict):
        return {"ok": False, "error": "message must be a JSON object"}
    op = msg.get("op")
    try:
        if op == "ping":
            return {"ok": True, "version": VERSION}
        if op == "pending":
            return op_pending(norm_client(msg.get("client")))
        if op == "applied":
            results = msg.get("results")
            if not isinstance(results, list):
                return {"ok": False, "error": "results must be a list"}
            return op_applied(norm_client(msg.get("client")), [r for r in results if isinstance(r, dict)])
        log(f"unknown op {op!r}")
        return {"ok": False, "error": "unknown op"}
    except Exception as e:
        log(f"op {op!r} failed: {e}")
        return {"ok": False, "error": str(e)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--init-db" in argv:
        # Not a protocol session: stdout stays untouched, the report goes to stderr.
        try:
            db().close()
        except Exception as e:
            sys.stderr.write(f"native_host: could not initialise {DB}: {e}\n")
            return 1
        sys.stderr.write(f"native_host: ledger schema ready in {DB}\n")
        return 0
    log(f"host start (pid {os.getpid()})")
    while True:
        try:
            msg = read_message()
        except Exception as e:
            log(f"read error: {e}"); return 1
        if msg is None:
            log("stream closed"); return 0
        try:
            send_message(handle(msg))
        except Exception:
            return 1

if __name__ == "__main__":
    sys.exit(main())
