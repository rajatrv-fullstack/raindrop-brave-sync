# Security policy

This project writes into a user's browser profile and handles a Raindrop API token. A bug in
the wrong place does not crash; it silently destroys a bookmark library or leaks a credential.
Reports are welcome and taken seriously.

## Reporting a vulnerability

Use GitHub's private reporting so the report is not public before a fix exists:

https://github.com/rajatrv-fullstack/raindrop-brave-sync/security/advisories/new

Include what you observed, the exact command or input, your Brave and macOS versions, and
whether the browser was running. Never include a real API token, a real extension id, or
contents of a real bookmark file; use a throwaway profile (see CONTRIBUTING.md).

This is a volunteer project. Expect an acknowledgement within a week and a fix or a
documented decision within a month for anything in scope. There is no bug bounty.

## In scope

- Any path by which the writer, the extension or the native host could delete, move or corrupt
  a bookmark the user did not create through this tool. The design promise is additive only,
  and a violation of that promise is the most serious class of bug here.
- Any way the Raindrop token could be written to a log, a transcript, `argv`, or a file with
  permissive modes, or sent anywhere other than api.raindrop.io.
- Native messaging: any way a page or an extension other than the one whose id is pinned in
  `allowed_origins` could reach `native_host.py`.
- The extension id derivation and the handling of `key.pem`.
- The installer writing outside `~/.raindrop-sync`, the two native-messaging directories, and
  `~/Library/LaunchAgents`.
- Anything that lets a crafted Raindrop bookmark (title, URL, note) inject into a shell, a
  SQL statement, or the bookmarks file in a way that changes structure.

## Out of scope

- Raindrop.io and Brave themselves. Findings about them belong upstream, though RESEARCH.md
  documents several Chromium behaviours and a correction there is always welcome as an issue.
- The LLM's classification choices. A bookmark filed in the wrong collection is an issue, not a
  vulnerability.
- Attacks that require an attacker to already run code as the user. The `.env` file and
  `key.pem` are protected by file mode only, and the documentation says so.

## Supported versions

The latest release and the `main` branch. Fixes land on `main` first and are tagged in the
next release; see [CHANGELOG.md](CHANGELOG.md).

## What this repo already runs

- Secret scanning with push protection, so a token cannot be pushed by accident.
- CodeQL on Python and JavaScript, plus Bandit and Semgrep, all failing the build on a finding.
- A fuzz harness (atheris) over the native host's message handler and the writer's preflight,
  45 seconds per push and ten minutes weekly. See `fuzz/README.md`.
- OpenSSF Scorecard, with every action pinned by commit hash and every CI dependency pinned by
  hash.
- Private vulnerability reporting.

## Design notes that bear on security

- The writer aborts rather than guesses: unknown file version, missing root, checksum
  mismatch, or Brave Sync metadata present all stop it before any write.
- Every write is atomic (temp file plus rename) and is re-read and verified before the backup
  is considered superseded.
- The extension holds only the `bookmarks`, `nativeMessaging`, `alarms` and `storage`
  permissions and never touches page content.
- No dependencies. The Python is standard library only and the extension has no build step,
  so there is no supply chain to compromise beyond this repository itself.
