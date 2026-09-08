#!/usr/bin/env python3
"""
Native messaging host for the Raindrop Sync Brave extension.

Protocol: 4-byte native-order length prefix + UTF-8 JSON, on stdin/stdout.
NOTHING may be written to stdout except protocol messages — a stray print corrupts the
stream and Brave silently drops the connection. All diagnostics go to the log file.

Ops:
  {"op":"ping"}     -> {"ok":true,"version":...}
  {"op":"pending","client":"brave"}  -> {"items":[{raindrop_id,name,url,folder_path}, ...]}
  {"op":"applied","client":"brave","results":[...]} -> {"ok":true}

Every browser running this extension keeps its OWN applied-set, keyed by `client`. Without
that, whichever browser synced first would record the item and every other browser would be
told there is nothing pending -- and would silently never receive it.
"""
import sys, json, struct, sqlite3, os
from datetime import datetime, timezone

VERSION = "1.0.0"
ROOT    = os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync"))
DESIRED = f"{ROOT}/desired.json"
DB      = f"{ROOT}/state.db"
LOG     = f"{ROOT}/log/native_host.log"

def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS extension_applied(
        client TEXT NOT NULL, raindrop_id INTEGER NOT NULL,
        bookmark_id TEXT, url TEXT, status TEXT, applied_at TEXT,
        PRIMARY KEY (client, raindrop_id))""")
    con.commit()
    return con

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
    c = (c or "").strip().lower()
    return c if c in ("brave", "chrome", "edge", "vivaldi", "opera") else "unknown"

def op_pending(client):
    if not os.path.exists(DESIRED):
        return {"items": []}
    try:
        desired = json.load(open(DESIRED))
    except Exception as e:
        log(f"desired.json unreadable: {e}")
        return {"items": [], "error": "desired unreadable"}
    con = db()
    done = {r[0] for r in con.execute(
        "SELECT raindrop_id FROM extension_applied "
        "WHERE client=? AND status IN ('created','already_present')", (client,))}
    con.close()
    items = [d for d in desired if d.get("raindrop_id") not in done]
    # A reset file asks the extension to tear down and rebuild a managed folder, e.g. after
    # the folder layout changes. Consumed once, then cleared.
    resets = []
    rp = f"{ROOT}/reset_folders.json"
    if os.path.exists(rp) and items:
        try: resets = json.load(open(rp))
        except Exception as e: log(f"reset_folders.json unreadable: {e}")
    log(f"pending[{client}]: {len(items)} of {len(desired)} staged"
        + (f", reset {resets}" if resets else ""))
    return {"items": items, "reset_folders": resets}

def op_applied(client, results):
    con = db()
    now = datetime.now(timezone.utc).isoformat()
    by_id = {}
    try:
        by_id = {d["raindrop_id"]: d for d in json.load(open(DESIRED))}
    except Exception:
        pass
    n = 0
    for r in results or []:
        rid = r.get("raindrop_id")
        if rid is None:
            continue
        con.execute("INSERT OR REPLACE INTO extension_applied VALUES(?,?,?,?,?,?)",
                    (client, rid, str(r.get("bookmark_id") or ""),
                     (by_id.get(rid) or {}).get("url", ""),
                     r.get("status", "unknown"), now))
        n += 1
        if r.get("status") == "error":
            log(f"[{client}] extension error on {rid}: {r.get('error')}")
        for f in (r.get("created_folders") or []):
            log(f"[{client}] created folder: {f}")
    con.commit(); con.close()
    log(f"applied[{client}]: recorded {n} results")
    return {"ok": True, "recorded": n}

def main():
    log(f"host start (pid {os.getpid()})")
    while True:
        try:
            msg = read_message()
        except Exception as e:
            log(f"read error: {e}"); return 1
        if msg is None:
            log("stream closed"); return 0
        op = msg.get("op")
        try:
            if op == "ping":      send_message({"ok": True, "version": VERSION})
            elif op == "pending": send_message(op_pending(norm_client(msg.get("client"))))
            elif op == "applied": send_message(op_applied(norm_client(msg.get("client")),
                                                          msg.get("results")))
            else:
                log(f"unknown op {op!r}"); send_message({"ok": False, "error": "unknown op"})
        except Exception as e:
            log(f"op {op} failed: {e}")
            try: send_message({"ok": False, "error": str(e)})
            except Exception: return 1

if __name__ == "__main__":
    sys.exit(main())
