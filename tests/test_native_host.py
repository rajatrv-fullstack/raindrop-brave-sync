"""
Subprocess tests for src/native_host.py, the native-messaging host.

The host is driven exactly the way Brave drives it: bytes in on stdin, bytes out on
stdout, each message a 4-byte native-order length prefix followed by UTF-8 JSON.
Every test points RAINDROP_SYNC_ROOT at a directory under tmp_path, so the host's
desired.json, state.db, reset_folders.json and log all live there. HOME is pointed at
the same directory as a second fence: the host never consults HOME once ROOT is set,
and if that ever regresses the damage lands in tmp_path, not in the real home.

Nothing here imports the module. Each test spawns the host once per exchange, which is
how the extension uses it (one spawn per alarm tick), so ledger state that must survive
between ticks is proven to survive a process restart.
"""
import json
import os
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parent.parent / "src" / "native_host.py"

# A small staged set, as Phase A would write it: {raindrop_id, name, url, folder_path}.
# One non-ASCII title so the framing is proven on more than plain ASCII.
ITEMS = [
    {"raindrop_id": 101, "name": "Anthropic", "url": "https://www.anthropic.com/",
     "folder_path": "Raindrop/AI"},
    {"raindrop_id": 102, "name": "Python docs", "url": "https://docs.python.org/3/",
     "folder_path": "Raindrop/Programming"},
    {"raindrop_id": 103, "name": "Café guide", "url": "https://example.org/cafe",
     "folder_path": "Raindrop/Food"},
]
KNOWN_CLIENTS = ("brave", "chrome", "edge", "vivaldi", "opera")


# ----------------------------------------------------------------------------- framing


def frame(obj):
    """Encode one message the way the browser does: native-order u32 length + JSON."""
    body = json.dumps(obj).encode("utf-8")
    return struct.pack("@I", len(body)) + body


def unframe_all(stream):
    """Decode an entire stdout stream into a list of replies.

    Every byte must belong to a complete frame. A stray print before, between or after
    frames, or a truncated reply, shows up here as an AssertionError rather than being
    silently skipped over.
    """
    replies = []
    pos = 0
    while pos < len(stream):
        header = stream[pos:pos + 4]
        assert len(header) == 4, (
            f"{len(header)} trailing byte(s) at offset {pos} are not a frame header: {header!r}")
        (length,) = struct.unpack("@I", header)
        body = stream[pos + 4:pos + 4 + length]
        assert len(body) == length, (
            f"frame at offset {pos} declares {length} bytes but only {len(body)} follow; "
            f"stream is corrupt or truncated: {stream[pos:pos + 64]!r}")
        try:
            reply = json.loads(body.decode("utf-8"))
        except ValueError as e:
            raise AssertionError(f"frame body at offset {pos} is not JSON: {body!r} ({e})") from e
        assert isinstance(reply, dict), f"reply at offset {pos} is not a JSON object: {reply!r}"
        replies.append(reply)
        pos += 4 + length
    assert pos == len(stream), f"consumed {pos} of {len(stream)} bytes"
    return replies


# ----------------------------------------------------------------------------- driving


def run_host(root, messages, timeout=60):
    """Spawn the host with ROOT (and HOME) under tmp_path, feed it the framed messages,
    close stdin, and return the CompletedProcess."""
    env = dict(os.environ)
    env["RAINDROP_SYNC_ROOT"] = str(root)
    env["HOME"] = str(root)
    payload = b"".join(frame(m) for m in messages)
    return subprocess.run([sys.executable, str(HOST)], input=payload, capture_output=True,
                          env=env, timeout=timeout)


def talk(root, messages):
    """run_host plus the checks every exchange must pass. Returns the decoded replies,
    one per message, in order."""
    proc = run_host(root, messages)
    assert proc.returncode == 0, (
        f"host exited {proc.returncode}; stderr: {proc.stderr!r}; stdout: {proc.stdout!r}")
    assert proc.stderr == b"", f"host wrote to stderr: {proc.stderr!r}"
    replies = unframe_all(proc.stdout)
    assert len(replies) == len(messages), (
        f"sent {len(messages)} messages, got {len(replies)} replies: {replies!r}")
    return replies


def write_desired(root, items):
    (root / "desired.json").write_text(json.dumps(items), encoding="utf-8")


def ledger_rows(root):
    """Every row of the host's extension_applied table, as (client, raindrop_id, status,
    bookmark_id, url), ordered so comparisons are deterministic."""
    db = root / "state.db"
    assert db.exists(), f"host did not create its ledger at {db}"
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT client, raindrop_id, status, bookmark_id, url FROM extension_applied "
            "ORDER BY client, raindrop_id").fetchall()
    finally:
        con.close()


def host_log(root):
    p = root / "log" / "native_host.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def created_results(items, bookmark_ids_from=500):
    """Results the extension reports after chrome.bookmarks.create() succeeded for each item."""
    return [{"raindrop_id": it["raindrop_id"], "status": "created",
             "bookmark_id": str(bookmark_ids_from + i), "created_folders": []}
            for i, it in enumerate(items)]


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture
def root(tmp_path):
    """An empty sync root under tmp_path. No desired.json, no ledger, no log."""
    r = tmp_path / "sync-root"
    r.mkdir()
    return r


@pytest.fixture
def staged_root(root):
    """A sync root with ITEMS staged in desired.json and nothing applied yet."""
    write_desired(root, ITEMS)
    return root


# ----------------------------------------------------------------------------- tests


def test_ping_replies_and_stdout_carries_only_framed_messages(root):
    # Three pings in one session: the reply stream must decode as exactly three frames
    # with no bytes before, between or after them, and nothing may reach stderr.
    messages = [{"op": "ping"}, {"op": "ping"}, {"op": "ping"}]
    proc = run_host(root, messages)

    assert proc.returncode == 0, f"host exited {proc.returncode}: {proc.stderr!r}"
    assert proc.stderr == b"", f"stderr must be empty, got {proc.stderr!r}"
    assert proc.stdout != b"", "host produced no output at all"

    replies = unframe_all(proc.stdout)
    assert len(replies) == 3, f"expected 3 replies, got {len(replies)}: {replies!r}"
    for i, reply in enumerate(replies):
        assert reply.get("ok") is True, f"ping {i} did not reply ok: {reply!r}"
        version = reply.get("version")
        assert isinstance(version, str) and version, f"ping {i} version is not a string: {reply!r}"
        assert set(reply) == {"ok", "version"}, f"ping {i} carries unexpected keys: {reply!r}"

    # Diagnostics went to the log under ROOT, not to stdout.
    assert "host start" in host_log(root), "host did not write its start line to the log under ROOT"


def test_pending_without_desired_json_returns_empty_items(root):
    assert not (root / "desired.json").exists(), "fixture must start without desired.json"

    (reply,) = talk(root, [{"op": "pending", "client": "brave"}])

    assert reply.get("items") == [], f"expected an empty items list, got {reply!r}"
    assert "error" not in reply, f"a missing desired.json is not an error: {reply!r}"
    assert not (root / "desired.json").exists(), "host must not create desired.json"


def test_pending_ledger_is_kept_per_client(staged_root):
    root = staged_root

    # Tick 1: a client that has applied nothing is told about every staged item, in order.
    (reply,) = talk(root, [{"op": "pending", "client": "brave"}])
    assert reply.get("items") == ITEMS, f"brave should see all staged items, got {reply!r}"

    # Tick 2: brave reports success for all of them.
    (reply,) = talk(root, [{"op": "applied", "client": "brave", "results": created_results(ITEMS)}])
    assert reply.get("ok") is True, f"applied was not acknowledged: {reply!r}"
    assert reply.get("recorded") == len(ITEMS), f"expected {len(ITEMS)} recorded, got {reply!r}"

    # Tick 3, a fresh host process: brave has nothing pending, chrome still has everything.
    brave, chrome = talk(root, [{"op": "pending", "client": "brave"},
                                {"op": "pending", "client": "chrome"}])
    assert brave.get("items") == [], f"brave applied everything but is still offered {brave!r}"
    assert chrome.get("items") == ITEMS, f"chrome applied nothing but is offered {chrome!r}"

    # The ledger holds exactly brave's rows, with the url filled in from desired.json.
    rows = ledger_rows(root)
    expected = [("brave", it["raindrop_id"], "created", str(500 + i), it["url"])
                for i, it in enumerate(ITEMS)]
    assert rows == expected, f"ledger rows differ from what brave reported:\n{rows!r}\n{expected!r}"


def test_applied_error_is_recorded_logged_and_item_stays_pending(staged_root):
    root = staged_root
    results = [
        {"raindrop_id": 101, "status": "error", "error": "Error: bookmark quota exceeded"},
        {"raindrop_id": 102, "status": "created", "bookmark_id": "77", "created_folders": ["Raindrop"]},
    ]

    applied, pending = talk(root, [{"op": "applied", "client": "brave", "results": results},
                                   {"op": "pending", "client": "brave"}])

    assert applied.get("ok") is True, f"applied was not acknowledged: {applied!r}"
    assert applied.get("recorded") == 2, f"both results should be recorded: {applied!r}"

    # 101 errored, 103 was never reported: both stay pending. 102 is done.
    assert pending.get("items") == [ITEMS[0], ITEMS[2]], (
        f"expected 101 (error) and 103 (unreported) to remain pending, got {pending!r}")

    rows = {(c, rid): status for c, rid, status, _bid, _url in ledger_rows(root)}
    assert rows.get(("brave", 101)) == "error", f"error result not recorded as such: {rows!r}"
    assert rows.get(("brave", 102)) == "created", f"created result not recorded: {rows!r}"
    assert ("brave", 103) not in rows, f"unreported item must not appear in the ledger: {rows!r}"

    log = host_log(root)
    assert "[brave] extension error on 101: Error: bookmark quota exceeded" in log, (
        f"error was not logged with client, id and message:\n{log}")
    assert "[brave] created folder: Raindrop" in log, f"created folder was not logged:\n{log}"


def test_reset_folders_returned_only_with_pending_items_and_cleared_after_applied(staged_root):
    root = staged_root
    reset_file = root / "reset_folders.json"

    # brave applies everything before the reset request exists.
    talk(root, [{"op": "applied", "client": "brave", "results": created_results(ITEMS)}])
    reset_file.write_text(json.dumps(["Raindrop"]), encoding="utf-8")

    # brave has nothing pending, so it is not asked to reset. chrome has items, so it is.
    brave, chrome = talk(root, [{"op": "pending", "client": "brave"},
                                {"op": "pending", "client": "chrome"}])
    assert brave.get("items") == [], f"brave should have nothing pending: {brave!r}"
    assert brave.get("reset_folders", []) == [], (
        f"reset must not be handed to a client with no pending items: {brave!r}")
    assert chrome.get("items") == ITEMS, f"chrome should see every item: {chrome!r}"
    assert chrome.get("reset_folders") == ["Raindrop"], (
        f"reset should accompany pending items: {chrome!r}")
    assert reset_file.exists(), "pending must not consume reset_folders.json; only applied does"

    # chrome reports back: the reset has been acted on and the file is consumed.
    (reply,) = talk(root, [{"op": "applied", "client": "chrome", "results": created_results(ITEMS)}])
    assert reply.get("ok") is True, f"applied was not acknowledged: {reply!r}"
    assert not reset_file.exists(), "reset_folders.json must be deleted once applied is reported"
    assert "reset_folders.json cleared" in host_log(root), "clearing the reset was not logged"

    # And a later tick offers no reset to anyone.
    (later,) = talk(root, [{"op": "pending", "client": "edge"}])
    assert later.get("items") == ITEMS, f"edge has applied nothing and should see items: {later!r}"
    assert later.get("reset_folders", []) == [], f"consumed reset reappeared: {later!r}"


def test_unknown_op_returns_ok_false_without_crashing_host(root):
    messages = [{"op": "frobnicate"}, {"client": "brave"}, {"op": "ping"}]

    bad_op, no_op, ping = talk(root, messages)

    assert bad_op.get("ok") is False, f"unknown op must reply ok false: {bad_op!r}"
    assert bad_op.get("error") == "unknown op", f"unknown op error text differs: {bad_op!r}"
    assert no_op.get("ok") is False, f"a message without op must reply ok false: {no_op!r}"
    assert no_op.get("error") == "unknown op", f"missing op error text differs: {no_op!r}"
    assert ping.get("ok") is True, f"host did not survive the unknown op; ping got {ping!r}"
    assert "unknown op 'frobnicate'" in host_log(root), "unknown op was not logged"


def test_client_outside_known_set_is_normalised_to_unknown(staged_root):
    root = staged_root
    stranger = "Firefox Nightly"
    assert stranger.strip().lower() not in KNOWN_CLIENTS

    (reply,) = talk(root, [{"op": "applied", "client": stranger, "results": created_results(ITEMS)}])
    assert reply.get("ok") is True and reply.get("recorded") == len(ITEMS), (
        f"applied from an unknown client was not recorded: {reply!r}")

    # Everything outside the known set shares the one "unknown" ledger, including a missing
    # client field. A known client keeps its own.
    as_unknown, as_safari, missing, as_brave = talk(root, [
        {"op": "pending", "client": "unknown"},
        {"op": "pending", "client": "safari"},
        {"op": "pending"},
        {"op": "pending", "client": "brave"},
    ])
    assert as_unknown.get("items") == [], f"'unknown' should see the stranger's applied set: {as_unknown!r}"
    assert as_safari.get("items") == [], f"'safari' should normalise to unknown too: {as_safari!r}"
    assert missing.get("items") == [], f"a missing client should normalise to unknown: {missing!r}"
    assert as_brave.get("items") == ITEMS, f"brave must not inherit the unknown ledger: {as_brave!r}"

    clients = sorted({row[0] for row in ledger_rows(root)})
    assert clients == ["unknown"], f"ledger should hold only the normalised client, got {clients!r}"

    log = host_log(root)
    assert f"applied[unknown]: recorded {len(ITEMS)} results" in log, f"normalised client not in log:\n{log}"
    assert stranger not in log, f"raw client value leaked into the log:\n{log}"
