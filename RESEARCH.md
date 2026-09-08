# RESEARCH — undocumented behaviour in Chromium, Brave and the Raindrop API

Everything below was established by experiment on a real machine while building this project,
not copied from documentation. Most of it is not written down anywhere else, and each item cost
real time to find. If you arrived here from a search engine at 2am, the finding you want is
probably in the table of contents.

Environment for every result unless stated otherwise:

| | |
|---|---|
| OS | macOS 26 (Darwin 25.5.0) |
| Browser | Brave 152.1.94.121 (Chromium 152) |
| Profile | `~/Library/Application Support/BraveSoftware/Brave-Browser/Default/` |
| Test fixture | a copied profile with roughly 120 bookmarks and 16 folders on the bar, `version: 1`, Brave Sync off |
| Raindrop | free plan, REST v1 |
| Dates | 2026-09-05 through 2026-09-07 |

Version numbers matter here. Several of these behaviours are Chromium implementation details
with no compatibility promise; re-verify after a major browser upgrade. Where a claim comes from
reading Chromium source rather than from an experiment on this machine, it is labelled as such.

---

## Table of contents

1. [Chromium's bookmark checksum hashes titles as UTF-16LE](#1-chromiums-bookmark-checksum-hashes-titles-as-utf-16le)
2. [Brave reads native-messaging manifests from Chrome's directory](#2-brave-reads-native-messaging-manifests-from-chromes-directory)
3. [Packing a CRX permanently poisons the extension id](#3-packing-a-crx-permanently-poisons-the-extension-id)
4. [Brave reads the Bookmarks file exactly once, at startup](#4-brave-reads-the-bookmarks-file-exactly-once-at-startup)
5. [`Bookmarks.bak` is not a safety net](#5-bookmarksbak-is-not-a-safety-net)
6. [A malformed Bookmarks file silently produces a fresh profile](#6-a-malformed-bookmarks-file-silently-produces-a-fresh-profile)
7. [Raindrop bumps `lastUpdate` on every write, including yours](#7-raindrop-bumps-lastupdate-on-every-write-including-yours)
8. [MV3 lifecycle races create duplicate bookmarks](#8-mv3-lifecycle-races-create-duplicate-bookmarks)
9. [`chrome.bookmarks` has no custom metadata field](#9-chromebookmarks-has-no-custom-metadata-field)
10. [Safari is not possible](#10-safari-is-not-possible)
11. [Smaller findings](#11-smaller-findings)
12. [How to verify these yourself](#12-how-to-verify-these-yourself)
13. [Verification status of every claim](#13-verification-status-of-every-claim)

---

## 1. Chromium's bookmark checksum hashes titles as UTF-16LE

### The belief

The `checksum` field at the top of Chromium's `Bookmarks` JSON is an MD5 over the file, or over
some canonical serialisation of it, in UTF-8 like everything else in the document. Write the JSON,
hash the bytes, done.

### What actually happens

It is an MD5 over a **pre-order walk of the node tree**, not over the file. No separators, no
lengths, no field names, no JSON — just concatenated field bytes in a fixed order. And the
encoding is not uniform:

* node `id` — UTF-8
* node **`name` (title) — UTF-16LE**
* the type literal, the ASCII string `"url"` or `"folder"` — UTF-8
* for URL nodes, the `url` — UTF-8
* for folders, recurse into `children` in document order

Only the title is UTF-16. This is the entire puzzle, and it is why every naive implementation
produces a digest that is right for ASCII-titled test data and wrong the moment a real library
with an accented word or an emoji shows up.

The reason is visible in `BookmarkCodec`: the title is a `base::string16` and its **native
UTF-16 buffer** is fed to the digest, while ids and URLs are `std::string` and go in as UTF-8.
Little-endian because Chromium's targets are; on a big-endian host the on-disk digest would
differ, which is a hypothetical, not a portability plan.

Working implementation, in stdlib Python:

```python
import hashlib

def bookmark_checksum(doc):
    """Reproduce Chromium/Brave's Bookmarks `checksum` field."""
    m = hashlib.md5()
    u8  = lambda s: m.update(s.encode("utf-8"))
    # surrogatepass: Chromium hashes raw UTF-16 code units, so an unpaired
    # surrogate that arrived as a \uD800 escape in the JSON must pass through
    # rather than raise. The default codec raises.
    u16 = lambda s: m.update(s.encode("utf-16-le", "surrogatepass"))

    def node(n):
        u8(n["id"])
        u16(n.get("name", ""))
        u8(n["type"])
        if n["type"] == "url":
            u8(n.get("url", ""))
        else:
            for c in n.get("children", []):
                node(c)

    for root in ("bookmark_bar", "other", "synced"):
        node(doc["roots"][root])
    return m.hexdigest()
```

Three traps inside those fifteen lines:

* **`surrogatepass`.** Titles can contain unpaired surrogates. Python's plain `utf-16-le` codec
  raises `UnicodeEncodeError`; Chromium does not care, it hashes code units. Without the error
  handler your job crashes on exactly the one bookmark you cannot reproduce on demand.
* **Order is document order.** The digest covers the three permanent roots in the order
  `bookmark_bar`, `other`, `synced`, and children in their stored order. Sorting anything, even
  for tidiness, changes the digest.
* **The type literal is hashed.** Omitting it produces a digest that is stable and wrong.

### How it was verified

Both directions, which is what makes it trustworthy:

1. **Read direction.** Compute over the untouched file that Brave itself had just written and
   compare against the `checksum` value stored in that same file. Exact match, including
   non-ASCII titles.
2. **Write direction.** Write the file with an independently computed checksum, let Brave load
   it, let Brave rewrite it from memory, recompute. Match again, and no node was renumbered.

```bash
python3 - <<'PY'
import json, sys
doc = json.load(open("Bookmarks"))
print("stored:  ", doc["checksum"])
print("computed:", bookmark_checksum(doc))   # function above
PY
```

Round-tripping a torture set — emoji, an astral-plane character (U+1D54F), CJK and Devanagari —
through both our writer and Brave's confirmed the encoding rule rather than merely not
contradicting it.

### What it means for you

* **Brave 152 never reads the checksum.** Chromium CL `115ab853ce94` removed the read side,
  with the commit explicitly noting the change was kept easy to revert. So on M152 a wrong
  checksum is currently harmless. Write it correctly anyway: the read side is one revert away,
  and other tools read the file too.
* **Do not add `checksum_sha256`.** The SHA-256 variant is behind
  `kEnableBookmarkCodecSHA256`, disabled by default, and the key is simply absent from files
  written by this browser version. Adding a field Chromium is not expecting is a gratuitous risk.
* **Use it as a tripwire, not just as an output.** Recomputing the checksum on read and
  comparing with the stored value is a cheap detector for "something else is editing this file".
  Our writer aborts on mismatch.
* **Pre-M152, a mismatch is expensive.** With the read side present, a checksum mismatch sets
  `required_recovery`, and recovery **renumbers every node in the tree**. Any state you keep
  keyed on Chromium's node `id` is invalidated in one shot. This is derived from the codec
  source, not exercised here — M152 does not read the checksum, so it cannot be triggered on
  this build. It is the reason our design anchors identity on **GUIDs**, which survive recovery,
  and treats `id` as a per-file allocation detail.

---

## 2. Brave reads native-messaging manifests from Chrome's directory

### The belief

Brave is its own Chromium product with its own application-support directory, so a native
messaging host manifest belongs in

```
~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/
```

The directory even exists in the profile, which makes the belief feel confirmed.

### What actually happens

On macOS, Brave does **not** override Chromium's stock product path for native messaging host
lookup. It searches Google Chrome's directory:

```
~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
```

A manifest placed only in Brave's own directory is **silently ignored**. `connectNative()` fails
in the extension with a generic "host not found" and no indication that the manifest was found
but rejected, or that the wrong directory was searched. This cost four rounds of failed testing —
each round spent debugging the host script, which was fine the whole time.

The tell was on the machine already: other vendors' native-messaging installers ship the same
manifest to **both** directories. That is not redundancy, it is the workaround.

### How it was verified

The extension console is effectively unreachable for an MV3 service worker in a
launched-with-flags profile, so the diagnosis has to come from Brave's own logging:

```bash
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

"$BRAVE" \
  --user-data-dir=/tmp/brave-lab \
  --load-extension="$HOME/.raindrop-sync/extension" \
  --enable-logging=stderr --v=0 \
  about:blank 2>&1 | grep -iE "native|<your-host-name>"
```

The decisive line is:

```
launch_context.cc:148 Can't find manifest for native messaging host <host name>
```

Copy the manifest into Chrome's directory, relaunch with the same flags, and the line is gone and
the host process spawns. That is a clean single-variable experiment: nothing but the manifest's
location changed.

### What it means for you

* **Install the manifest into both directories.** Chrome's is the one that works today; Brave's
  costs nothing and covers the day they fix it.
* `--enable-logging=stderr --v=0` is the only practical window into native-messaging and
  extension plumbing. Learn it before you need it.
* `allowed_origins` in the manifest must name the extension's origin, and the extension id is
  per-install — see finding 3. Whenever the id changes, **every** copy of the manifest must be
  updated, or you get the same silent failure from a different cause.
* Do not assume any Chromium fork uses its own product path for a given lookup. Brave overrides
  many paths; this is not one of them. Verify per feature, not per browser.

---

## 3. Packing a CRX permanently poisons the extension id

### The belief

To install a locally built extension without the Web Store, sign a CRX with your own key,
register it via an `External Extensions` preference file, and get a stable id plus a normal
install. If the browser objects, flip the enable toggle once and move on.

### What actually happens

The install succeeds and the extension is dead on arrival, permanently.

Registering a locally signed CRX does make Brave install it — `Preferences` records
`location: 2` (`EXTERNAL_PREF`) — and then disables it with:

```
disable_reasons: [256]        // DISABLE_NOT_VERIFIED
```

This is **not** a one-time prompt the user can clear. Chromium's `InstallVerifier` enforces the
id against a signed allowlist stored in `Preferences → extensions.install_signature.ids`.
A locally built CRX is not in that list, so re-enabling is reverted, and the enable toggle at
`brave://extensions` ends up greyed out for good.

The genuinely nasty part, and the reason this deserves a finding rather than a footnote:

> **The poison attaches to the extension id, not to the installation.**

Remove the external registration entirely, then load the **same folder** as unpacked. The load
*succeeds* — `Preferences` shows `location: 4` (`UNPACKED`) — and the extension **stays disabled
and un-enableable**, because the `DISABLE_NOT_VERIFIED` flag is keyed on the id and was inherited.
There is no UI, no flag and no profile edit that clears it.

The only escape is a **brand-new extension id**:

1. regenerate the signing key (`key.pem`),
2. update `manifest.json.key`,
3. update `allowed_origins` in **every** native host manifest — all copies, see finding 2,
4. **Remove** the poisoned card at `brave://extensions`,
5. **Load unpacked** again.

The first identity minted for this project was burned exactly this way and had to be abandoned.

**Unpacked extensions are exempt from the install verifier.** That is not a loophole, it is the
supported route for a personally built extension, and it is the only one that works.

### How it was verified

Read the profile's `Preferences` directly rather than trusting the UI:

```bash
python3 - <<'PY'
import json
p = json.load(open("Default/Preferences"))
for ext_id, s in p["extensions"]["settings"].items():
    if s.get("disable_reasons"):
        print(ext_id, "location:", s.get("location"), "disable_reasons:", s["disable_reasons"])
print("verified ids:", len(p["extensions"].get("install_signature", {}).get("ids", [])))
PY
```

The sequence observed: `location: 2, disable_reasons: [256]` after the CRX registration; then,
after removing the registration and loading the identical directory unpacked,
`location: 4, disable_reasons: [256]` — location changed, disable reason did not. The toggle in
the UI was greyed out throughout. A newly keyed id, loaded unpacked, came up enabled and stayed
enabled.

Extension ids are per-install and derived from the key. Real ones are never printed here; the
shape is 32 lowercase letters a–p, e.g. `abcdefghijklmnopabcdefghijklmnop`, and yours will differ.

### What it means for you

* **Never pack a CRX for a locally distributed extension, not even to "just test the install
  path".** One test permanently costs you that identity.
* Go straight to Developer mode → **Load unpacked**.
* Treat the extension id as burnable but expensive: keep `key.pem` safe (`0600`), because losing
  it changes the id and breaks every `allowed_origins` entry; but also make id changes cheap in
  your own tooling, because you may be forced into one.
* If you inherit a project whose extension "installs but will not enable", check
  `disable_reasons` in `Preferences` before spending a day on the manifest. `256` means the
  identity is gone, not the code.

---

## 4. Brave reads the Bookmarks file exactly once, at startup

### The belief

The `Bookmarks` file is the store. Edit it, and the browser picks the change up — immediately, or
at worst after a reload.

### What actually happens

Brave reads `Bookmarks` **exactly once**, during profile load, into an in-memory `BookmarkModel`
that is thereafter **authoritative**. There is no file watcher. Every write to the file after that
is Brave serialising its whole in-memory tree over the top, and it does so:

* on a debounce roughly **2.5 seconds after any bookmark change**, and
* again at quit.

Two consequences follow, and they point in opposite directions:

* **An external edit made while Brave is running is invisible** — the model does not know about
  it — **and is destroyed** by the next serialise, which writes a tree that never contained your
  nodes.
* **An external edit made while Brave is closed is picked up in full** at the next start,
  because that is the one moment the file is read.

### Why this is not a data-loss risk

This is worth stating precisely, because "a background job edits your bookmarks file while the
browser is running" sounds reckless and is not:

* **Both writers are atomic.** Ours uses `os.replace`; Chromium's `ImportantFileWriter` writes a
  temp file and renames. A rename on the same volume is atomic, so the file is never torn. One
  version wins whole.
* **The user's bookmarks are not on disk in any meaningful sense** while Brave runs — they are in
  Brave's in-memory model, which is the thing that gets written out. Losing the race cannot
  delete a user bookmark, because the loser's content was never the source of that data.
* **The only casualty is your own pending additions**, which you can simply re-apply.

So a retry loop converges. Write regardless of Brave's state; on the next tick, re-read the file
and check whether your marked nodes are present; if not, write again. The write sticks the moment
Brave has a quiet tick, and it is *guaranteed* to stick once Brave restarts, because that is when
Brave reads your file. **Never kill Brave to win the race** — quitting Brave triggers the
shutdown serialise, which is the very thing that would discard your write, and it is hostile to
the user besides.

The genuine catastrophic risk is a *malformed* file, not a *concurrent* one. That is finding 6,
and it is guarded independently.

### How it was verified

Throwaway-profile method (section 12), in two passes:

1. **File survives load.** Copy the profile, inject nodes into `Bookmarks`, launch
   `Brave --user-data-dir=<copy>`, quit, diff. Result: the file was byte-identical after Brave
   ran — 0 GUIDs vanished, 0 ids changed, 0 names changed, `required_recovery` never fired.
   That proves Brave accepted an externally authored file, but not that the nodes reached the
   model rather than being passed through.
2. **Nodes are in the model, and survive Brave's own rewrite.** Repeat with a probe extension
   loaded unpacked, holding the `bookmarks` permission, that calls `chrome.bookmarks.search({})`
   and `chrome.bookmarks.create()`. The `create()` forces a full serialise from memory. The probe
   logged `total=141 ours=6`, i.e. the six injected **URL** bookmarks were in the in-memory model (the seventh node is the
   folder holding them, which `search({})` does not return). All seven
   injected nodes survived the rewrite Brave performed itself.

That second pass is the one that matters. Without it you have only shown the file was not
rejected.

### File-format details worth knowing while you are in there

* `id` is a **decimal string**, not a number. So is every date.
* Dates are **microseconds since 1601-01-01**:
  `date = int(time.time() * 1e6) + 11644473600000000`.
* A URL node carries `date_added`, `date_last_used`, `guid`, `id`, `name`, `type`, `url`;
  a folder is the same minus `url`, plus `children` and `date_modified`.
* GUIDs are the durable identity anchor. Generate them deterministically from your source id
  (`uuid5(NS, "url:" + source_id)`) and re-runs become byte-identical instead of churning.
* **Locate your nodes by walking the whole tree, never by position.** Positional lookup silently
  duplicates the first time the user drags a folder, and Chromium then re-randomises one of the
  duplicate GUIDs, severing your identity anchor permanently.

---

## 5. `Bookmarks.bak` is not a safety net

### The belief

`Bookmarks.bak` sitting next to `Bookmarks` is the last known-good copy. Worst case, rename it
back.

### What actually happens

`Bookmarks.bak` is a copy of **whatever was on disk at the first save of the current browser
session**, taken once per session behind a latch (`backup_triggered_`). It is not a rolling
history and not a validity check — Chromium never inspects the file it is copying.

The failure sequence is short:

1. You (or anything else) write `Bookmarks`.
2. Brave starts, reads your file, and on its first save of the session copies **your file** over
   `Bookmarks.bak`.
3. The previous good copy is gone. One session is all it takes.

And nothing in Chromium ever reads `.bak` back. There is **no restore-from-backup path in the
product** — not automatic, not behind a flag, not in the UI. The file exists for a human with a
terminal, and only until step 2.

### How it was verified

This one is honest about its provenance: it is read from Chromium's bookmark storage source
(the once-per-session backup latch, and the absence of any read of the `.bak` path), and it was
adopted as a design constraint rather than deliberately reproduced. Reproducing it means
deliberately destroying a backup, which is exactly the experiment you do not want to run on
anything you care about. If you do want to confirm it, do it in a throwaway profile: note the
mtime and hash of `Bookmarks.bak`, launch Brave against the copy, add one bookmark, quit, and
compare.

### What it means for you

* **Make your own backups**, timestamped, of **both** `Bookmarks` and `Bookmarks.bak`, before
  every write. Keep the last N (we keep 10).
* **Re-open each backup after copying and confirm it parses** before you proceed. A backup you
  have not read is a hope, not a backup.
* Do not build any recovery story on `.bak`. Assume it holds your own most recent output.

---

## 6. A malformed Bookmarks file silently produces a fresh profile

### The belief

If you write a broken bookmarks file, the browser will complain — an error dialog, a "your
bookmarks could not be loaded" bar, a fallback to the backup, something.

### What actually happens

Silence. This is the worst outcome in the whole system and it has no error surface at all.

On a parse failure, on `version != 1`, or on any of the three permanent roots
(`bookmark_bar`, `other`, `synced`) being missing, Chromium's `model_loader.cc` records a **UMA
metric and nothing else**. Brave then constructs three **empty permanent nodes** and runs exactly
like a fresh profile: no bookmarks bar contents, no dialog, no error page, no prompt, no
restore path.

Then finding 5 completes the disaster: the first save of that session copies the broken file over
`Bookmarks.bak`, destroying the last good copy. A user who does not immediately understand what
happened, and who simply restarts the browser hoping it clears up, loses the recovery window.

That is how you turn a JSON bug into permanent data loss without ever seeing an error message.

### How it was verified

Derived from the loader source, and deliberately **not** reproduced end-to-end on a populated
profile. It was instead designed against: the guard list below exists precisely because the
failure is silent, so *your verification is the only error surface that exists*.

If you want to see it, use a throwaway profile and a fixture with a truncated `Bookmarks` file.
Never a real one.

### The guards

These are the preflight and postflight checks that make writing this file defensible.

**Preflight — abort on any of these, do not "repair":**

* `Bookmarks` missing while the profile is otherwise populated → abort and alert.
  **Never create a fresh file.** A missing file is a symptom, and writing a new one is
  indistinguishable from the wipe you are trying to prevent.
* `version != 1`, or any of `bookmark_bar` / `other` / `synced` missing.
* Recomputed MD5 does not match the stored `checksum` → something else is editing; refuse.
* `AccountBookmarks`, `EncryptedBookmarks2` or `EncryptedAccountBookmarks2` present — a different
  storage regime you are not modelling.
* Top-level `sync_metadata` present → browser sync is on. Injected nodes then trip
  `CorruptionReason::UNTRACKED_BOOKMARK`, all sync metadata is discarded, a full re-merge
  follows, and your folders upload to **every** device on the chain. Hard abort.

**Write:**

```
mkstemp in the profile directory (same volume, so rename is atomic)
  → write → flush → os.fsync(fd)
  → chmod 0600
  → os.replace(tmp, target)
  → fsync the directory fd
  → unlink the temp file on any exception
```

**Postflight — re-read from disk and verify, every time:**

* it parses,
* the checksum recomputes,
* `id` values are unique across all three roots,
* `guid` values are unique.

On any failure, restore your backup atomically and exit non-zero. Do **not** restore merely
because the browser is running — losing that race costs only your own additions (finding 4), and
restoring would undo the user's real work.

**Ownership by marker, not by folder.** Every node this project creates carries
`meta_info: {"raindrop_sync": "v1"}`, and the writer may only add or remove **marked** nodes.
Unmarked nodes are untouchable by construction, which is what makes it safe to place generated
bookmarks inside the user's real hand-curated folders instead of quarantining them in one
"synced" folder. When rebuilding a folder, harvest and re-attach its **unmarked** children —
users will drop their own bookmarks in there. Zero marker matches means create; more than one
means abort and let a human look.

Verified as part of this work: `meta_info` survives Brave's own round-trip, 7 of 7 nodes. That is
the load-bearing assumption for marker ownership — re-verify it after major Brave upgrades.

---

## 7. Raindrop bumps `lastUpdate` on every write, including yours

### The belief

Incremental sync is a watermark: remember the timestamp of the last run, ask for everything with
`lastUpdate` after it, process the delta. Standard, boring, correct.

### What actually happens

Raindrop bumps `lastUpdate` on **every** write to an item, including writes made by your own
sync. If your job writes anything back — a collection assignment, a tag — then every item it
touched is "modified" as of that instant. The next run's watermark query returns everything you
just wrote. And since that run writes again, it re-poisons the watermark. **The watermark
re-selects the entire library, forever.** There is no run at which it settles.

There is also no server-side operator that rescues it. The documented search operator table
supports `<` and `>` on `created:` and pointedly **not** on `lastUpdate:`, and the date operators
are day-granular at best — so even a working operator could not express an intra-day delta. That
path is not a future optimisation; delete it rather than carrying it.

### The fix: a content hash

Keep a ledger keyed on the source item id, storing a hash of the item's content:

```
content_hash = sha256(link | title | excerpt | note | type)
```

Classify an item if and only if:

* its id is absent from the ledger (**NEW**), or
* its stored hash differs from the freshly computed one (**EDITED**), or
* your taxonomy version changed.

Ordering rule: **mark the item's hash as classified only after the result is committed
downstream.** Crash between the two and you pay one redundant reclassification. Do it the other
way and you get a silent skip, which is unrecoverable because nothing will ever flag that item
again.

### The second-order trap — hash RAW values, not curated ones

This is the expensive part, and it is a trap you can fall into *after* you have correctly
rejected the timestamp watermark.

The first run of this system wrote **curated** values into the ledger's `title` and `link`
columns — emoji stripped, over-long blurbs shortened, HTML entities decoded, tracking parameters
removed — and then hashed **those**. Everything looked fine.

On the next run, **168 items (about 28% of the library) re-flagged as EDITED**, and would have
done so on every run forever. The reason is simple once seen: a hash of an edited record cannot
be reproduced from the source. The source still says `Foo &amp; Bar [emoji]`; your ledger says
`Foo & Bar`; the hashes will never agree. This is the *same failure mode* the timestamp watermark
was rejected for, reached by a completely different route.

The invariant, which requires two columns that **intentionally disagree**:

| column | holds | used for |
|---|---|---|
| `content_hash` | `sha256(raw_link\|raw_title\|...)` — verbatim source values | delta detection **only** |
| `title`, `link` | the curated display values | naming in the browser, staged output, human reading |

Never "repair" these into agreement. A future maintainer looking at a ledger row whose `title`
does not match the string that produced its hash will be tempted to recompute the hash from the
stored columns. That single act of tidying reintroduces the bug and re-flags a quarter of the
library indefinitely. Leave a comment saying so.

Two corollaries:

* **Widening the hash is a breaking change.** If you add a field to the hash input, every stored
  hash is invalidated at once and the entire library re-classifies. That is a
  `taxonomy_version`-class operation and must be explicit and user-invoked, never a silent
  upgrade shipped in a nightly job. (Our hash keeps `excerpt|note|type` slots present but always
  empty, because the classifier's data source does not return them — the format is stable even
  though the fields are unused.)
* **A missing API token degrades reconciliation, not hashing.** Twice during this project it was
  concluded that the hash could not be computed because a richer API surface was unavailable.
  Both conclusions were wrong: link and title were available the whole time, and the empty slots
  are empty *by design*. The error left several rows with a NULL hash for two days, during which
  edit detection was blind for them. Compute the hash from whatever authoritative source you
  already have.

### Reading efficiently, without introducing a paging race

* **Gate cheaply:** `GET /rest/v1/user/stats` → `meta.changedBookmarksDate`. Unchanged since last
  run → exit without reading the library.
* **Then one call:** `GET /rest/v1/raindrops/0/export.csv`. It is an atomic server-side render —
  cheaper than paging and immune to the paging race below.
* **If you must page, page ASCENDING** (`sort=created`). Newest-first paging shifts the window
  when a bookmark is saved mid-run, so an item silently falls between pages. Deletion logic then
  reads that absence as a deletion, and you have invented a delete that never happened.
* **Add a max-staleness override** — force a full read every N days (we use 7) regardless of the
  gate. Nothing documents which mutations advance `changedBookmarksDate`, so do not let a
  zero-call fast path be your only path.

**Untested, flagged honestly:** whether a *tag-only* edit advances `changedBookmarksDate` has not
been verified. Until it is, this project does a full read every time. If you rely on that gate,
test it first with a single tag edit.

### Other Raindrop API behaviour worth knowing

* **`tags` appends, it does not replace.** Tag writes must be read-modify-write, or a
  "correction" silently accumulates both the old and the new tag.
* **Writes with `collectionId 0` are rejected.**
* **`/raindrop/{id}/suggest` is Pro-gated.** It is tempting as a free classification tier; it is
  not available on a free plan (expect 402/403 — one curl confirms it for your account, and this
  particular check remains untested here). Its sibling, semantic search, already hard-errors on a
  free account.

### The canary

Log the number of items classified per run and alert above a small threshold (we use ~50).
That single number is the tell for **every** failure mode in this section: a poisoned watermark,
a curated-value hash, a silently widened hash, a paging race. If it is not small on a steady-state
day, something is re-selecting your library.

---

## 8. MV3 lifecycle races create duplicate bookmarks

### The belief

An MV3 service worker's top-level code runs, and then lifecycle events like
`chrome.runtime.onInstalled` fire. Roughly sequential. An idempotent "does this already exist?"
check is therefore enough.

### What actually happens

Worker load and `onInstalled` can fire in the **same millisecond**. Both paths ran the same
existence check, both saw the bookmark absent, both called `chrome.bookmarks.create()`.
Two identical bookmarks appeared in the bar.

An existence check is not idempotence when two copies of it interleave. `chrome.bookmarks` gives
you no uniqueness constraint, no upsert and no transaction — nothing stops the second create.

### How it was verified

Observed directly: after installing and loading the extension, the bar contained two identical
entries for a single staged item, and the host log showed the work performed twice.

After the fix, the same sequence logs cleanly:

```
pending: 1 of 1  →  already_present  →  0 of 1
```

one bookmark, no duplicate, checksum intact.

### The fix

Funnel every sync through a single promise chain, at module scope in the worker:

```js
// sw.js — do not remove this lock.
let chain = Promise.resolve();
const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;

// every entry point goes through it
chrome.runtime.onInstalled.addListener(() => serialize(syncOnce));
chrome.runtime.onStartup.addListener(()   => serialize(syncOnce));
chrome.alarms.onAlarm.addListener(()      => serialize(syncOnce));
serialize(syncOnce);                                  // top-level worker load
```

Note `chain.then(fn, fn)` — the second argument runs the next task even if the previous one rejected, and the separate `.catch` keeps the chain itself alive while still surfacing the rejection to the caller, so one
failed sync does not wedge every future one.

### What it means for you

* **In MV3, treat every entry point as concurrent**: top-level worker code, `onInstalled`,
  `onStartup`, `onAlarm`, `onConnect`, `onMessage`. They are not ordered relative to each other.
* The promise chain protects you *within* a worker instance. MV3 kills idle workers, so it does
  not protect across restarts — cross-instance safety has to come from an idempotent existence
  check plus a durable ledger outside the browser (see finding 9).
* Do not "fix" duplicates by adding a delay. You will move the race, not close it.

---

## 9. `chrome.bookmarks` has no custom metadata field

### The belief

The extension API is a view onto the same data as the `Bookmarks` file, so anything the file
format can express — in particular the per-node `meta_info` dictionary — is reachable from
`chrome.bookmarks`.

### What actually happens

It is not. `chrome.bookmarks.BookmarkTreeNode` exposes `id`, `parentId`, `index`, `url`, `title`,
`dateAdded`, `dateGroupModified` and `unmodifiable`. There is **no arbitrary metadata field**, and
no API for writing the file format's `meta_info`. An extension cannot stamp its own nodes, and it
cannot read a stamp written into the file by anything else.

This matters far more than it looks, because it breaks the obvious architecture for a hybrid
system. This project has two appliers — an extension that writes into the running browser, and a
file writer that runs on a timer as a fallback — and **they cannot share an ownership marker**:

* the file writer marks nodes `meta_info: {"raindrop_sync": "v1"}`;
* the extension physically cannot;
* so a marker-only ownership check does not see extension-created nodes, and the file writer
  cheerfully re-creates them.

### The consequences, and what to do instead

* **Ownership of extension-created nodes must live outside the browser.** Ours lives in a local
  SQLite ledger (`extension_applied`), written by the native messaging host.
* **The file writer must additionally match on URL**, not only on marker/GUID, or it duplicates
  everything the extension already applied.
* **Key that ledger by `(client, source_id)`, not by `source_id` alone.** With a bare id key, the
  first browser to sync an item records it, and every other browser is told nothing is pending —
  so the item silently never arrives anywhere else. The worker has to report which browser it is.
* **Detect Brave with `"brave" in navigator`.** Brave's user agent deliberately claims Chrome, so
  UA sniffing gets this wrong by design.
* Make the client key degrade safely: a service worker still running older code sends no client
  and is stored under a sentinel (`unknown`), which costs one redundant "already present" and
  nothing else.

The mirror-image fact, verified here: `meta_info` written into the file **does** survive Brave's
own round-trip (7 of 7 nodes). So marker ownership is viable for a file writer, just not shareable
with an extension. Re-verify after major Brave upgrades — the whole ownership model rests on it.

---

## 10. Safari is not possible

Stated plainly so nobody repeats the search: **there is no supported or unsupported programmatic
route to add a bookmark to Safari on macOS 26.** Not "difficult" — absent. Every avenue was
checked and every one is closed:

| Route | Result |
|---|---|
| `browser.bookmarks` in a Safari Web Extension | Compile-gated out of WebKit trunk and **absent from all six shipped dyld subcaches** — the symbol is not in the binary you have |
| AppleScript | No bookmark class; the terminology will not compile (errors **-2741 / -2740**) |
| Shortcuts | No bookmark action exists |
| URL scheme | None |
| `safaridriver` | Exposes no bookmark endpoint |
| Declarative Device Management (`com.apple.configuration.safari.bookmarks`) | Exists, but requires MDM enrolment; and `profiles` can no longer install configuration profiles on macOS 26 |
| Direct plist write | Guarded by `CloudBookmarkDatabaseLockArbiter` against an iCloud-backed store that fans out to every Apple device |

That last row is the one to take seriously even if you find a way around the lock: Safari's
bookmark store is iCloud-backed, so a successful write behind Safari's back does not stay local —
it propagates to every device on the account.

The single escape hatch, for a human doing something deliberate: emit a
`NETSCAPE-Bookmark-file-1` HTML file and import it by hand via **Safari → File → Import From**.
Export first. Re-import de-duplication behaviour is **unknown** and was not tested. This is
acceptable as a rare, deliberate, user-initiated action and is not a foundation for automation.

One near-miss worth naming so you do not chase it: `add reading list item` **is** scriptable and
does not require Full Disk Access. But the Reading List is flat, effectively write-only, and its
items cannot be removed in bulk. It is not a bookmarks API wearing a hat. Do not build on it.

### How it was verified

Symbol search across the shipped dyld shared-cache subcaches for the WebExtensions bookmarks
API; attempting to compile AppleScript bookmark terminology and recording the compiler errors;
enumerating Shortcuts' available Safari actions; enumerating `safaridriver`'s endpoints; checking
whether `profiles` can still install a configuration profile on this OS version. Each result was
then put through an adversarial refutation pass — an explicit attempt to find a counter-example —
and all of them survived it.

---

## 11. Smaller findings

Collected because each one costs an hour to rediscover.

* **`chrome.alarms` clamps to a one-minute floor.** A `periodInMinutes` below 1 does not give you
  faster polling. Genuinely instant delivery needs a long-lived `chrome.runtime.connectNative`
  port so the host can push — which MV3 fights, because it kills idle workers and the port must be
  re-established. One minute is usually below the threshold anyone notices.
* **Unpacked extensions do not auto-reload on file change.** After editing the service worker you
  must bump `manifest.json`'s `version` **and** press reload on the extension card. Editing the
  file and wondering why nothing changed is the default experience.
* **`--load-extension` only applies to a browser you launch yourself.** Your real, everyday
  profile still needs a one-time manual Developer-mode → Load unpacked. There is no flag that
  installs into an already-running browser.
* **An unattended job must never depend on a fingerprint.** Two scheduled runs of this system
  degraded silently because a secrets CLI blocked on an interactive Touch ID prompt with nobody at
  the machine. The macOS login keychain is the better primary source: `security show-keychain-info`
  reports it as `no-timeout`, i.e. unlocked at login and never re-locked on a timer. Verify
  non-interactivity by stripping the interactive tool from `PATH` and running with an empty
  environment.
* **`security add-generic-password -w` does not read the value from stdin.** During testing it
  silently consumed the *next flag* as the password. Use `security -i`, which reads commands from
  stdin, so the secret never appears in `argv` where `ps` can see it.
* **A stored secret with a label line will silently double your HTTP calls.** A note whose body
  was a label line followed by the token, piped through `xargs`, produced two `curl`
  invocations — a malformed one that 401'd and a correct one that 200'd. Because the second
  succeeded, it *looked* like it worked. Normalise retrieved secrets to exactly the value.
* **Separator drift in a path-keyed column is a landmine.** Two runs of the same code wrote
  `Parent / Child` and `Parent/Child` into the same ledger column. Nothing broke, because every
  current reader keys on something else — but an exact `map[row["collection"]]` lookup added later
  would silently miss most of the library. Normalise on read
  (`" / ".join(p.strip() for p in s.split("/"))`) and treat the repair as an explicit data
  migration, not a silent fixup inside a nightly job.

---

## 12. How to verify these yourself

Every finding above was established the same way, and none of it required risking a real profile.

### The throwaway-profile method

```bash
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
SRC="$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
LAB=/tmp/brave-lab

# 1. Copy. Never experiment against the real profile.
rm -rf "$LAB"; mkdir -p "$LAB"
cp -R "$SRC/Default" "$LAB/Default"
cp "$LAB/Default/Bookmarks" /tmp/Bookmarks.before

# 2. Run your writer against the copy (see "parameterise your paths" below).

# 3. Launch Brave on the copy, with logging. Quit it from the menu when done —
#    the quit-time save is part of what you are testing, so do not SIGKILL it.
"$BRAVE" --user-data-dir="$LAB" --enable-logging=stderr --v=0 about:blank

# 4. Diff on normalised JSON.
diff <(python3 -m json.tool /tmp/Bookmarks.before) \
     <(python3 -m json.tool "$LAB/Default/Bookmarks")
```

### The rules that made it work

1. **Parameterise every path in your writer** so the identical code runs against the lab and
   against production — environment variables for the profile directory and the state directory.
   This is not just hygiene: it is what let a dry run against a copy catch a folder-resolution bug
   that would have built an entire **duplicate nested tree** beside the user's real folders,
   because a leading `Bookmarks Bar` path component was being treated as a folder to create rather
   than as the root to resolve into. That bug reached a live profile in exactly zero runs.

   ```bash
   T=/tmp/applytest; rm -rf $T; mkdir -p $T/profile $T/root
   cp "$SRC/Default/Bookmarks" $T/profile/
   # stage your input in $T/root, then:
   RAINDROP_SYNC_PROFILE=$T/profile RAINDROP_SYNC_ROOT=$T/root python3 src/apply_brave.py
   ```

2. **Diff the things that matter separately**, not just the file. Compare the *set* of GUIDs,
   the *set* of ids, the titles, and the checksum. A raw diff of a 1 MB JSON file after Brave
   reformats it tells you nothing; "0 GUIDs vanished, 0 ids changed" tells you everything.

3. **Force a serialise from memory** to prove your nodes reached the model rather than merely
   surviving on disk untouched. Load a tiny unpacked probe extension with the `bookmarks`
   permission that calls `chrome.bookmarks.search({})`, logs the counts, and creates one node.
   Quit, then diff. This is the step that upgrades "Brave did not reject my file" into "Brave
   loaded my nodes and rewrote them itself".

4. **`--enable-logging=stderr --v=0` is your only window** into extension and native-messaging
   plumbing. `launch_context.cc` lines are where native-messaging failures actually explain
   themselves.

5. **Read `Preferences` directly** for extension state — `location`, `disable_reasons`,
   `install_signature.ids`. The `brave://extensions` UI hides exactly the fields you need.

6. **Run the experiment twice**: once with the browser closed for the whole run, once with it
   running. The two answers differ (finding 4), and a design that only ever tested one of them is
   untested.

---

## 13. Verification status of every claim

Nothing here is asserted beyond what was actually established. This table is the honest ledger.

| # | Finding | Status |
|---|---|---|
| 1 | Checksum walk, title as UTF-16LE | **Verified both directions** — our implementation reproduces Brave's digest, and Brave accepts ours |
| 1a | Brave 152 does not read the checksum | From the Chromium CL that removed the read side; consistent with observed behaviour |
| 1b | Pre-M152 mismatch triggers `required_recovery` and renumbers every node | **From source only** — cannot be triggered on M152, which does not read the checksum |
| 2 | Native-messaging manifests are read from Chrome's directory | **Verified** — single-variable experiment, confirmed by the `launch_context.cc` log line |
| 3 | CRX install yields `DISABLE_NOT_VERIFIED` (256), and the flag is inherited by an unpacked reload of the same id | **Verified** — observed in `Preferences` and in the greyed-out toggle; escaped only by minting a new id |
| 4 | File read once at startup; whole tree re-serialised ~2.5 s after a change and at quit | **Verified** for load, survival and Brave's own rewrite (probe extension). The exact debounce constant is a Chromium internal — do not depend on the number |
| 5 | `.bak` is overwritten by your file on the first save of the next session; nothing ever reads it back | **From source, adopted as a constraint** — not deliberately reproduced |
| 6 | Malformed file → empty permanent nodes, UMA only, no dialog, no restore | **From source, designed against** — deliberately not reproduced on a populated profile |
| 7 | `lastUpdate` bumps on your own writes, so a watermark never settles | Established as a design constraint; the content-hash ledger has run cleanly against it |
| 7a | Hashing curated values re-flags items forever | **Verified the hard way** — 168 items (~28% of the library) re-flagged on the following run; fixed, then re-validated against 139 known-good rows with 0 mismatches, and a full sweep showing 0 drift and 0 orphans in either direction |
| 7b | Whether a tag-only edit advances `changedBookmarksDate` | **UNTESTED** — this project does a full read until it is confirmed |
| 7c | `/raindrop/{id}/suggest` is Pro-gated | Documented as plan-gated; the confirming request (expect 402/403 on a free plan) is **UNTESTED** here |
| 8 | Worker load and `onInstalled` race, producing duplicates | **Verified** — real duplicates observed, then eliminated by the promise chain |
| 9 | `chrome.bookmarks` exposes no custom metadata | **Verified** by API surface and by consequence (the file writer had to match on URL) |
| 9a | `meta_info` survives Brave's round-trip | **Verified**, 7 of 7 nodes |
| 10 | Safari has no programmatic bookmark route | **Verified across seven independent avenues**, then adversarially refuted and unchanged |

Two rules of thumb, if you take nothing else from this document. First: for the browser's
bookmark file, **your own verification is the only error surface that exists** — nothing will
tell you that you broke it. Second: for the sync ledger, **hash raw source values, never your own
cleaned-up ones**, or you will re-process your entire library every night and it will look like
the API's fault.
