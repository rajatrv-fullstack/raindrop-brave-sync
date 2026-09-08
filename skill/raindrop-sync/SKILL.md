---
name: raindrop-sync
description: Incremental sync of a Raindrop.io bookmark library - classify new and changed items into a pinned taxonomy, write collections and tags back to Raindrop, and stage them for the browser bookmarks bar. Triggers on 'raindrop sync', 'sync bookmarks', 'classify new bookmarks', 'bookmark sync', 'sort my bookmarks'. Read-only on the browser's Bookmarks file - an extension and a launchd agent do the writing.
---

# Raindrop → browser sync (Phase A: classify)

Phase A is this skill: network and LLM work. It touches **no browser file**.
Phase B applies `desired.json` into the browser - an unpacked extension via `chrome.bookmarks`,
with `com.raindrop-sync.apply` (launchd) as a fallback. Never write the browser's Bookmarks
file from here.

State lives in `~/.raindrop-sync/`:

| file | role |
|---|---|
| `state.db` | SQLite ledger: `bookmarks`, `meta`, `runs`, `extension_applied` |
| `taxonomy.json` | the PINNED taxonomy, generated from this library on first run |
| `collection-map.json` | taxonomy path → Raindrop `collection_id` |
| `desired.json` | staged browser bookmarks, consumed by Phase B |
| `reset_folders.json` | asks Phase B to tear down and rebuild a managed folder |

## Rules that are not negotiable

### 1. Never re-derive the taxonomy

Classify into the existing tree, read from `taxonomy.json`. Re-deriving it each run regroups the
same bookmarks under different names every night, so the bookmarks bar is permanently unstable
even when every individual write is correct. Re-derivation is a separate, explicitly requested
operation that bumps `meta.taxonomy_version` and reclassifies everything.

Extending the tree with a NEW collection is not re-deriving, and is allowed - see rule 3.

### 2. Delta on content hash, never on a timestamp

`content_hash = sha256(link|title|excerpt|note|type)`. Classify an item when its id is absent
from the ledger, when its hash has changed, or when `taxonomy_version` has moved.

Raindrop bumps `lastUpdate` on **every** write, including this job's own, so a timestamp
watermark re-selects the entire library on every subsequent run, forever.

Hash the **raw source values**, never curated ones. If the ledger stores a cleaned title or a
URL with tracking parameters stripped, and the hash is computed from those, the hash can never
be reproduced from the API response - and every affected item re-flags as EDITED on every run.
The ledger's `title`/`link` columns and its `content_hash` are allowed to disagree; that is the
design, not a defect to repair.

Write `content_hash` only **after** the item is committed to `desired.json`. A crash then costs
one redundant reclassification rather than a silent skip.

### 3. Nothing is noise. There is no bin.

Every bookmark in the library was a deliberate act. A URL carrying an advertising click-id, or
arriving through a link shortener, tells you **how it got there** - never whether it matters.
Judge the destination and the interest it reveals.

- `Noise`, `Junk`, `Misc`, `Other`, `Various`, `Uncategorised` are forbidden collection names.
  So is any catch-all that quietly becomes a bin under a friendlier name.
- **When nothing fits, create a new collection.** That is the correct outcome, not a fallback.
  Say what was added in the run summary.
- A dead link or an unreadable slug title is a research problem - follow the redirect, try the
  Wayback Machine, establish what it pointed at - not grounds for discarding it.
- Keep provenance tags (`paid-ad`, `short-link`) as facts. They must never drive placement.
- Assume the library spans many unrelated interests, not one professional domain. Design for the
  next five hundred saves, not for a tidy-looking tree today.

A classifier that bins what it does not recognise destroys exactly the signal it was built to
find. If something is genuinely unfamiliar, that is information about a gap in the taxonomy.

### 4. Never delete, never overwrite a human's work

Quarantine is not deletion and neither is available here. Deletion propagation is off: an item
missing from a read is far more likely to be a paging race than a real removal.

## Procedure

1. Read the ledger: `SELECT raindrop_id, content_hash FROM bookmarks;`
2. Fetch with `find_bookmarks`, paging **ascending** (`sort: created_asc`, `limit: 150`).
   Never page newest-first: a bookmark saved mid-run shifts the window, an item is skipped, and
   deletion logic misreads the gap as a removal.
3. Compute the delta per rule 2.
4. Classify each delta item into exactly one path from `collection-map.json`, plus one to four
   tags from the taxonomy's controlled vocabulary. Nothing outside those vocabularies, except a
   new collection created under rule 3.
5. Write back with `update_bookmarks`, grouped by (collection, tagset); at most 150 bookmark ids
   per call. Tags use `{"add": [...]}`, which appends - read-modify-write to replace.
   You cannot `add` and `remove` tags in one operation; the API rejects it. Split them.
6. Append to `desired.json` as `{raindrop_id, name, url, folder_path}`, with `folder_path`
   relative to the bookmarks bar, e.g. `Raindrop/<Top Level Collection>`. Keep it **flat** - one level. Hover menus make every extra level of nesting a tax on the reader.
7. Update the ledger and insert a row into `runs`.
8. Do not write the browser's Bookmarks file. Phase B applies within about a minute.

## Writing into a hand-curated folder

If the user maintains their own curated folders outside the managed one, promotion into those is
**reject-by-default** and separate from the mirror. Such a bar tracks organisations and their
primary sources. Reject: URLs that will rot (repository blob deep links, query-filtered views),
commentary *about* a field rather than a primary source, one-off vendor landing pages, job
postings, news articles. A new curated folder needs at least three qualifying items.

Ask before promoting if you hesitate. A rejected item loses nothing; a wrongly promoted one
quietly degrades something a person built by hand.

## Secrets

The API token lives in the keychain or `.env` (mode 0600). **Never echo it** - not to stdout,
not into a variable you later print, not while probing which field holds it. Pipe it:

```bash
scripts/get-token.sh | xargs -I{} curl -s -H "Authorization: Bearer {}" \
  "https://api.raindrop.io/rest/v1/raindrops/0?perpage=50&page=0&sort=created"
```

If a token does reach a transcript, say so plainly and tell the user to rotate it. Do not move on
quietly.

## Canary

If a routine run's delta exceeds ~50 items, delta detection is broken. Say so loudly instead of
reclassifying the whole library. That number is the tell for every failure mode in this design.
