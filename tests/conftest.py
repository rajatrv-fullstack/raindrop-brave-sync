"""
Shared fixtures for the raindrop-brave-sync test suite.

Every test runs against a throwaway profile and sync root under pytest's tmp_path. Nothing in
this suite may read or write a real browser profile or anything under $HOME (CONTRIBUTING.md:
"Never test against your live browser profile" - a bug in the writer silently destroys a
bookmark library and Chromium has no restore path, RESEARCH.md finding 6).

Both modules under test resolve their paths from RAINDROP_SYNC_PROFILE and RAINDROP_SYNC_ROOT at
import time. Those variables are pinned to a path that does not exist BEFORE the modules are
imported, so a test that forgets to use the `sandbox` fixture fails loudly on a missing file
instead of touching real data.

Standard library plus pytest only. Runs on Python 3.9+ on Linux and macOS.
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# Pin the module-level path defaults to somewhere that cannot exist. Deliberately a plain
# assignment, not setdefault: a developer shell that exports the real profile path must not
# leak into the test process either.
_SENTINEL = "/nonexistent/raindrop-brave-sync-tests"
os.environ["RAINDROP_SYNC_PROFILE"] = f"{_SENTINEL}/profile"
os.environ["RAINDROP_SYNC_ROOT"] = f"{_SENTINEL}/root"

import apply_brave  # noqa: E402  (must follow the environment pin above)

# One table for the folder_path rule, shared with the extension's own test runner
# (tests/js/run.mjs) so the host, the file writer and the worker are held to the same cases.
# Each case is {"path": <staged string>, "parts": [<segments>] or None when rejected}.
with open(Path(__file__).resolve().parent / "js" / "folder_paths.json", encoding="utf-8") as _f:
    FOLDER_PATH_CASES = json.load(_f)["cases"]


@pytest.fixture
def folder_path_cases():
    """The shared folder_path table, one case per {"path", "parts"}."""
    return FOLDER_PATH_CASES

# Chromium's well-known permanent-node GUIDs (components/bookmarks/browser/bookmark_node.cc,
# kBookmarkBarNodeUuid / kOtherBookmarksNodeUuid / kMobileBookmarksNodeUuid).
ROOT_GUIDS = {
    "bookmark_bar": "00000000-0000-4000-a000-000000000002",
    "other": "00000000-0000-4000-a000-000000000003",
    "synced": "00000000-0000-4000-a000-000000000004",
}
ROOT_NAMES = {"bookmark_bar": "Bookmarks Bar", "other": "Other Bookmarks", "synced": "Mobile Bookmarks"}
ROOT_IDS = {"bookmark_bar": "1", "other": "2", "synced": "3"}   # "0" is the invisible root

# Microseconds between 1601-01-01 and 1970-01-01: Chromium dates are counted from 1601.
WEBKIT_EPOCH_OFFSET = 11_644_473_600_000_000
# A plausible Chromium timestamp (2024-01-17) for fixture nodes, as the decimal string the
# file format requires.
FIXED_DATE = "13350000000000000"

# Namespace for the deterministic GUIDs of fixture nodes. Distinct from the module's own NS so
# a fixture GUID can never collide with one the writer would generate.
TEST_NS = uuid.UUID("2b3c4d5e-6f70-5a81-92b3-c4d5e6f70a81")


class BookmarkFactory:
    """Builds Chromium-shaped bookmark nodes and documents.

    Ids are unique decimal strings allocated from 10 upwards (0-3 belong to the permanent
    roots). GUIDs are uuid5 under TEST_NS, so a document built the same way twice is identical.
    """

    def __init__(self):
        self._next_id = 10

    def _id(self):
        i = str(self._next_id)
        self._next_id += 1
        return i

    def url(self, name, url, **overrides):
        i = self._id()
        node = {
            "date_added": FIXED_DATE,
            "date_last_used": "0",
            "guid": str(uuid.uuid5(TEST_NS, "node:" + i)),
            "id": i,
            "name": name,
            "type": "url",
            "url": url,
        }
        node.update(overrides)
        return node

    def folder(self, name, children=None, **overrides):
        i = self._id()
        node = {
            "children": list(children or []),
            "date_added": FIXED_DATE,
            "date_modified": FIXED_DATE,
            "guid": str(uuid.uuid5(TEST_NS, "node:" + i)),
            "id": i,
            "name": name,
            "type": "folder",
        }
        node.update(overrides)
        return node

    def curated_folder(self):
        """The user's hand-curated folder: no marker anywhere, a few links, one sub-folder."""
        return self.folder("AI Assurance", [
            self.url("NIST AI RMF", "https://www.nist.gov/itl/ai-risk-management-framework"),
            self.url("ISO/IEC 42001", "https://www.iso.org/standard/81230.html"),
            self.folder("X", [self.url("Example X", "https://x.example.org/")]),
        ])

    def doc(self, bar=(), other=(), synced=(), version=1, with_checksum=True):
        """A minimal valid Bookmarks document: the three permanent roots with their well-known
        GUIDs and string ids, version 1, and a checksum computed by the module's own function."""
        roots = {}
        for key, kids in (("bookmark_bar", bar), ("other", other), ("synced", synced)):
            roots[key] = {
                "children": list(kids),
                "date_added": FIXED_DATE,
                "date_modified": FIXED_DATE,
                "guid": ROOT_GUIDS[key],
                "id": ROOT_IDS[key],
                "name": ROOT_NAMES[key],
                "type": "folder",
            }
        doc = {"roots": roots, "version": version}
        if with_checksum:
            doc["checksum"] = apply_brave.checksum(doc)
        return doc


class Sandbox:
    """A throwaway Brave profile and sync root under tmp_path.

    Patches every path constant in apply_brave, exports the env overrides for subprocesses, and
    replaces is_brave_running() so results never depend on what is running on the machine.
    """

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.profile = tmp_path / "profile"
        self.root = tmp_path / "root"
        self.home = tmp_path / "home"          # HOME for subprocesses, never the real one
        for d in (self.profile, self.root, self.home):
            d.mkdir()
        self.bookmarks = self.profile / "Bookmarks"
        self.bak = self.profile / "Bookmarks.bak"
        self.desired = self.root / "desired.json"
        self.backups = self.root / "backups"
        self.applied = self.root / "applied.json"

        monkeypatch.setenv("RAINDROP_SYNC_PROFILE", str(self.profile))
        monkeypatch.setenv("RAINDROP_SYNC_ROOT", str(self.root))
        for attr, value in (
            ("PROFILE", self.profile), ("BOOKMARKS", self.bookmarks), ("BAK", self.bak),
            ("ROOT", self.root), ("DESIRED", self.desired), ("BACKUPS", self.backups),
            ("APPLIED", self.applied),
        ):
            monkeypatch.setattr(apply_brave, attr, str(value))
        monkeypatch.setattr(apply_brave, "is_brave_running", lambda: False)

    # --- Bookmarks file -------------------------------------------------------------------
    def write_bookmarks(self, doc):
        """Write the document the way Chromium does (pretty printed, 3-space indent, sorted
        keys, UTF-8). Returns the exact bytes written, for byte-identity assertions."""
        text = json.dumps(doc, ensure_ascii=False, indent=3, sort_keys=True)
        self.bookmarks.write_text(text, encoding="utf-8")
        return self.bookmarks.read_bytes()

    def bookmarks_bytes(self):
        return self.bookmarks.read_bytes()

    def read_bookmarks(self):
        return json.loads(self.bookmarks.read_text(encoding="utf-8"))

    def profile_listing(self):
        return sorted(os.listdir(self.profile))

    # --- sync root ------------------------------------------------------------------------
    def write_desired(self, items):
        self.desired.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    def backup_listing(self):
        return sorted(os.listdir(self.backups)) if self.backups.exists() else []

    # --- subprocess -----------------------------------------------------------------------
    def subprocess_env(self):
        """A minimal environment for running either module as a child process. HOME points at
        an empty directory under tmp_path so even the module defaults cannot reach real data."""
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "RAINDROP_SYNC_PROFILE": str(self.profile),
            "RAINDROP_SYNC_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            # Let coverage follow the child process when CI is measuring (harmless otherwise).
            **{k: v for k, v in os.environ.items() if k.startswith("COVERAGE_")},
        }

    def run_apply(self):
        """Run apply_brave.py as a child process and return the CompletedProcess."""
        return subprocess.run(
            [sys.executable, str(SRC / "apply_brave.py")],
            env=self.subprocess_env(), capture_output=True, text=True, timeout=60, check=False,
        )


@pytest.fixture
def bm():
    """A fresh BookmarkFactory per test."""
    return BookmarkFactory()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated profile + sync root; see Sandbox."""
    return Sandbox(tmp_path, monkeypatch)
