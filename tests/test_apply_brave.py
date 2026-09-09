"""
Invariant tests for src/apply_brave.py, the Brave Bookmarks-file writer.

Each test is named for the invariant it proves. The invariants come from RESEARCH.md sections
1 (checksum encoding), 4 (file format, GUID identity) and 6 (preflight and postflight guards).
Every test runs inside the `sandbox` fixture from conftest.py: a throwaway profile and sync
root under tmp_path, with the module's path constants and is_brave_running() patched. Nothing
here touches a real profile.

Exit codes are checked through a subprocess (`sandbox.run_apply()`); everything else calls the
module's functions directly.
"""
import copy
import hashlib
import json
import os
import re
import time
import uuid

import pytest

import apply_brave

# Microseconds between 1601-01-01 and 1970-01-01; Chromium dates count from 1601.
WEBKIT_EPOCH_OFFSET = 11_644_473_600_000_000
MARKER = {"raindrop_sync": "v1"}
HEX32 = re.compile(r"^[0-9a-f]{32}$")
# RFC 4122 layout with the version nibble fixed at 5 and a lowercase alphabet.
UUID5_LOWER = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


# --- helpers (explicit, local to this file) ---------------------------------------------------

def item(raindrop_id, name, url, folder_path):
    """One staged promotion, in the shape the classifier appends to desired.json."""
    return {"raindrop_id": raindrop_id, "name": name, "url": url, "folder_path": folder_path}


def all_nodes(doc):
    """Every node under the three permanent roots as (node, slash-path), in document order."""
    out = []
    for key in ("bookmark_bar", "other", "synced"):
        apply_brave.walk(doc["roots"][key], lambda n, p: out.append((n, p)))
    return out


def nodes_named(doc, name, type_=None):
    return [n for n, _ in all_nodes(doc) if n["name"] == name and (type_ is None or n["type"] == type_)]


def nodes_with_url(doc, url):
    return [n for n, _ in all_nodes(doc) if n.get("url") == url]


def strip_marked(node):
    """A deep copy of a subtree with every marked node removed. What is left is exactly the
    material the writer is never allowed to touch."""
    out = {k: v for k, v in node.items() if k != "children"}
    if "children" in node:
        out["children"] = [strip_marked(c) for c in node["children"] if not apply_brave.marked(c)]
    return out


def naive_utf8_checksum(doc):
    """The same pre-order walk as the module, but hashing titles as UTF-8 like everything else.
    This is the digest every first implementation produces (RESEARCH.md section 1)."""
    m = hashlib.md5(usedforsecurity=False)

    def node(n):
        m.update(n["id"].encode("utf-8"))
        m.update(n.get("name", "").encode("utf-8"))
        m.update(n["type"].encode("utf-8"))
        if n["type"] == "url":
            m.update(n.get("url", "").encode("utf-8"))
        else:
            for c in n.get("children", []):
                node(c)

    for key in ("bookmark_bar", "other", "synced"):
        node(doc["roots"][key])
    return m.hexdigest()


# --- 1. checksum: titles hash as UTF-16LE ---------------------------------------------------

def test_checksum_hashes_titles_as_utf16le(bm):
    plain = bm.doc(bar=[bm.url("Cafe", "https://example.com/cafe")], with_checksum=False)
    accented = copy.deepcopy(plain)
    accented["roots"]["bookmark_bar"]["children"][0]["name"] = "Café"

    plain_sum = apply_brave.checksum(plain)
    accented_sum = apply_brave.checksum(accented)
    assert HEX32.match(plain_sum), f"checksum is not a 32-char hex digest: {plain_sum!r}"
    assert plain_sum != accented_sum, "a non-ASCII character in a title must change the checksum"
    assert naive_utf8_checksum(accented) != accented_sum, (
        "hashing the accented title as UTF-8 must not reproduce the module's digest; "
        "titles are hashed as UTF-16LE")

    # Independent reference: the byte stream spelled out by hand for a tiny tree, with the
    # UTF-16LE code units of the title written as explicit bytes (no codec involved).
    tiny = {"roots": {
        "bookmark_bar": {"id": "1", "name": "B", "type": "folder", "children": [
            {"id": "10", "name": "Café", "type": "url", "url": "https://x/"}]},
        "other": {"id": "2", "name": "O", "type": "folder", "children": []},
        "synced": {"id": "3", "name": "S", "type": "folder", "children": []},
    }}
    stream = (b"1" + b"B\x00" + b"folder"
              + b"10" + b"C\x00a\x00f\x00\xe9\x00" + b"url" + b"https://x/"
              + b"2" + b"O\x00" + b"folder"
              + b"3" + b"S\x00" + b"folder")
    expected = hashlib.md5(stream, usedforsecurity=False).hexdigest()
    assert apply_brave.checksum(tiny) == expected, (
        "module checksum does not match the hand-built pre-order byte stream "
        "(id utf-8, title utf-16le, type literal, url utf-8, children in order)")


# --- 2. checksum: a lone surrogate does not raise ----------------------------------------------

def test_checksum_lone_surrogate_does_not_raise(bm):
    # Why surrogatepass is needed: the default codec refuses an unpaired surrogate outright.
    with pytest.raises(UnicodeEncodeError):
        "\ud800".encode("utf-16-le")

    doc = bm.doc(bar=[bm.url("lone \ud800 surrogate", "https://example.com/s")], with_checksum=False)
    digest = apply_brave.checksum(doc)          # must not raise
    assert HEX32.match(digest), f"unexpected digest for a surrogate title: {digest!r}"

    # The surrogate is hashed as a real code unit, not dropped: a different lone surrogate
    # yields a different digest.
    other = copy.deepcopy(doc)
    other["roots"]["bookmark_bar"]["children"][0]["name"] = "lone \udfff surrogate"
    assert apply_brave.checksum(other) != digest, "the surrogate code unit must contribute to the digest"

    # The way such a title actually arrives is as a \ud800 escape in the JSON text. Round-trip
    # through json to prove the parsed form still hashes to the same value.
    roundtrip = json.loads(json.dumps(doc, ensure_ascii=True))
    assert "\\ud800" in json.dumps(doc, ensure_ascii=True), "fixture did not produce the escape form"
    assert apply_brave.checksum(roundtrip) == digest, "the JSON-escaped form must hash identically"


# --- 3. checksum: field order and the type literal matter --------------------------------------

def test_checksum_depends_on_sibling_order_and_type_literal(bm):
    a = bm.url("A", "https://a.example/")
    b = bm.url("B", "https://b.example/")
    ordered = bm.doc(bar=[a, b], with_checksum=False)
    swapped = copy.deepcopy(ordered)
    swapped["roots"]["bookmark_bar"]["children"].reverse()
    assert apply_brave.checksum(ordered) != apply_brave.checksum(swapped), (
        "swapping two sibling nodes must change the checksum: the digest is over document order")

    # Type literal in isolation: a url node with an EMPTY url hashes id, title, "url", "" and a
    # folder with no children hashes id, title, "folder". The only difference is the literal.
    as_url = bm.doc(bar=[bm.url("T", "")], with_checksum=False)
    as_folder = copy.deepcopy(as_url)
    as_folder["roots"]["bookmark_bar"]["children"][0]["type"] = "folder"
    assert apply_brave.checksum(as_url) != apply_brave.checksum(as_folder), (
        "changing only the type literal must change the checksum: the literal is hashed")

    # And the realistic case: renaming a real url node to a folder type.
    real_url = bm.doc(bar=[bm.url("Real", "https://real.example/")], with_checksum=False)
    real_folder = copy.deepcopy(real_url)
    real_folder["roots"]["bookmark_bar"]["children"][0]["type"] = "folder"
    assert apply_brave.checksum(real_url) != apply_brave.checksum(real_folder)


# --- 4. node shape ---------------------------------------------------------------------------

def test_emitted_nodes_are_chromium_shaped_with_stable_guids(sandbox, bm):
    doc = bm.doc(bar=[bm.curated_folder()])
    original = sandbox.write_bookmarks(doc)
    sandbox.write_desired([item(4242, "Raindrop Item", "https://example.com/4242", "Raindrop/Security")])

    t0 = int(time.time() * 1_000_000)
    assert apply_brave.main() == 0, "first apply must succeed"
    t1 = int(time.time() * 1_000_000)
    after = sandbox.read_bookmarks()

    for node, path in all_nodes(after):
        assert isinstance(node["id"], str) and node["id"].isdigit(), (
            f"id must be a decimal JSON string at {path}: {node['id']!r}")
        for key in ("date_added", "date_modified", "date_last_used"):
            if key in node:
                assert isinstance(node[key], str) and node[key].isdigit(), (
                    f"{key} must be a decimal JSON string at {path}: {node[key]!r}")
        assert int(node["date_added"]) > WEBKIT_EPOCH_OFFSET, (
            f"date_added must be microseconds since 1601 at {path}: {node['date_added']}")

    created = nodes_with_url(after, "https://example.com/4242")
    assert len(created) == 1, f"expected exactly one emitted url node, got {len(created)}"
    node = created[0]
    assert set(node) == {"date_added", "date_last_used", "guid", "id", "meta_info", "name", "type", "url"}, (
        f"unexpected url node shape: {sorted(node)}")
    assert node["meta_info"] == MARKER, f"url node must carry the raindrop_sync marker: {node.get('meta_info')!r}"
    assert node["date_last_used"] == "0"
    added = int(node["date_added"]) - WEBKIT_EPOCH_OFFSET
    assert t0 <= added <= t1, f"date_added {node['date_added']} is not now in microseconds since 1601"

    expected_guid = str(uuid.uuid5(apply_brave.NS, "url:4242"))
    assert node["guid"] == expected_guid, f"guid must be uuid5(NS, 'url:<raindrop_id>'): {node['guid']}"
    assert UUID5_LOWER.match(node["guid"]), f"guid must be a lowercase version-5 uuid: {node['guid']}"

    folders = nodes_named(after, "Security", "folder")
    assert len(folders) == 1, "the created leaf folder must exist exactly once"
    folder = folders[0]
    assert set(folder) == {"children", "date_added", "date_modified", "guid", "id", "meta_info", "name", "type"}, (
        f"unexpected folder node shape: {sorted(folder)}")
    assert folder["meta_info"] == MARKER, "created folders must carry the marker"
    assert folder["guid"] == str(uuid.uuid5(apply_brave.NS, "folder:Raindrop/Security"))
    assert int(folder["date_modified"]) > WEBKIT_EPOCH_OFFSET

    # Stability: a second run from the identical starting state emits the identical identity.
    sandbox.bookmarks.write_bytes(original)
    os.remove(sandbox.applied)
    assert apply_brave.main() == 0, "second apply from the original state must succeed"
    again = sandbox.read_bookmarks()
    node_again = nodes_with_url(again, "https://example.com/4242")[0]
    folder_again = nodes_named(again, "Security", "folder")[0]
    assert node_again["guid"] == node["guid"], "url guid must be identical across runs"
    assert node_again["id"] == node["id"], "id allocation must be deterministic across runs"
    assert folder_again["guid"] == folder["guid"], "folder guid must be identical across runs"


# --- 5. placement ----------------------------------------------------------------------------

def test_folder_path_is_relative_to_bar_and_never_nests_a_bookmarks_bar_duplicate(sandbox, bm, capsys):
    # Direct: both spellings resolve to the same existing, unmarked folder object.
    curated = bm.curated_folder()
    doc = bm.doc(bar=[curated])
    bar = doc["roots"]["bookmark_bar"]
    x = curated["children"][2]
    assert x["name"] == "X", "fixture: the curated sub-folder must be named X"

    node_a, created_a = apply_brave.find_folder(bar, "Bookmarks Bar/AI Assurance/X".split("/"))
    node_b, created_b = apply_brave.find_folder(bar, "AI Assurance/X".split("/"))
    assert node_a is x, "'Bookmarks Bar/AI Assurance/X' must resolve to the existing X folder"
    assert node_b is x, "'AI Assurance/X' must resolve to the existing X folder"
    assert created_a == [] and created_b == [], f"nothing should be created: {created_a} {created_b}"
    assert [c["name"] for c in bar["children"]] == ["AI Assurance"], (
        "a leading 'Bookmarks Bar' must be stripped, never created as a nested child of the bar")
    assert not apply_brave.marked(node_a), "an existing curated folder must stay unmarked"

    # Direct: a path to a folder that does not exist creates it, marked, under its parent.
    node_c, created_c = apply_brave.find_folder(bar, ["AI Assurance", "New Sub"])
    assert created_c == ["AI Assurance/New Sub"], f"unexpected created list: {created_c}"
    assert apply_brave.marked(node_c), "a created folder must carry the marker"
    assert node_c in curated["children"], "the created folder must hang under its existing parent"
    assert node_c["guid"] == str(uuid.uuid5(apply_brave.NS, "folder:AI Assurance/New Sub"))
    assert node_c["id"] is None, "find_folder leaves id allocation to main()"

    # End to end through main(): the two spellings land in the same folder, a brand new
    # nested path is created and marked, and the bar gains no 'Bookmarks Bar' child.
    doc2 = bm.doc(bar=[bm.curated_folder()])
    sandbox.write_bookmarks(doc2)
    sandbox.write_desired([
        item(1, "A", "https://example.com/a", "Bookmarks Bar/AI Assurance/X"),
        item(2, "B", "https://example.com/b", "AI Assurance/X"),
        item(3, "C", "https://example.com/c", "Bookmarks Bar/Brand New/Deep"),
    ])
    assert apply_brave.main() == 0
    out = capsys.readouterr().out
    after = sandbox.read_bookmarks()
    bar_after = after["roots"]["bookmark_bar"]

    assert [c["name"] for c in bar_after["children"]] == ["AI Assurance", "Brand New"], (
        f"unexpected bar children: {[c['name'] for c in bar_after['children']]}")
    root_guid = after["roots"]["bookmark_bar"]["guid"]
    nested_bars = [n for n in nodes_named(after, "Bookmarks Bar") if n["guid"] != root_guid]
    assert nested_bars == [], "no descendant of the bar may be named 'Bookmarks Bar' (the root itself is)"
    assert len(nodes_named(after, "AI Assurance", "folder")) == 1, "the curated folder must not be duplicated"

    x_folders = nodes_named(after, "X", "folder")
    assert len(x_folders) == 1, "the X folder must exist exactly once"
    x_urls = [c["url"] for c in x_folders[0]["children"] if c["type"] == "url"]
    assert "https://example.com/a" in x_urls and "https://example.com/b" in x_urls, (
        f"both spellings must land in the same X folder: {x_urls}")

    brand_new = nodes_named(after, "Brand New", "folder")
    deep = nodes_named(after, "Deep", "folder")
    assert len(brand_new) == 1 and len(deep) == 1, "the new nested path must be created once"
    assert apply_brave.marked(brand_new[0]) and apply_brave.marked(deep[0]), "created folders must be marked"
    assert deep[0] in brand_new[0]["children"], "Deep must hang under Brand New"
    assert [c["url"] for c in deep[0]["children"]] == ["https://example.com/c"]
    assert "created folder: Brand New" in out and "created folder: Brand New/Deep" in out, out


# --- 6. idempotency --------------------------------------------------------------------------

def test_second_run_is_a_noop_and_byte_identical(sandbox, bm, capsys):
    sandbox.write_bookmarks(bm.doc(bar=[bm.curated_folder()]))
    sandbox.write_desired([
        item(1, "One", "https://example.com/1", "Raindrop/Sec"),
        item(2, "Two", "https://example.com/2", "AI Assurance"),
    ])
    assert apply_brave.main() == 0, "first run must write"
    first_bytes = sandbox.bookmarks_bytes()
    first_backups = sandbox.backup_listing()
    assert len(first_backups) == 1, f"first run must make exactly one backup: {first_backups}"
    capsys.readouterr()

    assert apply_brave.main() == 0, "second run must succeed"
    out = capsys.readouterr().out
    assert "no write needed" in out, f"second run must report nothing to do:\n{out}"
    assert sandbox.bookmarks_bytes() == first_bytes, "second run must leave the file byte-identical"
    assert sandbox.backup_listing() == first_backups, "a no-op run must not create a backup"


# --- 7. coexistence with the extension -------------------------------------------------------

def test_url_already_present_without_marker_is_not_duplicated(sandbox, bm, capsys):
    # As chrome.bookmarks.create() would leave it: right url, random guid, no meta_info.
    ext_url = "https://example.com/from-extension"
    ext_node = bm.url("Created by the extension", ext_url)
    assert "meta_info" not in ext_node
    doc = bm.doc(bar=[bm.folder("Raindrop", [ext_node])])
    original = sandbox.write_bookmarks(doc)

    sandbox.write_desired([item(7, "Extension item", ext_url, "Raindrop")])
    assert apply_brave.main() == 0
    out = capsys.readouterr().out
    assert "no write needed" in out, f"an unmarked node with the same url counts as present:\n{out}"
    assert sandbox.bookmarks_bytes() == original, "nothing to add means the file is untouched"

    # Mixed: one present-unmarked plus one genuinely new. Only the new one is added.
    sandbox.write_desired([
        item(7, "Extension item", ext_url, "Raindrop"),
        item(8, "New item", "https://example.com/new", "Raindrop"),
    ])
    assert apply_brave.main() == 0
    out = capsys.readouterr().out
    assert "1 to apply (1 already present)" in out, out
    after = sandbox.read_bookmarks()
    assert len(nodes_with_url(after, ext_url)) == 1, "the extension's bookmark must not be duplicated"
    assert len(nodes_with_url(after, "https://example.com/new")) == 1, "the new item must be added once"
    kept = nodes_with_url(after, ext_url)[0]
    assert (kept["id"], kept["guid"], kept["name"]) == (ext_node["id"], ext_node["guid"], ext_node["name"])
    assert "meta_info" not in kept, "the writer must not retro-mark a node it did not create"


# --- 8. curated content is preserved verbatim ------------------------------------------------

def test_curated_nodes_keep_id_guid_name_and_order(sandbox, bm):
    curated = bm.curated_folder()
    doc = bm.doc(
        bar=[curated, bm.url("Loose link", "https://loose.example/")],
        other=[bm.url("Other link", "https://other.example/")],
        synced=[bm.folder("Mobile stuff", [bm.url("Phone", "https://phone.example/")])],
    )
    before_roots = copy.deepcopy(doc["roots"])
    sandbox.write_bookmarks(doc)
    sandbox.write_desired([
        item(1, "Into curated", "https://example.com/1", "AI Assurance"),
        item(2, "Into curated sub", "https://example.com/2", "AI Assurance/X"),
        item(3, "Into new", "https://example.com/3", "Raindrop/New"),
    ])
    assert apply_brave.main() == 0
    after = sandbox.read_bookmarks()

    # Remove everything marked and the tree must be exactly what the user had before.
    assert strip_marked({"children": list(after["roots"].values())})["children"] == list(before_roots.values()), (
        "unmarked material must survive a write verbatim")

    cur_before = before_roots["bookmark_bar"]["children"][0]
    cur_after = after["roots"]["bookmark_bar"]["children"][0]
    assert (cur_after["id"], cur_after["guid"], cur_after["name"]) == (
        cur_before["id"], cur_before["guid"], cur_before["name"]), "the curated folder's identity must not change"
    n = len(cur_before["children"])
    assert [(c["id"], c["guid"], c["name"]) for c in cur_after["children"][:n]] == [
        (c["id"], c["guid"], c["name"]) for c in cur_before["children"]], (
        "the curated children must keep id, guid, name and order")
    appended = cur_after["children"][n:]
    assert len(appended) == 1 and apply_brave.marked(appended[0]), "only marked nodes may be appended"
    assert not any(apply_brave.marked(c) for c in cur_after["children"][:n]), "curated nodes must stay unmarked"


# --- 9. preflight aborts ---------------------------------------------------------------------

def _version_not_1(doc, sandbox):
    doc["version"] = 2
    sandbox.write_bookmarks(doc)


def _missing_permanent_root(doc, sandbox):
    del doc["roots"]["synced"]
    sandbox.write_bookmarks(doc)


def _sync_metadata_present(doc, sandbox):
    doc["sync_metadata"] = "Q0hST01FX1NZTkM="
    sandbox.write_bookmarks(doc)


def _checksum_mismatch(doc, sandbox):
    doc["checksum"] = "0" * 32
    sandbox.write_bookmarks(doc)


def _bookmarks_missing(doc, sandbox):
    pass    # never written: the profile directory exists but holds no Bookmarks file


def _account_storage_present(doc, sandbox):
    sandbox.write_bookmarks(doc)
    (sandbox.profile / "AccountBookmarks").write_text("{}", encoding="utf-8")


PREFLIGHT_CASES = [
    pytest.param(_version_not_1, "unexpected version 2", id="version_not_1"),
    pytest.param(_missing_permanent_root, "missing root synced", id="missing_permanent_root"),
    pytest.param(_sync_metadata_present, "Brave Sync is enabled", id="sync_metadata_present"),
    pytest.param(_checksum_mismatch, "stored checksum does not match", id="checksum_mismatch"),
    pytest.param(_bookmarks_missing, "Refusing to create one", id="bookmarks_missing"),
    pytest.param(_account_storage_present, "AccountBookmarks present", id="account_storage_present"),
]


@pytest.mark.parametrize("arrange, expected_message", PREFLIGHT_CASES)
def test_preflight_aborts_with_exit_2_and_leaves_the_file_untouched(sandbox, bm, arrange, expected_message):
    doc = bm.doc(bar=[bm.curated_folder()])
    arrange(doc, sandbox)
    sandbox.write_desired([item(1, "One", "https://example.com/1", "Raindrop")])
    before_bytes = sandbox.bookmarks_bytes() if sandbox.bookmarks.exists() else None
    before_listing = sandbox.profile_listing()

    proc = sandbox.run_apply()

    assert proc.returncode == 2, f"expected preflight exit 2, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "ABORT" in proc.stdout and expected_message in proc.stdout, proc.stdout
    if before_bytes is None:
        assert not sandbox.bookmarks.exists(), "a missing Bookmarks file must never be created"
    else:
        assert sandbox.bookmarks_bytes() == before_bytes, "an aborted run must leave the file byte-identical"
    assert sandbox.profile_listing() == before_listing, "an aborted run must leave no new files in the profile"
    assert not sandbox.backups.exists(), "preflight runs before backup; no backup directory may appear"
    assert not sandbox.applied.exists(), "an aborted run must not record anything as applied"


# --- 10. backup ------------------------------------------------------------------------------

def test_backup_is_timestamped_parses_and_prunes_per_prefix(sandbox, bm):
    keep = apply_brave.KEEP
    doc = bm.doc(bar=[bm.curated_folder()])
    original = sandbox.write_bookmarks(doc)
    # A distinguishable Bookmarks.bak, so the .bak copy can be told apart from the primary.
    bak_bytes = json.dumps(bm.doc(bar=[bm.url("Older state", "https://older.example/")]), indent=3).encode("utf-8")
    sandbox.bak.write_bytes(bak_bytes)

    # 25 stale copies of each prefix, stamped in the year 2000 so they sort before today's.
    sandbox.backups.mkdir(parents=True)
    fake_primary = [f"Bookmarks.20000101T{i:02d}0000Z" for i in range(25)]
    fake_bak = [f"Bookmarks.bak.20000101T{i:02d}0000Z" for i in range(25)]
    for name in fake_primary + fake_bak:
        (sandbox.backups / name).write_text("{}", encoding="utf-8")

    sandbox.write_desired([item(1, "One", "https://example.com/1", "Raindrop")])
    assert apply_brave.main() == 0

    names = sandbox.backup_listing()
    primaries = [n for n in names if n.startswith("Bookmarks.") and not n.startswith("Bookmarks.bak.")]
    baks = [n for n in names if n.startswith("Bookmarks.bak.")]
    assert set(names) == set(primaries) | set(baks), f"unexpected files in backups: {names}"

    new_primary = [n for n in primaries if n not in fake_primary]
    new_bak = [n for n in baks if n not in fake_bak]
    assert len(new_primary) == 1, f"expected one fresh primary backup: {new_primary}"
    assert len(new_bak) == 1, f"expected one fresh .bak backup: {new_bak}"
    assert re.fullmatch(r"Bookmarks\.\d{8}T\d{6}Z", new_primary[0]), new_primary[0]
    assert re.fullmatch(r"Bookmarks\.bak\.\d{8}T\d{6}Z", new_bak[0]), new_bak[0]
    assert (sandbox.backups / new_primary[0]).read_bytes() == original, "primary backup must be a copy of Bookmarks"
    assert (sandbox.backups / new_bak[0]).read_bytes() == bak_bytes, ".bak backup must be a copy of Bookmarks.bak"
    json.loads((sandbox.backups / new_primary[0]).read_text(encoding="utf-8"))   # parses
    json.loads((sandbox.backups / new_bak[0]).read_text(encoding="utf-8"))       # parses

    assert len(primaries) == keep, f"expected {keep} primary backups after pruning, got {len(primaries)}"
    assert len(baks) == keep, f"expected {keep} .bak backups after pruning, got {len(baks)}"
    assert primaries == sorted(fake_primary)[-(keep - 1):] + new_primary, (
        "pruning must drop the OLDEST primaries and keep the fresh one; it must not discard "
        "primaries ahead of .bak copies")
    assert baks == sorted(fake_bak)[-(keep - 1):] + new_bak, "pruning must drop the oldest .bak copies"


# --- 11. verification ------------------------------------------------------------------------

def test_failed_post_write_verification_restores_the_original_file(sandbox, bm, monkeypatch, capsys):
    original = sandbox.write_bookmarks(bm.doc(bar=[bm.curated_folder()]))
    sandbox.write_desired([item(1, "One", "https://example.com/1", "Raindrop")])

    # Variant A: the re-read from disk fails. Reads of BOOKMARKS in main(): preflight, then
    # the verification pass. Make the second one raise.
    real_read = apply_brave.read_json
    bookmark_reads = []

    def flaky_read(path):
        if path == apply_brave.BOOKMARKS:
            bookmark_reads.append(path)
            if len(bookmark_reads) == 2:
                raise RuntimeError("simulated unreadable file on re-read")
        return real_read(path)

    monkeypatch.setattr(apply_brave, "read_json", flaky_read)
    rc = apply_brave.main()
    out = capsys.readouterr().out
    assert rc == 1, f"a failed verification must return 1, got {rc}\n{out}"
    assert "VERIFY FAILED" in out and "simulated unreadable" in out, out
    assert len(bookmark_reads) == 2, "the verification pass must re-read the file from disk"
    assert sandbox.bookmarks_bytes() == original, "the original file content must be restored"
    assert not sandbox.applied.exists(), "nothing may be recorded as applied after a failed verification"
    assert not any(n.startswith(".bm-") for n in sandbox.profile_listing()), sandbox.profile_listing()
    monkeypatch.setattr(apply_brave, "read_json", real_read)

    # Variant B: the file re-reads fine but its checksum does not recompute. Calls to
    # checksum() in main(): preflight, the write, the verification pass. Corrupt the third.
    real_checksum = apply_brave.checksum
    checksum_calls = []

    def corrupt_on_verify(doc):
        checksum_calls.append(1)
        digest = real_checksum(doc)
        return digest if len(checksum_calls) < 3 else "0" * 32

    monkeypatch.setattr(apply_brave, "checksum", corrupt_on_verify)
    rc = apply_brave.main()
    out = capsys.readouterr().out
    assert rc == 1, f"a checksum mismatch after write must return 1, got {rc}\n{out}"
    assert "checksum mismatch after write" in out, out
    assert sandbox.bookmarks_bytes() == original, "the original file content must be restored"
    assert not sandbox.applied.exists()


# --- 12. atomicity ---------------------------------------------------------------------------

def test_no_temp_file_is_left_after_success_or_failure(sandbox, bm, monkeypatch, capsys):
    sandbox.write_bookmarks(bm.doc(bar=[bm.curated_folder()]))
    sandbox.write_desired([item(1, "One", "https://example.com/1", "Raindrop")])
    assert apply_brave.main() == 0
    assert sandbox.profile_listing() == ["Bookmarks"], (
        f"after a successful write only Bookmarks may remain: {sandbox.profile_listing()}")
    after_success = sandbox.bookmarks_bytes()

    # Forced failure at the rename step, seen only through the module's own `os` name so the
    # real os module (and pytest) are untouched.
    class RenameFails:
        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, src, dst):
            raise OSError("simulated rename failure")

    monkeypatch.setattr(apply_brave, "os", RenameFails())
    sandbox.write_desired([item(2, "Two", "https://example.com/2", "Raindrop")])
    rc = apply_brave.main()
    out = capsys.readouterr().out
    assert rc == 1, f"a failed write must return 1, got {rc}\n{out}"
    assert "WRITE FAILED" in out and "simulated rename failure" in out, out
    assert sandbox.profile_listing() == ["Bookmarks"], (
        f"a failed write must unlink its temp file: {sandbox.profile_listing()}")
    assert sandbox.bookmarks_bytes() == after_success, "a failed write must leave the previous content in place"


# --- extra: a bug found while writing test 2 ------------------------------------------------

def test_write_survives_a_lone_surrogate_in_an_existing_title(sandbox, bm, capsys):
    doc = bm.doc(bar=[bm.url("lone \ud800 surrogate", "https://example.com/s")])
    # On disk such a title is a \ud800 escape inside otherwise valid UTF-8.
    text = json.dumps(doc, ensure_ascii=True, indent=3, sort_keys=True)
    assert "\\ud800" in text
    sandbox.bookmarks.write_text(text, encoding="utf-8")
    sandbox.write_desired([item(1, "One", "https://example.com/1", "Raindrop")])

    rc = apply_brave.main()
    out = capsys.readouterr().out
    assert rc == 0, f"a lone surrogate in an existing title must not block every write:\n{out}"
    after = sandbox.read_bookmarks()
    assert apply_brave.checksum(after) == after["checksum"]
    assert len(nodes_with_url(after, "https://example.com/s")) == 1, "the surrogate title must survive"
    assert len(nodes_with_url(after, "https://example.com/1")) == 1, "the promotion must land"


# --- 13. rules shared with the host and the extension ----------------------------------------

def test_canon_and_folder_rule_agree_with_the_host_copy_and_the_shared_table(folder_path_cases):
    """canon() and norm_folder_path() are duplicated in native_host.py because the two files
    are installed separately. This is the check that keeps the copies identical, and holds
    both to the table the extension's harness reads (tests/js/folder_paths.json)."""
    import importlib.util
    from pathlib import Path
    src = Path(apply_brave.__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("nh", src / "native_host.py")
    nh = importlib.util.module_from_spec(spec); spec.loader.exec_module(nh)
    import inspect
    for name in ("canon", "norm_folder_path", "_split_netloc"):
        assert inspect.getsource(getattr(apply_brave, name)) == inspect.getsource(getattr(nh, name)), (
            f"{name}() differs between apply_brave.py and native_host.py")
    for c in folder_path_cases:
        assert apply_brave.norm_folder_path(c["path"]) == c["parts"], c
    for raw, want in (("https://WWW.Example.org", "https://www.example.org/"),
                      ("HTTPS://example.org/A?b=C#D", "https://example.org/A?b=C#D"),
                      (" https://example.org/x \n", "https://example.org/x"),
                      ("https://user:pw@Example.org:8443", "https://user:pw@example.org:8443/"),
                      ("file:///Users/x/doc.pdf", "file:///Users/x/doc.pdf"),
                      ("not a url", None), ("https://", None), (None, None), (42, None)):
        assert apply_brave.canon(raw) == want, (raw, apply_brave.canon(raw))


def test_non_canonical_staged_url_matches_the_browser_form_and_is_not_duplicated(sandbox, bm, capsys):
    """Brave stores https://www.example.org/ ; Raindrop may hand back https://WWW.example.org.
    A strict compare created a second copy from each applier (audit EXT-02)."""
    doc = bm.doc(bar=[bm.folder("Raindrop", [bm.url("Existing", "https://www.example.org/")])])
    sandbox.write_bookmarks(doc)
    sandbox.write_desired([item(1, "Same site", "https://WWW.example.org", "Raindrop/Sites"),
                           item(2, "Other", "https://other.example/", "Raindrop/Sites")])
    assert apply_brave.main() == 0
    out = capsys.readouterr().out
    assert "1 to apply (1 already present)" in out, out
    after = sandbox.read_bookmarks()
    assert len(nodes_with_url(after, "https://www.example.org/")) == 1, "canonical duplicate created"
    assert "https://WWW.example.org" not in json.dumps(after), "the non-canonical form must not be written"


def test_invalid_url_is_skipped_with_one_line_and_the_rest_still_lands(sandbox, bm, capsys):
    sandbox.write_bookmarks(bm.doc())
    sandbox.write_desired([item(1, "Broken", "not a url", "Raindrop/X"),
                           item(2, "Bar only", "https://ok.example/", "Bookmarks Bar"),
                           item(3, "Fine", "https://fine.example/", "Raindrop/X")])
    assert apply_brave.main() == 0
    out = capsys.readouterr().out
    assert "skipping 1: invalid url 'not a url'" in out, out
    assert "skipping 2: empty folder_path" in out, out
    after = sandbox.read_bookmarks()
    assert len(nodes_with_url(after, "https://fine.example/")) == 1
    assert nodes_with_url(after, "not a url") == [] and nodes_with_url(after, "https://ok.example/") == []


def test_profile_comes_from_env_then_config_json_then_the_stock_default(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    cfg = tmp_path / "from-config"
    (root / "config.json").write_text(json.dumps({"profile": str(cfg)}), encoding="utf-8")
    assert apply_brave.resolve_profile(str(root), {"RAINDROP_SYNC_PROFILE": "/from/env"}) == "/from/env"
    assert apply_brave.resolve_profile(str(root), {}) == str(cfg)
    (root / "config.json").write_text(json.dumps({"profile": None}), encoding="utf-8")
    assert apply_brave.resolve_profile(str(root), {}).endswith("BraveSoftware/Brave-Browser/Default")
    (root / "config.json").write_text("{not json", encoding="utf-8")
    assert apply_brave.resolve_profile(str(root), {}).endswith("BraveSoftware/Brave-Browser/Default")


def test_launchd_logs_rotate_at_one_megabyte(sandbox, capsys):
    logdir = sandbox.root / "log"; logdir.mkdir()
    big = logdir / "apply.out.log"; big.write_bytes(b"x" * (apply_brave.MAX_LOG + 1))
    small = logdir / "apply.err.log"; small.write_bytes(b"y" * 10)
    apply_brave.rotate_logs()
    assert (logdir / "apply.out.log.1").exists() and not big.exists(), "the oversized log must move to .1"
    assert small.exists() and not (logdir / "apply.err.log.1").exists(), "a small log is left alone"
    assert "rotated apply.out.log" in capsys.readouterr().out
