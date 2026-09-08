# Setup

A complete, ordered install. Follow it top to bottom the first time; the steps depend on each
other (the extension id in step 3 is what step 4 allowlists, and what step 5 verifies).

Everything here was developed and verified on macOS 26 (Darwin 25.x) with Brave 152 and the
stock system Python (`/usr/bin/python3`). Where something has not been verified, it says so.

**Read `RESEARCH.md` before you deviate from any of this.** Several steps look arbitrary
and are not - the native-messaging directory in step 4 and the "never pack a CRX" rule in
step 5 each cost multiple failed rounds to discover, and both fail *silently* when you get
them wrong.

---

## What you end up with

| Piece | Lives at | Runs |
|---|---|---|
| Phase A - classification | `~/.claude/skills/raindrop-sync/` | on demand, as a Claude Code skill |
| Phase B - extension | `~/.raindrop-sync/extension/` (unpacked) | polls every 60 s while Brave is open |
| Phase B - native host | `~/.raindrop-sync/bin/native_host.py` | spawned by the extension, ~30 ms per tick |
| Phase B - fallback writer | `~/.raindrop-sync/bin/apply_brave.py` | launchd, every 15 min |
| State | `~/.raindrop-sync/state.db`, `taxonomy.json`, `desired.json` | |

Phase B is deliberately **two appliers, both live**. The extension applies into the running
browser instantly; the launchd writer edits Brave's `Bookmarks` file and lands on the next
Brave start. They coexist safely - the file writer no-ops on anything already present by URL.
Neither ever deletes.

You can stop after step 6 and skip the extension entirely. The functional cost is zero; you
lose only *instant* application, and the promotion gate typically passes a handful of items a
month.

---

## Step 0 - prerequisites

- macOS with Brave installed and run at least once (so the profile exists).
- `/usr/bin/python3` (system Python - the scripts are pure stdlib; see step 6 for why the
  system interpreter and not Homebrew's).
- `openssl` (system) for step 3.
- A Raindrop.io account. The free plan is sufficient. (Two Raindrop features are Pro-only and
  are **not** used: `/raindrop/{id}/suggest` classification and semantic search.)
- **Brave Sync must be off.** If bookmark sync is enabled, injected nodes trip
  `CorruptionReason::UNTRACKED_BOOKMARK`, Brave discards its sync metadata, re-merges, and
  uploads the injected folders to every device on the chain. The file writer hard-aborts when
  it sees top-level `sync_metadata`, so nothing will be damaged - but nothing will apply
  either, and it will keep aborting until you turn sync off.

Check before you start:

```bash
ls "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/Default/Bookmarks"
python3 - <<'EOF'
import json, os
p = os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Bookmarks")
d = json.load(open(p))
print("version:", d.get("version"))
print("roots:", sorted(d["roots"]))
print("sync_metadata present:", "sync_metadata" in d)
EOF
```

Expected: `version: 1`, roots including `bookmark_bar`, `other`, `synced`, and
`sync_metadata present: False`. Anything else means stop and read the preflight rules in
`RESEARCH.md`.

---

## Step 1 - clone and bootstrap

```bash
git clone <this-repo> ~/src/raindrop-brave-sync
cd ~/src/raindrop-brave-sync
./scripts/bootstrap.sh
```

`bootstrap.sh` creates the runtime tree, separate from the checkout. The checkout is code;
`~/.raindrop-sync/` is state, and nothing in it is meant to be committed:

```
~/.raindrop-sync/            0700 - everything below inherits this boundary
├── bin/                           apply_brave.py, native_host.py, get-token.sh, seed-token.sh
├── extension/                     manifest.json, sw.js  (loaded unpacked in step 5)
├── log/                           apply.out.log, apply.err.log, native_host.log
├── backups/                       Bookmarks.<ISO8601>.json + .bak copies, keep 10
├── state.db                       SQLite ledger: bookmarks, taxonomy, sync_runs, extension_applied
├── taxonomy.json                  your pinned taxonomy - created by the FIRST classification pass
├── collection-map.json            collection path → Raindrop id
├── desired.json                   staged promotions, written by Phase A, read by both appliers
└── .env                           0600, optional token fallback (step 2)
```

Two things worth knowing about this layout:

- **The mode matters.** `~/.raindrop-sync` is `0700` because `.env` and `key.pem` live in it.
- **Keep the runtime tree here, not in `~/Documents` or `~/Downloads`.** On the machine this
  was built on, both of those were TCC-denied to a shell, and a TCC denial under launchd
  surfaces as a bare `Operation not permitted` with no dialog at all. `~/.raindrop-sync` is
  not a protected path, and neither - verified - is the Brave profile directory.

Confirm:

```bash
ls -ld ~/.raindrop-sync            # expect: drwx------
ls ~/.raindrop-sync/bin
```

---

## Step 2 - get a Raindrop API token

1. Go to **app.raindrop.io → Settings → Integrations**.
2. **Create new app**, name it anything, accept.
3. Open the app you just created and click **Create test token**.

That test token authenticates as *you*, with no expiry and no scoping. Treat it like a
password.

### Do not let it reach a terminal

**A token that has been printed is a token that must be rotated.** Not "should" - must.
Anything you echo lands in at least three places you will forget about: your shell history,
your terminal's scrollback buffer, and - if you are debugging inside Claude Code or any other
agent - the conversation transcript, which is stored and may be replayed to a model later.
There is no way to un-print it. If you catch yourself having run `echo $RAINDROP_TOKEN` or
`cat .env`, go back to Settings → Integrations, delete the token, and create a new one.

The same applies to `ps`. Never pass a token as a command-line argument: argv is world-readable
to every process running as you. The helper scripts avoid this deliberately - `seed-token.sh`
writes to the keychain through `security -i`, which reads its command from stdin, and uses the
`printf` builtin rather than a subprocess to write `.env`.

### Store it

`seed-token.sh` does not prompt - it resolves a token from the chain below and copies it into
the keychain and `.env`. For a first install there is nothing to resolve yet, so hand it one
through the environment, reading it with `read -rs` so the paste is never echoed and never
enters your shell history:

```bash
read -rs RAINDROP_TOKEN            # paste, press Return; nothing is displayed
export RAINDROP_TOKEN
~/.raindrop-sync/bin/seed-token.sh
unset RAINDROP_TOKEN
```

(If you keep the token in 1Password, `seed-token.sh --from-op` re-reads it from there instead.
That triggers Touch ID, so only do it while you are at the machine - on rotation, for example.)

It writes the token to two places and then proves they agree, printing only an 8-character
SHA-256 fingerprint - one-way, not reversible, safe to show:

```
Token resolved (36 chars), fingerprint: 1a2b3c4d
Keychain : service 'raindrop-api' updated
Fallback : /Users/you/.raindrop-sync/.env (mode 600)

  keychain fingerprint : 1a2b3c4d
  .env     fingerprint : 1a2b3c4d
  resolver fingerprint : 1a2b3c4d
OK: all agree. Unattended runs resolve from the keychain, no Touch ID.
```

At read time, `bin/get-token.sh` resolves in this order and prints the token on stdout and
nothing else - always consume it through a pipe:

1. `$RAINDROP_TOKEN` - explicit override, for one-off runs
2. **login keychain** (service `raindrop-api`) - the primary source
3. `~/.raindrop-sync/.env`, mode 0600 - cleartext fallback
4. `op read` (1Password CLI) - last

The order is not arbitrary. The keychain is primary because `security show-keychain-info`
reports the login keychain as `no-timeout`: it unlocks at login and is never re-locked on a
timer, so an unattended job can read it. A password manager is *last* because two scheduled
runs died waiting on an interactive Touch ID prompt with nobody at the machine. **An
unattended job must never depend on a human fingerprint.**

`.env` is a deliberate cleartext fallback. Any process running as you can read it; the
directory is `0700` and the file `0600`, and that is the whole of its protection. If that
trade is not acceptable to you, delete `.env` and rely on the keychain alone.

If you keep the token in 1Password and read it at point of use, note that a note field can be
multi-line - a label line plus the token. A bare `op read` returns both lines, and piping that
through `xargs` into `curl` will run curl *twice*: the malformed first call 401s, the second
succeeds, and the whole thing looks like it worked. `get-token.sh` normalises to the single
36-character UUID line for exactly this reason.

---

## Step 3 - generate the extension signing key and derive the id

Skip this and step 4–5 if you are only installing the launchd fallback.

```bash
cd ~/.raindrop-sync
openssl genrsa -out key.pem 2048
chmod 600 key.pem
```

Now derive the two values that come from it. The extension id is the first 16 bytes of the
SHA-256 of the DER-encoded **public** key, rendered as hex with each nibble mapped `0-f` → `a-p`:

```bash
# 1. the id (32 chars, a-p only)
openssl rsa -in key.pem -pubout -outform DER 2>/dev/null \
  | openssl dgst -sha256 -binary \
  | xxd -p -c 32 | cut -c1-32 | tr '0-9a-f' 'a-p'

# 2. the base64 DER public key - goes into manifest.json as "key"
openssl rsa -in key.pem -pubout -outform DER 2>/dev/null | openssl base64 -A
```

The first command prints something shaped like `abcdefghijklmnopabcdefghijklmnop`. **Yours will
be different** - it is derived from a key you just generated, so no id in this repo is yours,
and none of the examples below are real. Write yours down; you need it twice more.

Put the base64 output into `extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "Raindrop Sync",
  "version": "1.3.0",
  "key": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A...<your base64 DER public key>...IDAQAB",
  "permissions": ["bookmarks", "nativeMessaging", "alarms", "storage"],
  "background": { "service_worker": "sw.js" }
}
```

### Why the key is pinned

Without a `key` in the manifest, Chromium derives an unpacked extension's id **from its
absolute install path**. That id is stable only as long as the folder never moves. The moment
you move `~/.raindrop-sync/extension` - reorganising, migrating to a new Mac, renaming a home
directory - the id changes, and the native-messaging manifest's `allowed_origins` (step 4) no
longer matches. The extension then loads fine and silently cannot reach its host: no error in
the UI, nothing in a console you can open, just nothing happening.

Pinning `key` makes the id a function of the keypair instead of the path. `key.pem` is the only
thing that can reproduce it. **Back it up, and never commit it.** If you lose it you must
regenerate, take a new id, and update `allowed_origins` in both host manifests.

---

## Step 4 - install the native messaging host manifest

The extension talks to `native_host.py` over stdio. Brave finds the host by reading a manifest
from a fixed directory - and this is the part that is not what you would guess:

> **Brave does not override Chromium's stock product path for native-messaging host lookup on
> macOS.** A manifest under
> `~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/` is
> **silently ignored**. It must live in **Chrome's** directory:
>
> ```
> ~/Library/Application Support/Google/Chrome/NativeMessagingHosts/
> ```

This was verified on a real machine and cost four failed test rounds. Google Chrome does not
need to be installed - the directory is a path convention, not evidence of Chrome. One caveat:
this was verified on a machine where that directory already existed, created by another vendor's
installer. If it is absent on yours, `bootstrap.sh` creates it, and that path is untested. See `RESEARCH.md` for the diagnosis and the log line that proves it.

Install into **both** directories - Chrome's because it works, Brave's because a future Brave
may honour its own path:

```bash
EXT_ID=$(openssl rsa -in ~/.raindrop-sync/key.pem -pubout -outform DER 2>/dev/null \
         | openssl dgst -sha256 -binary | xxd -p -c 32 | cut -c1-32 | tr '0-9a-f' 'a-p')

for D in "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" \
         "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"; do
  mkdir -p "$D"
  cat > "$D/com.raindrop_sync.host.json" <<EOF
{
  "name": "com.raindrop_sync.host",
  "description": "Raindrop Sync native host",
  "path": "$HOME/.raindrop-sync/bin/native_host.py",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://${EXT_ID}/"
  ]
}
EOF
done

chmod +x ~/.raindrop-sync/bin/native_host.py
```

Four rules the file must obey, all of them silent failures if broken:

- **The filename must equal the `name` field**, `com.raindrop_sync.host.json` ↔
  `"name": "com.raindrop_sync.host"`.
- **The same string must appear in `extension/sw.js`** as the `HOST` constant. Three places,
  one value.
- **`path` must be absolute** and the target executable (`chmod +x`, with a `#!` line).
- **`allowed_origins` must carry the trailing slash**: `chrome-extension://<id>/`.

`native_host.py` writes nothing to stdout except protocol frames. A stray `print()` corrupts
the length-prefixed stream and Brave drops the connection without saying why - all diagnostics
go to `~/.raindrop-sync/log/native_host.log`.

---

## Step 5 - load the extension unpacked

1. Open `brave://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** → select `~/.raindrop-sync/extension`.
4. **Read the id on the card and confirm it matches the id from step 3.** If it differs,
   `manifest.json` has no `key` (or the wrong one) and the id came from the install path - fix the manifest, remove the card, and load again.

### Never pack a CRX. Not once. Not to test.

Packing this extension into a CRX and installing it locally **permanently destroys that
extension id**, and the damage is not undoable by any means available to you.

What happens, verified: registering a locally signed CRX through `External Extensions/<id>.json`
*does* make Brave install it (location 2 = `EXTERNAL_PREF`) - and Brave then disables it with
`disable_reasons: [256]` = `DISABLE_NOT_VERIFIED`. That is not a toggle you can clear.
Chromium's InstallVerifier enforces it against the signed allowlist in
`Preferences → extensions.install_signature.ids`. A locally built CRX is not on that list, so
every attempt to re-enable is reverted, and the enable toggle greys out for good.

The part that makes it fatal rather than annoying: **re-loading the very same folder as
unpacked inherits the flag.** The load succeeds (`location: 4 = UNPACKED`), the card appears,
and the extension stays disabled and un-enableable. The flag follows the *id*, not the
installation.

The only escape is a new id: regenerate `key.pem`, update `manifest.json.key`, update
`allowed_origins` in **both** host manifests, Remove the dead card, and Load unpacked again.
The first identity built for this project was lost exactly this way.

Unpacked extensions are exempt from the install verifier. That is the supported route, and the
only one. `--load-extension` on the command line does not help either - it only applies to a
browser instance you launch yourself, not to your real profile.

### After any code change

Unpacked extensions **do not hot-reload**. After editing `sw.js`:

1. bump `manifest.json` `"version"`, then
2. click **reload** on the extension card in `brave://extensions`.

Skipping this is the single most common reason a fix "doesn't work" - you are still running
the old service worker.

---

## Step 6 - install the launchd fallback agent

This is the applier that works when Brave is closed or the extension is off. It edits Brave's
`Bookmarks` file directly, so its changes appear at the next Brave start.

Write `~/Library/LaunchAgents/com.raindrop-sync.apply.plist`, substituting your real home
directory for `/Users/you` (launchd does not expand `~` or `$HOME`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.raindrop-sync.apply</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/.raindrop-sync/bin/apply_brave.py</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/you/.raindrop-sync/log/apply.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/.raindrop-sync/log/apply.err.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityBackgroundIO</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
```

Load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.raindrop-sync.apply.plist
launchctl kickstart -p gui/$(id -u)/com.raindrop-sync.apply     # run once now
launchctl list | grep raindropsync
```

Expected last line - `-` (not currently running), exit status `0`:

```
-	0	com.raindrop-sync.apply
```

To unload later: `launchctl bootout gui/$(id -u)/com.raindrop-sync.apply`.

**Why `/usr/bin/python3` and not Homebrew.** The interpreter path must be absolute and stable.
`/opt/homebrew/bin/python3` is a symlink that moves on `brew upgrade`, which silently breaks the
agent. If you must use Homebrew, name the versioned path
(`/opt/homebrew/opt/python@3.x/bin/python3.x`), never the `bin` symlink. These scripts are pure
stdlib, so the system interpreter is strictly the more robust choice here.

**Every 15 minutes, and why it re-runs.** The agent does not wait for Brave to close. If every
marked node is already present it exits silently without writing. If not, it writes regardless
of Brave's state, then re-verifies on the next tick. Brave re-serialises its in-memory tree over
the file about 2.5 s after any bookmark change and again at quit, which can discard a write made
while it was running - so the retry is the mechanism, not a workaround. The write sticks on the
first quiet tick, and always survives once Brave restarts and reads the file. **Never kill Brave
to make a write land.**

Losing that race costs only *our* pending additions. Your own bookmarks live in Brave's
authoritative in-memory model and are never at risk from a concurrent write, because both
writers are atomic (`os.replace` against Chromium's `ImportantFileWriter` rename) - one version
wins whole and the file is never torn.

---

## Step 7 - run the first classification pass

Phase A is a Claude Code skill, invoked on demand:

```
/raindrop-sync
```

*(In the design there is also a daily `StartCalendarInterval` agent for Phase A. What is
described here - the on-demand skill - is what has actually been built and run. The scheduled
fetch agent is not installed.)*

The first pass is different from every pass after it, and it is worth understanding before you
run it:

**It builds your taxonomy from your library. Nothing is shipped.** This repo contains no
taxonomy. The first pass reads your bookmarks and derives a folder tree and tag vocabulary that
fit *them* - if your library is recipes and woodworking, you get Recipes and Woodworking, and
the examples in this documentation ("Research", "Recipes", "Woodworking") are invented
placeholders that will not appear in your install.

**After that first pass, the taxonomy is pinned.** Daily runs never re-derive it; they classify
new items *into* the existing tree, which is passed to the model in the prompt. This is the rule
that makes the bookmarks bar stable. Without it, the same bookmarks get regrouped under
plausibly-different names every night - every individual write correct, the bar permanently
churning. Re-deriving the taxonomy is a separate, explicit, user-invoked operation that bumps
`taxonomy_version` and re-classifies everything.

**The first pass classifies your whole library and will take a while.** Later passes classify
only the delta: an item is re-classified iff it is absent from the ledger (new), its
`content_hash` changed (edited), or `taxonomy_version` changed.

What the pass does:

1. Reads Raindrop and diffs against the ledger in `state.db`.
2. Classifies the delta into the pinned taxonomy.
3. Writes back to Raindrop - creates collections, applies tags. Raindrop is the source of
   truth. (Tag writes are read-modify-write: Raindrop's `tags` field *appends*, never replaces.)
4. Stages the small set of items that clear the promotion bar into `desired.json`, for the
   browser bar.

Then check the canary: the run logs `classified_count`. On a steady-state daily run that number
should be small - a handful. **If it is in the dozens or hundreds, stop and read the
troubleshooting entry on re-classification below.** That single number is the tell for nearly
every failure mode in the delta logic.

Note that `desired.json` holds *promotions to the browser bar*, which is a much narrower set
than "everything classified". The bar is curated; the promotion gate is deliberately strict, and
it is normal for a run to classify many items and promote none.

---

## Verify your install

Run these in order. Each one has an expected output.

**1. Runtime tree and permissions**

```bash
ls -ld ~/.raindrop-sync                  # drwx------
stat -f '%Lp' ~/.raindrop-sync/.env      # 600   (skip if you deleted .env)
stat -f '%Lp' ~/.raindrop-sync/key.pem   # 600   (extension installs only)
```

**2. The token resolves - without printing it**

```bash
~/.raindrop-sync/bin/get-token.sh | shasum -a 256 | cut -c1-8
```

Expected: 8 hex characters, matching the fingerprint `seed-token.sh` printed. If it prints
`get-token: no token from env, keychain, .env, or op`, go back to step 2.

**3. The token actually authenticates - without putting it in argv**

```bash
printf 'header = "Authorization: Bearer %s"\n' "$(~/.raindrop-sync/bin/get-token.sh)" \
  | curl --config - -s -o /dev/null -w '%{http_code}\n' https://api.raindrop.io/rest/v1/user
```

Expected: `200`. A `401` means the token is wrong or was truncated. The token goes to curl
through stdin, so it never appears in `ps` and never reaches your scrollback.

**4. The native host answers**

```bash
python3 -c 'import struct,sys; m=b"{\"op\":\"ping\"}"; sys.stdout.buffer.write(struct.pack("@I",len(m))+m)' \
  | ~/.raindrop-sync/bin/native_host.py | tail -c +5
```

Expected, exactly:

```
{"ok": true, "version": "1.0.0"}
```

This bypasses the browser entirely, so it isolates the host from the messaging plumbing. If
this works and the extension still does nothing, the problem is the manifest (step 4), not the
host.

**5. The extension is actually talking to it**

With Brave open and the extension loaded, wait about a minute, then:

```bash
tail -5 ~/.raindrop-sync/log/native_host.log
```

Expected shape - `host start`, a `pending` line, `stream closed`, roughly once a minute:

```
2026-01-01T12:00:00.000000+00:00 host start (pid 12345)
2026-01-01T12:00:00.002000+00:00 pending[brave]: 0 of 42 staged
2026-01-01T12:00:00.002300+00:00 stream closed
```

`pending[brave]` - not `pending[unknown]` - confirms the worker is reporting its client
correctly. `0 of N` means everything staged has already been applied; that is the healthy
steady state.

**6. The launchd agent is loaded and exiting clean**

```bash
launchctl list | grep raindropsync           # -	0	com.raindrop-sync.apply
tail -3 ~/.raindrop-sync/log/apply.out.log
cat ~/.raindrop-sync/log/apply.err.log       # expect empty
```

Expected in `apply.out.log`, one line per 15-minute tick - one of:

```
2026-01-01T12:00:00.000000+00:00 no desired.json; nothing to do
2026-01-01T12:00:00.000000+00:00 all 42 promotions already present and marked; no write needed
```

Exit codes: `0` ok or nothing to do · `1` hard failure, backup restored · `2` preflight abort.

**7. Dry-run the file writer against a copy - do this before trusting it**

Never let a first run touch the live file. Both applier paths honour environment overrides for
exactly this:

```bash
T=/tmp/applytest; rm -rf $T; mkdir -p $T/profile $T/root
cp "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/Default/Bookmarks" $T/profile/
cp ~/.raindrop-sync/desired.json $T/root/
RAINDROP_SYNC_PROFILE=$T/profile RAINDROP_SYNC_ROOT=$T/root \
  /usr/bin/python3 ~/.raindrop-sync/bin/apply_brave.py
```

Then inspect `$T/profile/Bookmarks` - confirm your promotions landed **inside your existing
folders**, not in a duplicate tree beside them. Keep this test. It is how a real bug was caught:
`find_folder` originally treated a leading `Bookmarks Bar` path component as a folder to
*create*, building a shadow tree next to the user's real one.

**8. The checksum implementation is right**

```bash
python3 - <<'EOF'
import json, hashlib, os
p = os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Bookmarks")
doc = json.load(open(p))
m = hashlib.md5()
u8  = lambda s: m.update(s.encode("utf-8"))
u16 = lambda s: m.update(s.encode("utf-16-le", "surrogatepass"))
def node(n):
    u8(n["id"]); u16(n.get("name","")); u8(n["type"])
    if n["type"] == "url": u8(n.get("url",""))
    else:
        for c in n.get("children", []): node(c)
for k in ("bookmark_bar","other","synced"): node(doc["roots"][k])
print("match:", m.hexdigest() == doc["checksum"])
EOF
```

Expected: `match: True`. Titles hash as **UTF-16LE**; every other field is UTF-8. That asymmetry
is the whole puzzle, and getting it wrong is how you write a file that looks fine and isn't.
(Brave 152 does not actually read this checksum - the read side was removed upstream - but write
it correctly anyway, and never add `checksum_sha256`.)

---

## Troubleshooting

### "Specified native messaging host not found"

**Symptom.** The extension loads, the card looks healthy, nothing is ever applied. The service
worker's `connectNative` fails with `Specified native messaging host not found.`

**Cause.** The host manifest is in the wrong directory. Almost always: you put it under
`BraveSoftware/Brave-Browser/NativeMessagingHosts/`, which seems obviously right and is
silently ignored - Brave does not override Chromium's stock product path on macOS.

**Fix.** Put the manifest in **Chrome's** directory (step 4):

```bash
ls -l "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/"
```

Then re-check the four silent-failure rules: filename equals the `name` field; that same name is
the `HOST` constant in `sw.js`; `path` is absolute and executable; `allowed_origins` has the
trailing slash and your real id.

The extension console is not reachable for a service worker in this situation, so to see the
actual error you need Brave's own log, from a throwaway profile:

```bash
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --user-data-dir=/tmp/lab --load-extension="$HOME/.raindrop-sync/extension" \
  --enable-logging=stderr --v=0 about:blank 2>&1 | grep -iE "native|raindrop"
```

The decisive line is `launch_context.cc:148 Can't find manifest for native messaging host`.

---

### Extension greyed out, enable toggle disabled

**Symptom.** The card shows the extension as disabled. Clicking the toggle does nothing, or it
flips back. `brave://extensions` gives no reason.

**Cause.** A CRX poisoned the id. Chromium flagged it as an unrecognised sideload
(`DISABLE_NOT_VERIFIED`, `disable_reasons: [256]`) and enforces that against the signed
allowlist in `Preferences → extensions.install_signature.ids`. Re-enabling is reverted every
time. Reloading the same folder unpacked inherits the flag, because the flag follows the **id**.

**Fix.** The id is dead. Take a new one:

1. `openssl genrsa -out ~/.raindrop-sync/key.pem 2048 && chmod 600 ~/.raindrop-sync/key.pem`
2. Derive the new id and base64 public key (step 3); update `manifest.json`'s `key`.
3. Update `allowed_origins` in **both** host manifests to the new id (step 4).
4. In `brave://extensions`, **Remove** the old card - do not just reload it.
5. **Load unpacked** again, and confirm the card shows the new id.

Do not attempt the CRX route again. See `RESEARCH.md`.

---

### Bookmarks appear, then vanish

**Symptom.** The apply log says `OK: N applied`, the bookmarks show up in the file, and after a
few seconds - or after quitting Brave - they are gone again.

**Cause.** The write happened while Brave was running. Brave's in-memory model is authoritative
and re-serialises over the file about 2.5 s after any bookmark change, and again at quit. Your
nodes were not in that in-memory tree, so the rewrite dropped them. The apply log flags it:

```
NOTE: Brave is running. It may overwrite this from memory; the next tick re-applies.
```

**Fix.** Nothing. This is expected and self-healing - the 15-minute agent re-verifies and
re-writes each tick, the write sticks on the first quiet tick, and it always survives once Brave
restarts and *reads* the file. If you want it to land immediately, that is what the extension is
for: it writes through the model instead of around it.

**Do not kill Brave**, and do not "fix" this by adding a Brave-is-running gate. Losing this race
costs only the pending additions, which the retry recovers.

---

### Nothing appears at all

**Symptom.** No errors anywhere, no bookmarks, both appliers apparently idle.

**Causes, in the order worth checking:**

1. **Nothing is staged.** `python3 -c 'import json;print(len(json.load(open("desired.json"))))'`
   from `~/.raindrop-sync`. The promotion gate is strict - classifying many items and promoting
   none is normal. Check `desired.json` before assuming a bug.
2. **The host log is silent.** `tail ~/.raindrop-sync/log/native_host.log`. No `host start` line
   in the last minute means the extension never invoked the host → previous entry.
3. **The extension is running old code.** Unpacked extensions do not hot-reload. If you edited
   `sw.js` and did not bump `manifest.json`'s `version` *and* click reload on the card, the old
   service worker is still live. This is the most common cause after a code change.
4. **The launchd agent never fired.** `launchctl list | grep raindropsync`. A missing line means
   it was never bootstrapped; a non-zero exit column means it ran and failed - read
   `apply.err.log`. A bare `Operation not permitted` there is a TCC denial, which under launchd
   produces no dialog at all.
5. **Preflight aborted (exit 2).** `apply.out.log` will name it: `ABORT: sync_metadata present`,
   `ABORT: unexpected version`, `ABORT: stored checksum does not match`, and so on. Each is a
   deliberate refusal, not a crash.

---

### Every item re-classifies on every run

**Symptom.** The `classified_count` canary is huge - dozens or hundreds every run, on a library
that barely changes. The LLM cost is real and it never settles.

**Cause.** Almost certainly the ledger is hashing **curated** values instead of **raw source**
values. This is a real bug that shipped and had to be fixed: run 1 wrote the *curated* title and
link into the ledger's `title`/`link` columns - emoji stripped, long blurbs shortened, `&amp;`
decoded, tracking parameters removed - and then computed `content_hash` from those. A hash of an
edited record cannot be reproduced from the source, so a large fraction of the library re-flagged
as EDITED on the next run, and would have done so forever.

The other route to the same symptom is a **timestamp watermark**. Raindrop bumps `lastUpdate` on
every write *including your own write-back*, so any `lastUpdate`-based watermark re-selects
everything, permanently.

**Fix.** Restore the invariant. Two columns that intentionally disagree:

| column | holds | used for |
|---|---|---|
| `content_hash` | `sha256(raw_link\|raw_title\|\|\|)` - verbatim Raindrop values | delta detection **only** |
| `title`, `link` | the curated display values | Brave naming, `desired.json`, reading |

Never "repair" these into agreement by recomputing the hash from the curated columns - that is
precisely how the bug is reintroduced. Recompute the affected hashes from raw source values and
re-validate against known-good rows before trusting the next run.

Two related rules: `classified_hash` is marked **only after** the item is committed to
`desired.json` (a crash then costs one redundant re-classification, never a silent skip); and the
`excerpt|note|type` slots in the hash format are deliberately always empty, because the MCP
surface the classifier uses does not return them. Widening the hash to include real excerpt/note
invalidates every hash at once and is a `taxonomy_version`-class change - an explicit,
user-invoked reclassification, never a silent upgrade.

---

### Duplicate bookmarks

**Symptom.** Two identical bookmarks, same title and URL, created within the same second.

**Cause.** The MV3 service-worker lifecycle. Worker load and `onInstalled` can fire in the same
millisecond; each independently saw the URL as absent and created it. This is not hypothetical - it produced a real duplicate during testing.

**Fix.** The sync must be serialized. `sw.js` funnels every sync through a single promise chain
(`serialize()`), so concurrent callers queue instead of racing. **Do not remove that lock**, and
do not add a second entry point that bypasses it.

There is a second duplication route between the two appliers: `chrome.bookmarks` cannot write the
file format's `meta_info` marker, so extension-created nodes are invisible to a GUID-only
ownership check and the file writer would happily re-create them. `apply_brave.py` therefore
matches on **URL as well as GUID**. Ownership is tracked differently on each side - the file
writer marks nodes `meta_info {"raindrop_sync":"v1"}`, the extension records its work in
`state.db → extension_applied`, keyed `(client, raindrop_id)`.

That `client` key matters if you run the extension in more than one browser: without it, the
first browser to sync records the item and every other browser is told nothing is pending, and
silently never receives it. Brave is detected via `"brave" in navigator`, because its user-agent
deliberately claims to be Chrome.

---

### Brave shows no bookmarks at all after a write

**Symptom.** Brave starts with an empty bookmarks bar and behaves like a fresh profile. No
dialog, no error, no warning.

**Cause.** A malformed `Bookmarks` file. On a parse failure, `version != 1`, or a missing root,
Chromium's model loader records a UMA metric and loads three empty permanent nodes. That is the
entire user-visible feedback. **This is the catastrophic failure mode of the whole design.**

Worse: Chromium has **no restore-from-`.bak` path anywhere**, and the first save of the new
session copies the broken file over `Bookmarks.bak` - destroying the last good copy. Brave's
`backup_triggered_` latch means `Bookmarks.bak` is *not* a safety net.

**Fix - quit Brave first, before it saves.**

```bash
# 1. Quit Brave completely (Cmd-Q). Confirm nothing is running:
pgrep -x "Brave Browser"        # expect no output

# 2. Pick the newest good backup:
ls -t ~/.raindrop-sync/backups/ | head

# 3. Confirm it parses BEFORE restoring:
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("version",d["version"],
  "roots",sorted(d["roots"]))' ~/.raindrop-sync/backups/Bookmarks.<TIMESTAMP>.json

# 4. Restore:
P="$HOME/Library/Application Support/BraveSoftware/Brave-Browser/Default"
cp ~/.raindrop-sync/backups/Bookmarks.<TIMESTAMP>.json "$P/Bookmarks"

# 5. Start Brave and confirm the bar is back.
```

This is why the writer takes its own timestamped backups of **both** `Bookmarks` and
`Bookmarks.bak` before every write, re-opens the copy to confirm it parses, writes atomically
(`mkstemp` on the same volume → fsync → `os.replace` → fsync the directory), then re-reads from
disk and verifies parse, checksum, id uniqueness and GUID uniqueness - restoring the backup and
exiting non-zero if any check fails. If you are modifying the writer, keep every one of those
steps.

---

## Notes on what is per-install, and what is not tested

**Per-install, never shared:** your Raindrop token, `key.pem`, your extension id, your Raindrop
collection ids, `taxonomy.json`, `collection-map.json`, `state.db`, `desired.json`. Every id and
key shown in this document is a placeholder of the right shape. None of them are real, and none
of them are yours.

**Not verified, as of this writing:**

- Raindrop's `/user/stats` → `meta.changedBookmarksDate` gate semantics. It is unconfirmed
  whether a tag-only edit advances that field, so Phase A currently does a full read every run
  rather than trusting a zero-call exit path. The design also calls for a max-staleness override - a forced full read after 7 days - because nothing documents which mutations advance it.
- `/raindrop/{id}/suggest` plan gating (expected to 402/403 on the free plan). Unused either way.
- `meta_info` marker survival is verified against Brave 152 only. **Re-verify it after a major
  Brave upgrade** - marker-based ownership is what makes it safe to place bookmarks into your
  own hand-curated folders, and it is the assumption most likely to be broken by an upstream
  change.

**Deletion propagation is off by default and additive-only.** A Raindrop item that is trashed,
or simply missed by a read race, looks identical to a hard delete. Turning deletion on would
require absence from N consecutive full reads, a re-query of the id, and a check of collection
`-99` (Trash) - none of which is built.

**Safari is not supported and cannot be.** Every route was investigated and refuted; see
`RESEARCH.md`. If you want your classified bookmarks in Safari, export a
`NETSCAPE-Bookmark-file-1` HTML file and import it by hand via Safari → File → Import From,
after exporting your existing bookmarks first. Re-import dedupe behaviour is unknown, so treat
that as a rare deliberate action, never a habit.
