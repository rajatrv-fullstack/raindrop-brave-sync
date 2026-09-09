#!/usr/bin/env python3
"""Fuzz the Bookmarks-file side of apply_brave.py.

Contracts:
  1. checksum() accepts any tree whose nodes have string id, name, type and url fields. Titles
     may hold any Unicode including lone surrogates (they hash as UTF-16 code units). Ids and
     URLs are UTF-8 in Chromium, so the generator keeps surrogates out of those two fields.
     The digest is a pure function of the tree.
  2. preflight() on an arbitrary Bookmarks file either returns a document or exits with
     status 2. It must never raise anything else and must never modify the file.
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = tempfile.mkdtemp(prefix="fuzz-profile-")
ROOT = tempfile.mkdtemp(prefix="fuzz-root-")
os.environ["RAINDROP_SYNC_PROFILE"] = PROFILE
os.environ["RAINDROP_SYNC_ROOT"] = ROOT
os.environ["HOME"] = ROOT
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from _fdp import FuzzedDataProvider, run  # noqa: E402
import apply_brave  # noqa: E402

apply_brave.log = lambda *a, **k: None  # the fuzz loop runs millions of inputs; keep stderr for the fuzzer

BOOKMARKS = os.path.join(PROFILE, "Bookmarks")


def node(fdp, depth):
    n = {"id": str(fdp.ConsumeIntInRange(0, 10**6)),
         "name": fdp.ConsumeUnicode(fdp.ConsumeIntInRange(0, 16)),
         "type": "folder" if fdp.ConsumeBool() else "url"}
    if n["type"] == "url":
        n["url"] = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 24))
    else:
        n["children"] = [node(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 3 if depth < 3 else 0))]
    return n


def well_formed_doc(fdp):
    roots = {k: {"id": str(i), "name": k, "type": "folder",
                 "children": [node(fdp, 0) for _ in range(fdp.ConsumeIntInRange(0, 3))]}
             for i, k in enumerate(("bookmark_bar", "other", "synced"), 1)}
    return {"version": 1, "roots": roots, "checksum": ""}


def target(data):
    fdp = FuzzedDataProvider(data)

    # Contract 1: checksum is total over well-formed trees and deterministic.
    doc = well_formed_doc(fdp)
    c1 = apply_brave.checksum(doc)
    assert len(c1) == 32 and all(ch in "0123456789abcdef" for ch in c1), "checksum is not hex md5"
    assert apply_brave.checksum(json.loads(json.dumps(doc))) == c1, "checksum changed across a JSON round trip"

    # Contract 2: preflight never raises anything but SystemExit(2), never touches the file.
    mode = fdp.ConsumeIntInRange(0, 3)
    if mode == 0:
        doc["checksum"] = c1
        payload = json.dumps(doc, ensure_ascii=True).encode()
    elif mode == 1:
        doc["checksum"] = c1
        if fdp.ConsumeBool(): doc["version"] = fdp.ConsumeInt(2)
        if fdp.ConsumeBool(): doc["sync_metadata"] = "x"
        if fdp.ConsumeBool(): doc["roots"].pop(("bookmark_bar", "other", "synced")[fdp.ConsumeIntInRange(0, 2)])
        if fdp.ConsumeBool():
            bag = doc["roots"].get("bookmark_bar")
            if bag: bag["children"] = fdp.ConsumeUnicode(4)  # children that are not a list
        if fdp.ConsumeBool():
            bag = doc["roots"].get("other")
            if bag: bag["children"] = [{"id": "9", "name": "x", "type": "url", "url": "https://x/\udfa5"}]
        payload = json.dumps(doc, ensure_ascii=True).encode()
    elif mode == 2:
        payload = fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 200))
    else:
        payload = json.dumps([fdp.ConsumeInt(4)]).encode()  # valid JSON, wrong shape
    with open(BOOKMARKS, "wb") as f:
        f.write(payload)
    before = hashlib.sha256(payload).hexdigest()

    try:
        result = apply_brave.preflight()
        assert isinstance(result, dict), "preflight returned a non-dict without aborting"
    except SystemExit as e:
        assert e.code == 2, f"preflight exited {e.code}, contract says 2"
    with open(BOOKMARKS, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == before, "preflight modified the Bookmarks file"


if __name__ == "__main__":
    run(target, sys.argv)
