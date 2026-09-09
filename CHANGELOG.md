# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Each entry is also published as the notes on the
matching GitHub release.

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

[1.0.0]: https://github.com/rajatrv-fullstack/raindrop-brave-sync/releases/tag/v1.0.0
