#!/usr/bin/env python3
"""Fuzz native_host.handle(): for ANY JSON value it must return a dict and never raise.

Also fuzzes the on-disk inputs the host reads (desired.json and reset_folders.json) with
arbitrary bytes, since those files are written by another process and may be truncated or
corrupt at the moment the host reads them.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = tempfile.mkdtemp(prefix="fuzz-host-")
os.environ["RAINDROP_SYNC_ROOT"] = ROOT
os.environ["HOME"] = ROOT
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from _fdp import FuzzedDataProvider, run  # noqa: E402
import native_host  # noqa: E402

native_host.log = lambda *a, **k: None  # the fuzz loop runs millions of inputs; keep stderr for the fuzzer

OPS = ["ping", "pending", "applied", "reset", "", None, 0, [], {}]
CLIENTS = ["brave", "chrome", "edge", "", "BRAVE ", None, 7, ["brave"]]


def json_value(fdp, depth=0):
    kind = fdp.ConsumeIntInRange(0, 6 if depth < 3 else 4)
    if kind == 0: return None
    if kind == 1: return fdp.ConsumeBool()
    if kind == 2: return fdp.ConsumeInt(8)
    if kind == 3: return fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 24))
    if kind == 4: return fdp.ConsumeIntInRange(-10**12, 10**12)
    if kind == 5: return [json_value(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 4))]
    return {fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 8)): json_value(fdp, depth + 1)
            for _ in range(fdp.ConsumeIntInRange(0, 4))}


def message(fdp):
    if fdp.ConsumeBool():
        return json_value(fdp)
    msg = {"op": OPS[fdp.ConsumeIntInRange(0, len(OPS) - 1)],
           "client": CLIENTS[fdp.ConsumeIntInRange(0, len(CLIENTS) - 1)]}
    if fdp.ConsumeBool():
        msg["results"] = [json_value(fdp) for _ in range(fdp.ConsumeIntInRange(0, 5))]
        for r in msg["results"]:
            if isinstance(r, dict) and fdp.ConsumeBool():
                r["raindrop_id"] = fdp.ConsumeInt(8)
                r["status"] = ["created", "already_present", "error", "?"][fdp.ConsumeIntInRange(0, 3)]
    return msg


def target(data):
    fdp = FuzzedDataProvider(data)
    # Corrupt or replace the files the host reads, sometimes with valid content.
    for name in ("desired.json", "reset_folders.json"):
        path = os.path.join(ROOT, name)
        choice = fdp.ConsumeIntInRange(0, 3)
        if choice == 0 and os.path.exists(path):
            os.remove(path)
        elif choice == 1:
            with open(path, "wb") as f:
                f.write(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64)))
        elif choice == 2:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(json_value(fdp), f, ensure_ascii=True)
    reply = native_host.handle(message(fdp))
    assert isinstance(reply, dict), f"handle() returned {type(reply).__name__}, not a dict"
    json.dumps(reply)  # the reply must be serialisable, or send_message would fail
    if fdp.ConsumeBool():
        assert native_host.handle({"op": "ping"}).get("ok") is True, "host unusable after input"


if __name__ == "__main__":
    run(target, sys.argv)
