# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Each entry is also published as the notes on the
matching GitHub release.

## [1.1.0] - 2026-09-09

A deployability release. An independent fresh-install audit of 1.0.1 found that a clone of the
public repository could not be installed at all, plus twenty verified defects a second machine
would hit. Every one of them is fixed here, and the install is now rehearsed in CI on a clean
macOS runner.

### Fixed

- **Install blocker:** `bootstrap.sh` copied `.env.example`, which `.gitignore`'s `.env.*` rule
  had kept out of the repository, so every first-time install aborted before creating anything.
  Bootstrap now writes `.env` itself and `.env.example` is tracked.
- `RAINDROP_SYNC_ROOT` never reached the native host or the launchd agent: the host manifest
  now points at a wrapper (`bin/native_host`) that pins the root and the interpreter, and the
  plist carries `RAINDROP_SYNC_ROOT` and `RAINDROP_SYNC_PROFILE`.
- The Brave profile was hard-wired to `Brave-Browser/Default`. Bootstrap now resolves the
  last-used profile across stable, Beta and Nightly from `Local State`, honours
  `RAINDROP_SYNC_PROFILE`, records the result in `config.json` (which `apply_brave.py` reads),
  refuses with a clear message when no `Bookmarks` file exists, and detects Brave Sync (no
  launchd agent is installed when it is on; the extension path still works).
- A missing Xcode Command Line Tools interpreter is detected before anything is created.
- A folder rebuild (`reset_folders.json`) removed every applied bookmark under the folder and
  the ledger then prevented them from ever being re-created. The host now clears its ledger
  rows for the folder in the same reply, so the rebuild re-creates everything.
- A staged URL in a non-canonical form (`https://WWW.example.org`) was duplicated beside the
  browser's `https://www.example.org/` by both appliers. Both now compare canonically and the
  extension trusts `chrome.bookmarks.search`.
- An entry Chromium refuses (unparseable URL, empty folder path) was retried every minute
  forever and left an empty folder behind. Such entries are now `rejected` once, terminally,
  by whichever side sees them first, and the extension validates before touching the tree.
- The service worker ran a second full sync on every wake; the top-level sync is gone.
- Host failures were swallowed: the extension now persists the error and backs off (1 minute
  doubling to 15), checks the `applied` acknowledgement, and never records a failed run as a
  success.
- The folder-path rule differed between the host, the file writer and the extension; one rule
  now, held to one table (`tests/js/folder_paths.json`) by all three test suites.
- `native_host.log`, `apply.out.log` and `apply.err.log` rotate at 1 MB.
- `is_brave_running()` recognises Brave Beta and Nightly.
- `launchctl load`/`unload` replaced by `bootout`/`bootstrap`; the plist is rendered with
  `plistlib` instead of `sed`.
- SETUP.md: prerequisites were incomplete (Command Line Tools, the Raindrop MCP connector,
  git); three verification greps could never match the agent label; the host version, the
  manifest example, the plist PATH, the Brave Sync abort text and the ledger table names were
  wrong; steps 3, 4 and 6 redid what bootstrap had done and step 3 silently replaced the key;
  the skill's token example put the token in `curl`'s argv. All corrected; the manual steps
  are now an appendix, and there are "Make it daily" and "Uninstall" sections.
- `.coverage` was committed by mistake.

### Added

- `scripts/doctor.sh` (installed as `bin/doctor.sh`): one `ok`/`warn`/`FAIL` line per check,
  exit 0 iff nothing failed, never prints the token. Run it first; send its output when asking
  for help.
- `scripts/ci-rehearsal.sh` and the `Install` workflow: a sandboxed first-time install on a
  clean macOS runner with the stock `python3`, on every push touching the installer or `src/`.
- `native_host.py --init-db` creates `state.db` with all four ledger tables, so the classifier
  never has to; the schema has one home.
- `tests/js/run.mjs`: 25 checks over the real `sw.js` under a stub `chrome`, run in CI.
- Eight new pytest tests (33 total).
- Knobs: `RAINDROP_SYNC_SKIP_PROFILE_CHECK`, `RAINDROP_SYNC_NO_KEYCHAIN`,
  `RAINDROP_SYNC_KEYCHAIN_SERVICE`; `seed-token.sh --dry-run`.

### Upgrade impact

- Re-run `scripts/bootstrap.sh` from the updated checkout, then click **reload** on the
  extension card in `brave://extensions` (the worker changed; unpacked extensions do not
  hot-reload). Your key, id, token and ledger are kept. The host manifest and plist are
  rewritten to the new wrapper and environment.
- No ledger migration: the four tables are created if missing and existing rows are untouched.

## [1.0.1] - 2026-09-09

### Added

- `fuzz/`: two atheris targets (native host message handler; bookmark checksum and preflight)
  with a `Fuzz` workflow that runs them for 45 seconds on every push touching `src/` and for
  ten minutes weekly. Crashing inputs are uploaded as workflow artifacts.
- `native_host.handle(msg)`: the dispatch is now a function that returns a reply dict for any
  input, so it can be tested and fuzzed without a pipe.

### Fixed

- `apply_brave.preflight` raised `UnicodeEncodeError` instead of aborting with exit 2 when a
  URL or id in the Bookmarks file held an unpaired surrogate. Found by the fuzz harness on its
  first run.
- `apply_brave.preflight` now also aborts cleanly on a Bookmarks file that is not valid JSON,
  has no `roots` object, or has a malformed tree, instead of raising.
- `native_host` no longer raises on a non-object message, a non-string `client`, or a
  `results` field that is not a list of objects; each returns an error reply.

### Upgrade impact

- None for users; re-run `scripts/bootstrap.sh` to pick up the new `src/`. No change to the
  extension, the manifest, the ledger schema or any file format.

## [1.0.0] - 2026-09-09

First public release.

### Added

- `src/apply_brave.py`: atomic writer for Brave's Bookmarks file with preflight aborts,
  marker-based ownership, per-prefix backups and post-write verification.
- `src/native_host.py`: stdio native-messaging host with a per-browser applied ledger.
- `extension/`: MV3 service worker that applies staged bookmarks into the running browser,
  with serialized syncs and folder rebuild support.
- `scripts/bootstrap.sh`: installer that generates the signing key, derives the extension id,
  registers the native host in the directory Brave actually reads, and installs the launchd
  fallback.
- `skill/raindrop-sync`: the classification pass for Claude Code.
- `tests/`: 25 pytest tests, run in CI on Python 3.10 and 3.12 with an 85 percent coverage
  floor.
- `RESEARCH.md`: ten documented Chromium and Brave behaviours, each marked verified,
  source-derived or untested.
- `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`, a 13-page wiki and a public roadmap board.

### Security

- No known vulnerabilities fixed in this release.
- CodeQL, Bandit and Semgrep run on every push with hash-pinned dependencies; secret scanning
  with push protection and private vulnerability reporting are enabled.

### Upgrade impact

- None; this is the first release. Install with `scripts/bootstrap.sh` as described in
  SETUP.md.

### Not included, by design

- Safari support (no supported write path exists; see RESEARCH.md finding 10).
- A bundled taxonomy, any cloud component, any telemetry.

[1.1.0]: https://github.com/rajatrv-fullstack/raindrop-brave-sync/releases/tag/v1.1.0
[1.0.1]: https://github.com/rajatrv-fullstack/raindrop-brave-sync/releases/tag/v1.0.1
[1.0.0]: https://github.com/rajatrv-fullstack/raindrop-brave-sync/releases/tag/v1.0.0
