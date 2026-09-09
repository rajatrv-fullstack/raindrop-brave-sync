# Documentation index

Everything here is in the repository or its wiki. This page exists so the documentation is
discoverable from one place.

## Install and run

- [SETUP.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/SETUP.md): prerequisites, the installer, the extension, the
  launchd fallback, the first classification pass, and a verify-your-install section.
- [Operations](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Operations): day-two use, logs, cadence, rebuilding a folder, moving
  machines, uninstalling.
- [Troubleshooting](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Troubleshooting): symptom-first, with the diagnosis command
  for each.

## Use

- [README](https://github.com/rajatrv-fullstack/raindrop-brave-sync#readme): what it does, the architecture, the design principles.
- [Taxonomy and Classification](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Taxonomy-and-Classification): how the taxonomy is
  built, why it is pinned, how it grows, and the rule that nothing is noise.
- [Ledger and File Formats](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Ledger-and-File-Formats): every file the tool writes
  and reads.
- [FAQ](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/FAQ) and [Glossary](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Glossary).

## Use securely

- [SECURITY.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/SECURITY.md): reporting, scope, and the controls that run on
  every push.
- [scripts/doctor.sh](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/scripts/doctor.sh): the health check to run first, and to
  ask a friend for; [scripts/ci-rehearsal.sh](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/scripts/ci-rehearsal.sh): the
  fresh-install rehearsal CI runs on every push.
- [fuzz/README.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/fuzz/README.md): the fuzz targets, their contracts, and
  how to replay a crashing input.
- [PRIVACY.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/PRIVACY.md): the complete list of what leaves your machine.
- [Safety and Ownership Model](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Safety-and-Ownership-Model): what can and cannot
  happen to your bookmarks, and why.
- Do not pack the extension as a CRX, never print the API token, never test against a live
  profile. Each of these is explained in [RESEARCH.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/RESEARCH.md) and
  [CONTRIBUTING.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/CONTRIBUTING.md).

## Research

- [RESEARCH.md](https://github.com/rajatrv-fullstack/raindrop-brave-sync/blob/main/RESEARCH.md): the Chromium and Brave behaviours behind the
  design, each labelled verified, source-derived or untested.
- [Research Status](https://github.com/rajatrv-fullstack/raindrop-brave-sync/wiki/Research-Status): the same findings with their current
  verification state and the issue tracking each open one.

## Images

- [images/](https://github.com/rajatrv-fullstack/raindrop-brave-sync/tree/main/docs/images): the README screenshots, captured from a throwaway
  profile filled with invented data.
