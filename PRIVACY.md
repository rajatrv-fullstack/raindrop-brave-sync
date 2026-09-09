# Privacy

What leaves your machine, stated precisely. Nothing else does.

## Outbound traffic

| Destination | What | When |
|---|---|---|
| `api.raindrop.io` | your API token as a bearer header; reads of your library; writes of collections and tags | during the classification pass |
| the model provider behind your Claude Code session | bookmark titles, URLs and excerpts from the delta being classified | during the classification pass |

That is the complete list. The applier, the native host and the browser extension make no
network requests at all. There is no telemetry, no crash reporting, no update check, no
analytics, and no third-party service in the loop.

## What is stored, and where

Everything is local, under `~/.raindrop-sync/` with mode `0700`:

- `state.db`: a ledger of your bookmarks (id, title, URL, content hash, collection, tags).
- `desired.json`: the staged bookmarks for the browser.
- `backups/`: timestamped copies of Brave's `Bookmarks` file, kept ten per prefix.
- `log/`: applier and native-host logs. They record ids, counts and folder names, never the
  token.
- `key.pem`: the extension signing key, mode `0600`.
- `.env` or the login keychain: the Raindrop token.

Delete the directory and the extension, and nothing of yours remains.

## The browser extension

It requests four permissions: `bookmarks`, `nativeMessaging`, `alarms` and `storage`. It has
no host permissions, so it cannot read or modify any web page, and it communicates with one
process only: the native host whose id is pinned in the extension's own manifest.

## The token

The token is never written to a log, passed on a command line, or printed by any script in
this repository. `get-token.sh` emits it on stdout for a pipe and nothing else. If it ever
reaches a terminal or a transcript, rotate it; SECURITY.md treats that as an incident.

## The classification pass and the model

The classification half sends bookmark metadata to a model. If a bookmark title or excerpt is
itself sensitive, it will be sent. Nothing in this repository can change that; it is inherent
in using an LLM to classify. Keep such items out of the library, or classify by hand.
