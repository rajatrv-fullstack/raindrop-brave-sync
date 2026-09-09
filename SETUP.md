# Setup

A complete, ordered install. Follow it top to bottom the first time. Step 1 runs the installer,
which does nearly everything; the steps after it are the parts that cannot be automated: your
token, one click in Brave, the first classification run, and a schedule.

Everything here was developed and verified on macOS 26 (Darwin 25.x) with Brave 152 and the
stock system Python (`/usr/bin/python3`). Where something has not been verified, it says so.

**Read `RESEARCH.md` before you deviate from any of this.** Several things look arbitrary and
are not - the native-messaging directory the installer writes to and the "never pack a CRX"
rule in step 3 each cost multiple failed rounds to discover, and both fail *silently* when you
get them wrong.

---

## What you end up with

| Piece | Lives at | Runs |
|---|---|---|
| Phase A - classification | `~/.claude/skills/raindrop-sync/` | inside a Claude session: on demand, or on the schedule you create in step 5 |
| Phase B - extension | `~/.raindrop-sync/extension/` (unpacked) | polls every 60 s while Brave is open |
| Phase B - native host | `~/.raindrop-sync/bin/native_host` (a wrapper around `native_host.py`) | spawned by the extension, ~30 ms per tick |
| Phase B - fallback writer | `~/.raindrop-sync/bin/apply_brave.py` | launchd, every 15 min |
| Health check | `~/.raindrop-sync/bin/doctor.sh` | whenever you want to know what is wrong |
| State | `~/.raindrop-sync/state.db`, `config.json`, `taxonomy.json`, `desired.json` | |

Phase B is deliberately **two appliers, both live**. The extension applies into the running
browser instantly; the launchd writer edits Brave's `Bookmarks` file and lands on the next
Brave start. They coexist safely - the file writer no-ops on anything already present by URL.
Neither ever deletes.

You can skip step 3 and never load the extension. The functional cost is zero; you lose only
*instant* application, and the promotion gate typically passes a handful of items a month.

---

## Step 0 - prerequisites

- **macOS.** launchd, the login keychain and the profile paths below are all macOS-specific.
- **Brave** - stable, Beta or Nightly - launched at least once, so a profile exists. The
  installer looks under `~/Library/Application Support/BraveSoftware/` at `Brave-Browser`,
  `Brave-Browser-Beta` and `Brave-Browser-Nightly`, in that order, reads each channel's
  `Local State` for the profile it last used (`Default` if it names none), and takes the first
  one that has a `Bookmarks` file. If that is not the profile you want, set
  `RAINDROP_SYNC_PROFILE` when you run the installer (step 1).
- **Xcode Command Line Tools:** `xcode-select --install`. This is what provides `git` and a
  real `/usr/bin/python3` (3.9 or later). Without it, `/usr/bin/python3` is a stub that pops
  the install dialog, and the installer stops at its first check and tells you to run that
  command. The scripts are pure standard library: no venv, no pip.
- **Claude Code, with the Raindrop MCP connector.** Phase A is a Claude skill that reads and
  writes your library through the connector's tools (`find_bookmarks`, `find_collections`,
  `update_bookmarks`). The connector is added in Claude's connector settings (Settings >
  Connectors, in the Claude desktop app or on claude.ai), not by anything in this repository,
  and a headless `claude -p` run cannot see it: Phase A only works inside a Claude session.
  Check it before step 4: in a Claude session, ask it to call `fetch_current_user` on the
  Raindrop connector and expect your own account back. If the tool does not exist, the
  connector is not added.
- **A Raindrop.io account and a test token.** The free plan is sufficient. (Two Raindrop
  features are Pro-only and are **not** used: `/raindrop/{id}/suggest` classification and
  semantic search.) The token comes from app.raindrop.io > Settings > Integrations > For
  Developers > Create test token; step 2 stores it.
- **Brave Sync must be off** for the file writer. If bookmark sync is enabled, injected nodes
  trip `CorruptionReason::UNTRACKED_BOOKMARK`, Brave discards its sync metadata, re-merges, and
  uploads the injected folders to every device on the chain. The installer checks for this:
  when it finds a top-level `sync_metadata` key in your `Bookmarks` file it prints a loud
  warning, records `"brave_sync": true` in `config.json`, and does not install the launchd
  agent (it removes one that a previous install loaded). The extension path still works,
  because it goes through the bookmarks API rather than the file. The writer itself refuses
  too, with `ABORT: Brave Sync is enabled. External nodes would corrupt sync metadata and
  upload to every device. Disable bookmark sync or remove this job.`

Check before you start. This is the same detection the installer does, so what it prints is
what the installer will choose:

```bash
python3 - <<'EOF'
import json, os
base = os.path.expanduser("~/Library/Application Support/BraveSoftware")
found = None
for ch in ("Brave-Browser", "Brave-Browser-Beta", "Brave-Browser-Nightly"):
    last = "Default"
    try:
        with open(f"{base}/{ch}/Local State", encoding="utf-8") as f:
            last = json.load(f).get("profile", {}).get("last_used") or "Default"
    except (OSError, ValueError):
        pass
    p = f"{base}/{ch}/{last}/Bookmarks"
    print("checked:", p, "->", "found" if os.path.exists(p) else "missing")
    if found is None and os.path.exists(p):
        found = p
if not found:
    raise SystemExit("no Bookmarks file: launch Brave once, or set RAINDROP_SYNC_PROFILE")
with open(found, encoding="utf-8") as f:
    d = json.load(f)
print("profile:", os.path.dirname(found))
print("version:", d.get("version"))
print("roots:", sorted(d["roots"]))
print("sync_metadata present:", "sync_metadata" in d)
EOF
```

Expected: one `checked:` line per channel, a `profile:` line, `version: 1`, roots including
`bookmark_bar`, `other` and `synced`, and `sync_metadata present: False`. `no Bookmarks file`
means launch Brave once (or set `RAINDROP_SYNC_PROFILE`). `sync_metadata present: True` means
read the preflight rules in `RESEARCH.md` before going on.

---

## Step 1 - clone and bootstrap

```bash
git clone https://github.com/rajatrv-fullstack/raindrop-brave-sync.git ~/src/raindrop-brave-sync
cd ~/src/raindrop-brave-sync
./scripts/bootstrap.sh
```

`bootstrap.sh` needs no sudo and prompts for nothing. It is idempotent: re-running it
re-installs the scripts and re-renders the manifests without touching your key, your token or
your state. What it does, in order:

1. **Probes Python.** Runs `"$PY" -c 'import sys; assert sys.version_info >= (3, 9)'` before
   creating anything. On failure it prints `Install the Xcode Command Line Tools: xcode-select
   --install` and exits 1.
2. **Resolves your Brave profile** exactly as in step 0, or takes `RAINDROP_SYNC_PROFILE` if it
   is set. If no `Bookmarks` file is found it prints what it checked, says `launch Brave once,
   or set RAINDROP_SYNC_PROFILE=<profile dir>`, and exits 1.
3. **Checks Brave Sync** on that file. `sync_metadata` present means a loud warning,
   `"brave_sync": true` in `config.json`, and no launchd agent.
4. **Creates the runtime tree** `~/.raindrop-sync/` (mode `0700`) with `bin/`, `extension/`,
   `backups/` and `log/`.
5. **Installs the scripts** into `bin/`: `apply_brave.py`, `native_host.py`, `get-token.sh`,
   `seed-token.sh`, `doctor.sh`, and `native_host`, a three-line `/bin/sh` wrapper that exports
   `RAINDROP_SYNC_ROOT` and execs the pinned interpreter on `native_host.py`. The
   native-messaging manifest points at the wrapper, never at the `.py`, so the host always
   knows where its state lives regardless of the environment Brave spawns it with.
6. **Writes `.env`** containing `RAINDROP_TOKEN=` (empty, mode `0600`) if there is none. Step 2
   fills it or, preferably, uses the keychain instead.
7. **Generates `key.pem`** (RSA 2048, mode `0600`) if absent, derives your extension id from it,
   and writes `extension/manifest.json` with the public key pinned in, `extension/sw.js`, and
   `.extension-identity.json`. An existing `key.pem` is reused, so your id is stable across
   re-runs.
8. **Writes the native-messaging host manifest** `com.raindrop_sync.host.json` into both
   `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` (the one Brave actually
   reads; `RESEARCH.md` finding 2) and
   `~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/`, with your
   id in `allowed_origins` and `path` set to the wrapper.
9. **Creates `state.db`** by running `native_host.py --init-db`, which creates the four ledger
   tables: `bookmarks`, `meta`, `runs`, `extension_applied`.
10. **Writes `config.json`**: the resolved root, interpreter, profile, extension id, Brave Sync
    flag and install time. `apply_brave.py` reads the profile from here whenever
    `RAINDROP_SYNC_PROFILE` is not set.
11. **Installs the launchd agent** `com.raindrop-sync.apply`. The plist is rendered in Python
    (not `sed`, so a home directory with unusual characters survives), its
    `EnvironmentVariables` carry `PATH`, `RAINDROP_SYNC_ROOT` and `RAINDROP_SYNC_PROFILE`, and
    it is loaded with `launchctl bootout gui/$(id -u)/com.raindrop-sync.apply || true` followed
    by `launchctl bootstrap gui/$(id -u) <plist>`. Skipped, with a note, when Brave Sync is on
    or `SKIP_LAUNCHCTL=1` is set.
12. **Installs the Claude skill** to `~/.claude/skills/raindrop-sync/SKILL.md`, so
    `/raindrop-sync` exists in your next Claude session.

It ends by printing your extension id and the things left to do by hand.

### The runtime tree

The checkout is code; `~/.raindrop-sync/` is state, and nothing in it is meant to be committed:

```
~/.raindrop-sync/            0700 - everything below inherits this boundary
├── bin/                           apply_brave.py, native_host.py, native_host (wrapper),
│                                  get-token.sh, seed-token.sh, doctor.sh
├── extension/                     manifest.json (key pinned in), sw.js  (loaded unpacked in step 3)
├── log/                           apply.out.log, apply.err.log, native_host.log  (+ .1 after rotation)
├── backups/                       Bookmarks.<stamp> and Bookmarks.bak.<stamp>, keep 10 of each
├── config.json                    root, python, profile, extension_id, brave_sync, installed_at
├── state.db                       SQLite ledger: bookmarks, meta, runs, extension_applied
├── key.pem                        0600 - the extension signing key; back it up, never commit it
├── .extension-identity.json       {"id": "<your extension id>"}
├── taxonomy.json                  your pinned taxonomy - created by the FIRST classification pass
├── collection-map.json            collection path -> Raindrop id, created with it
├── desired.json                   staged promotions, written by Phase A, read by both appliers
└── .env                           0600, RAINDROP_TOKEN= ; a non-empty value here wins over the keychain
```

Two things worth knowing about this layout:

- **The mode matters.** `~/.raindrop-sync` is `0700` because `.env` and `key.pem` live in it.
- **Keep the runtime tree here, not in `~/Documents` or `~/Downloads`.** On the machine this
  was built on, both of those were TCC-denied to a shell, and a TCC denial under launchd
  surfaces as a bare `Operation not permitted` with no dialog at all. `~/.raindrop-sync` is
  not a protected path, and neither - verified - is the Brave profile directory.

### Environment knobs

Every path the installer, the doctor and the two appliers touch can be redirected. A normal
install needs none of these; they exist for unusual profiles, for tests, and for the sandbox in
`CONTRIBUTING.md`.

| Variable | Default | Read by |
|---|---|---|
| `RAINDROP_SYNC_ROOT` | `~/.raindrop-sync` | everything |
| `RAINDROP_SYNC_PROFILE` | detected (step 0), then recorded in `config.json` | bootstrap, doctor, `apply_brave.py` |
| `RAINDROP_SYNC_PYTHON` | `/usr/bin/python3` | bootstrap; pinned into the wrapper and the plist |
| `RAINDROP_SYNC_NMH_CHROME` | `~/Library/Application Support/Google/Chrome/NativeMessagingHosts` | bootstrap, doctor |
| `RAINDROP_SYNC_NMH_BRAVE` | `~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts` | bootstrap, doctor |
| `RAINDROP_SYNC_LAUNCHAGENTS` | `~/Library/LaunchAgents` | bootstrap, doctor |
| `RAINDROP_SYNC_SKILLS` | `~/.claude/skills` | bootstrap |
| `SKIP_LAUNCHCTL=1` | unset | bootstrap writes the plist but does not load it; doctor skips the launchd check |
| `RAINDROP_SYNC_SKIP_PROFILE_CHECK=1` | unset | bootstrap does not require a Brave profile (`profile` is `null` in `config.json`); doctor reports a missing profile as `warn`, not `FAIL` |
| `RAINDROP_SYNC_NO_KEYCHAIN=1` | unset | `get-token.sh` never calls `security` |
| `RAINDROP_SYNC_KEYCHAIN_SERVICE` | `raindrop-api` | `get-token.sh`, `seed-token.sh` |

`RAINDROP_SYNC_PROFILE` is the absolute path of the profile directory that contains the
`Bookmarks` file, for example
`"$HOME/Library/Application Support/BraveSoftware/Brave-Browser-Beta/Profile 1"`. The installer
records it in `config.json` and in the launchd plist, so you set it once, at install time:

```bash
RAINDROP_SYNC_PROFILE="$HOME/Library/Application Support/BraveSoftware/Brave-Browser-Beta/Profile 1" \
  ./scripts/bootstrap.sh
```

Confirm:

```bash
ls -ld ~/.raindrop-sync            # expect: drwx------
~/.raindrop-sync/bin/doctor.sh     # expect: ok lines, plus warn lines for what steps 2 to 4 have not done yet
```

---

## Step 2 - get a Raindrop API token

1. Go to **app.raindrop.io > Settings > Integrations > For Developers**.
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
`cat .env`, go back to Settings > Integrations, delete the token, and create a new one.

The same applies to `ps`. Never pass a token as a command-line argument: argv is world-readable
to every process running as you. The helper scripts avoid this deliberately: `seed-token.sh`
reads the token from a hidden prompt or from the environment, never from an argument, and
every example in this repository feeds `curl` through `--config -` on stdin rather than `-H`.

### Store it

`seed-token.sh` stores the token in your login keychain under the service name `raindrop-api`
(`RAINDROP_SYNC_KEYCHAIN_SERVICE` changes it; set it for `get-token.sh` too). Run it with no
arguments and paste at the hidden prompt, or hand it the token through the environment for a
scripted install:

```bash
~/.raindrop-sync/bin/seed-token.sh                 # prompts; input is not echoed
```

```bash
read -rs RAINDROP_TOKEN                            # paste, press Return; nothing is displayed
export RAINDROP_TOKEN
~/.raindrop-sync/bin/seed-token.sh --from-env
unset RAINDROP_TOKEN
```

It prints only a confirmation, never the value. It calls `security add-generic-password -U`,
and `-U` **replaces** an existing item with the same service name: re-running it with a wrong
value overwrites a good one, and the only undo is running it again with the right value.

The `.env` file is an alternative, not a second copy. Bootstrap left `RAINDROP_TOKEN=` empty in
`~/.raindrop-sync/.env` (mode `0600`); put the value there if you prefer a file, and skip the
keychain.

At read time `bin/get-token.sh` resolves in this order and prints the token on stdout and
nothing else, so always consume it through a pipe:

1. `$RAINDROP_TOKEN`, an explicit override for one-off runs
2. `~/.raindrop-sync/.env`, when the `RAINDROP_TOKEN=` line there is non-empty
3. the **login keychain**, service `raindrop-api`, unless `RAINDROP_SYNC_NO_KEYCHAIN=1`

A non-empty `.env` therefore **wins over the keychain**. If you seed a fresh token into the
keychain but leave a stale value in `.env`, you keep getting the stale one and nothing tells
you why: clear the line back to `RAINDROP_TOKEN=` or delete the file.

The login keychain is the right home for unattended runs: `security show-keychain-info` reports
it as `no-timeout`, so it unlocks at login and is never re-locked on a timer, and a launchd job
can read it without a prompt. A password manager that requires Touch ID is the wrong home for
the same reason: an unattended job must never depend on a human being at the machine. If you
keep the token in a password manager, copy it into the keychain with `seed-token.sh --from-env`
rather than reading it at point of use.

`.env` is cleartext. Any process running as you can read it; the directory is `0700` and the
file `0600`, and that is the whole of its protection. If that trade is not acceptable, use the
keychain alone.

---

## Step 3 - load the extension unpacked

Skip this step if you are happy with the launchd writer alone (changes land at the next Brave
start instead of within a minute).

1. Open `brave://extensions`.
2. Turn on **Developer mode** (top right).
3. **Load unpacked** > select `~/.raindrop-sync/extension`.
4. **Read the id on the card and confirm it matches the id bootstrap printed**, which is also
   in `~/.raindrop-sync/.extension-identity.json`. If it differs, `manifest.json` has no `key`
   (or the wrong one) and the id came from the install path: re-run `./scripts/bootstrap.sh`,
   which re-pins the key, then remove the card and load again.

Then confirm the extension reached the host. Wait about a minute and run
`~/.raindrop-sync/bin/doctor.sh`: the line about the extension having contacted the host goes
from `warn` to `ok`. Or look directly: `tail -3 ~/.raindrop-sync/log/native_host.log` should
show a `pending[brave]` line.

### Why the key is pinned

Without a `key` in the manifest, Chromium derives an unpacked extension's id **from its
absolute install path**. That id is stable only as long as the folder never moves. The moment
you move `~/.raindrop-sync/extension` - reorganising, migrating to a new Mac, renaming a home
directory - the id changes, and the native-messaging manifest's `allowed_origins` no longer
matches. The extension then loads fine and silently cannot reach its host: no error in the UI,
nothing in a console you can open, just nothing happening.

Pinning `key` makes the id a function of the keypair instead of the path. `key.pem` is the only
thing that can reproduce it. **Back it up, and never commit it.** If you lose it you must take
a new id: delete `key.pem`, re-run bootstrap (which mints a key, re-pins it, and rewrites both
host manifests), remove the old card, and load unpacked again.

### Never pack a CRX. Not once. Not to test.

Packing this extension into a CRX and installing it locally **permanently destroys that
extension id**, and the damage is not undoable by any means available to you.

What happens, verified: registering a locally signed CRX through `External Extensions/<id>.json`
*does* make Brave install it (location 2 = `EXTERNAL_PREF`) - and Brave then disables it with
`disable_reasons: [256]` = `DISABLE_NOT_VERIFIED`. That is not a toggle you can clear.
Chromium's InstallVerifier enforces it against the signed allowlist in
`Preferences > extensions.install_signature.ids`. A locally built CRX is not on that list, so
every attempt to re-enable is reverted, and the enable toggle greys out for good.

The part that makes it fatal rather than annoying: **re-loading the very same folder as
unpacked inherits the flag.** The load succeeds (`location: 4 = UNPACKED`), the card appears,
and the extension stays disabled and un-enableable. The flag follows the *id*, not the
installation.

The only escape is a new id: delete `key.pem`, re-run bootstrap, Remove the dead card, and Load
unpacked again. The first identity built for this project was lost exactly this way.

Unpacked extensions are exempt from the install verifier. That is the supported route, and the
only one. `--load-extension` on the command line does not help either - it only applies to a
browser instance you launch yourself, not to your real profile.

### After any code change

Unpacked extensions **do not hot-reload**. After editing `sw.js` in the checkout:

1. bump `"version"` in the checkout's `extension/manifest.json`,
2. re-run `./scripts/bootstrap.sh`, which copies both files into `~/.raindrop-sync/extension`
   and re-pins the key, then
3. click **reload** on the extension card in `brave://extensions`.

Skipping this is the single most common reason a fix "doesn't work" - you are still running
the old service worker.

---

## Step 4 - run the first classification pass

Phase A is a Claude Code skill, invoked inside a Claude session:

```
/raindrop-sync
```

Do the connector check from step 0 first if you have not already; the skill has nothing to
read from without it.

The first pass is different from every pass after it, and it is worth understanding before you
run it:

**It builds your taxonomy from your library. Nothing is shipped.** This repo contains no
taxonomy. On the first run there is no `taxonomy.json`, so the skill takes the collections you
already have in Raindrop (`find_collections`) as the pinned taxonomy, writes `taxonomy.json` and
`collection-map.json`, and records `taxonomy_version = 1` in the ledger's `meta` table. If your
library is Recipes and Gardening, you get Recipes and Gardening; the examples in this
documentation ("Research", "Recipes", "Gardening") are invented placeholders that will not
appear in your install.

**After that first pass, the taxonomy is pinned.** Later runs never re-derive it; they classify
new items *into* the existing tree, which is passed to the model in the prompt. This is the
rule that makes the bookmarks bar stable. Without it, the same bookmarks get regrouped under
plausibly-different names on every run - every individual write correct, the bar permanently
churning. Re-deriving the taxonomy is a separate, explicit, user-invoked operation that bumps
`taxonomy_version` and re-classifies everything.

**The first pass classifies your whole library and will take a while.** Later passes classify
only the delta: an item is re-classified iff it is absent from the ledger (new), its
`content_hash` changed (edited), or `taxonomy_version` changed.

**The ledger already exists.** Bootstrap created `state.db` and its four tables with
`native_host.py --init-db`; the skill never creates tables. If a table is missing, re-run
`./scripts/bootstrap.sh`.

What the pass does:

1. Reads Raindrop and diffs against the ledger in `state.db`.
2. Classifies the delta into the pinned taxonomy.
3. Writes back to Raindrop - creates collections, applies tags. Raindrop is the source of
   truth. (Tag writes are read-modify-write: Raindrop's `tags` field *appends*, never replaces.)
4. Stages the small set of items that clear the promotion bar into `desired.json`, for the
   browser bar. Each entry is `{raindrop_id, name, url, folder_path}` with a flat
   `folder_path` of `Raindrop/<Top Level Collection>`.

Then check the canary: the run logs `classified_count`. On a steady-state run that number
should be small - a handful. **If it is in the dozens or hundreds, stop and read the
troubleshooting entry on re-classification below.** That single number is the tell for nearly
every failure mode in the delta logic.

Note that `desired.json` holds *promotions to the browser bar*, which is a much narrower set
than "everything classified". The bar is curated; the promotion gate is deliberately strict, and
it is normal for a run to classify many items and promote none.

---

## Step 5 - make it daily

Nothing in this repository schedules Phase A. There is no launchd agent for it, and there
cannot be one that works: the skill reads Raindrop through a Claude connector, and a connector
only exists inside a Claude session, so a headless `claude -p` run started by launchd or cron
would find no `find_bookmarks` tool. Phase B (the extension and the file writer) already runs
unattended; it just has nothing new to apply until Phase A runs.

To get the daily behaviour the rest of this documentation assumes, create a scheduled task in
the Claude desktop app whose prompt is `/raindrop-sync`. The simplest way is to ask Claude, in
the app, to schedule it; the example schedule used throughout is

```
0 9 * * *
```

in cron syntax, which is 09:00 every day. The task runs in a Claude session, so the connector is
available, and Phase B picks up the resulting `desired.json` within a minute (extension) or at
the next Brave start (file writer). Until you create that task, classification happens only
when you type `/raindrop-sync` yourself.

---

## Verify your install

**1. Run the doctor**

```bash
~/.raindrop-sync/bin/doctor.sh; echo "exit $?"
```

One line per check, each prefixed `ok`, `warn` or `FAIL`; the exit status is 0 iff nothing
failed. It honours every knob from step 1, and the same script runs from the checkout as
`scripts/doctor.sh`. It never prints the token: the token line reads `found (N bytes)` or
`missing`. What it checks:

- the interpreter runs and is 3.9 or newer;
- the runtime tree is laid out as above and `key.pem` is mode `0600`;
- `extension/manifest.json` has a `key`, and the id derived from it equals `extension_id` in
  `config.json`;
- both native-messaging manifests parse, are named `com.raindrop_sync.host`, point at a `path`
  that exists and is executable, and list your id in `allowed_origins`;
- the host answers a `ping` through the wrapper;
- the profile directory and its `Bookmarks` file exist (a `warn` rather than a `FAIL` when
  `RAINDROP_SYNC_SKIP_PROFILE_CHECK=1`), and whether Brave Sync is on;
- the launchd agent is loaded, via `launchctl print gui/$(id -u)/com.raindrop-sync.apply`
  (skipped with a note under `SKIP_LAUNCHCTL=1`; expected absent when `brave_sync` is true);
- the token resolves;
- `desired.json` parses, if present;
- `state.db` has all four tables;
- the extension has contacted the host at least once (rows in `extension_applied`, or a
  `pending[` line in `log/native_host.log`). Until it has, this is a `warn` telling you to load
  it unpacked (step 3).

`warn` lines are for things not done yet or deliberately off. A `FAIL` names the file or check
that is wrong; the troubleshooting section below is organised by symptom.

The manual checks follow. Each one has an expected output.

**2. Runtime tree and permissions**

```bash
ls -ld ~/.raindrop-sync                  # drwx------
stat -f '%Lp' ~/.raindrop-sync/.env      # 600   (skip if you deleted .env)
stat -f '%Lp' ~/.raindrop-sync/key.pem   # 600
cat ~/.raindrop-sync/config.json         # profile is the directory you expect; brave_sync false
```

**3. The token resolves - without printing it**

```bash
~/.raindrop-sync/bin/get-token.sh | shasum -a 256 | cut -c1-8
```

Expected: 8 hex characters and nothing else. If it prints a `No Raindrop token found` line on
stderr instead, go back to step 2.

**4. The token actually authenticates - without putting it in argv**

```bash
printf 'header = "Authorization: Bearer %s"\n' "$(~/.raindrop-sync/bin/get-token.sh)" \
  | curl --config - -s -o /dev/null -w '%{http_code}\n' https://api.raindrop.io/rest/v1/user
```

Expected: `200`. A `401` means the token is wrong or was truncated. The token goes to curl
through stdin, so it never appears in `ps` and never reaches your scrollback.

**5. The native host answers, through the wrapper**

```bash
python3 -c 'import struct,sys; m=b"{\"op\":\"ping\"}"; sys.stdout.buffer.write(struct.pack("@I",len(m))+m)' \
  | ~/.raindrop-sync/bin/native_host | tail -c +5
```

Expected, exactly:

```
{"ok": true, "version": "<VERSION>"}
```

where `<VERSION>` is whatever `VERSION` in `src/native_host.py` says for the checkout you
installed from: `grep -m1 '^VERSION' ~/src/raindrop-brave-sync/src/native_host.py`. This
bypasses the browser entirely and goes through the same wrapper the manifest names, so it
isolates the host from the messaging plumbing. If this works and the extension still does
nothing, the problem is the manifest, not the host.

**6. The extension is actually talking to it**

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

**7. The launchd agent is loaded and exiting clean**

```bash
launchctl print gui/$(id -u)/com.raindrop-sync.apply
launchctl list | grep raindrop-sync           # -	0	com.raindrop-sync.apply
tail -3 ~/.raindrop-sync/log/apply.out.log
cat ~/.raindrop-sync/log/apply.err.log       # expect empty
```

`launchctl print` shows a block describing the job when it is loaded; an error saying the
service could not be found in the domain means it is not. The `list` line is `-` (not
currently running), exit status `0`, then the label. Expected in `apply.out.log`, one line per
15-minute tick - one of:

```
2026-01-01T12:00:00.000000+00:00 no desired.json; nothing to do
2026-01-01T12:00:00.000000+00:00 all 42 promotions already present and marked; no write needed
```

Exit codes: `0` ok or nothing to do · `1` hard failure, backup restored · `2` preflight abort.
If `config.json` says `"brave_sync": true`, the agent is expected to be absent and the doctor
says so.

**8. Dry-run the file writer against a copy - do this before trusting it**

Never let a first run touch the live file. Both applier paths honour environment overrides for
exactly this. Read the profile from `config.json` rather than assuming `Brave-Browser/Default`:

```bash
P="$(python3 -c 'import json,os; r=os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync")); print(json.load(open(r + "/config.json"))["profile"])')"
T=/tmp/applytest; rm -rf $T; mkdir -p $T/profile $T/root
cp "$P/Bookmarks" $T/profile/
cp ~/.raindrop-sync/desired.json $T/root/
RAINDROP_SYNC_PROFILE=$T/profile RAINDROP_SYNC_ROOT=$T/root \
  /usr/bin/python3 ~/.raindrop-sync/bin/apply_brave.py
```

Then inspect `$T/profile/Bookmarks` - confirm your promotions landed **inside your existing
folders**, not in a duplicate tree beside them. Keep this test. It is how a real bug was caught:
`find_folder` originally treated a leading `Bookmarks Bar` path component as a folder to
*create*, building a shadow tree next to the user's real one.

**9. The checksum implementation is right**

```bash
P="$(python3 -c 'import json,os; r=os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync")); print(json.load(open(r + "/config.json"))["profile"])')"
python3 - "$P/Bookmarks" <<'EOF'
import json, hashlib, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
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

### bootstrap stops before installing anything

**`Install the Xcode Command Line Tools: xcode-select --install`.** The interpreter probe
failed: `/usr/bin/python3` is the stub, or `RAINDROP_SYNC_PYTHON` points at something older
than 3.9. Run the command it names, wait for the install to finish, and re-run bootstrap.
Nothing was created.

**`launch Brave once, or set RAINDROP_SYNC_PROFILE=<profile dir>`**, after a list of the paths
it checked. No `Bookmarks` file was found in any channel's last-used profile. Either Brave has
never been launched (launch it, quit it, re-run), or the profile you use is not the one
`Local State` names as last used: pass its directory explicitly, as in step 1.

**A warning that Brave Sync is on.** Bootstrap finished, but did not install the launchd
agent, and `config.json` says `"brave_sync": true`. The extension (step 3) still applies
through the bookmarks API. To get the file writer as well, turn off bookmark sync in Brave and
re-run bootstrap.

---

### "Specified native messaging host not found"

**Symptom.** The extension loads, the card looks healthy, nothing is ever applied. The service
worker's `sendNativeMessage` fails with `Specified native messaging host not found.`

**Cause.** The host manifest is in the wrong directory, or names the wrong path. Almost always:
it is only under `BraveSoftware/Brave-Browser/NativeMessagingHosts/`, which seems obviously
right and is silently ignored - Brave does not override Chromium's stock product path on macOS.

**Fix.** Run the doctor; it checks both manifests. By hand, confirm the manifest is in
**Chrome's** directory:

```bash
ls -l "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/"
```

Then re-check the four silent-failure rules: the filename equals the `name` field; that same
name is the `HOST` constant in `sw.js`; `path` is absolute and executable (it must be the
`~/.raindrop-sync/bin/native_host` wrapper, not the `.py`); `allowed_origins` has the trailing
slash and your real id. Re-running bootstrap rewrites both manifests correctly.

The extension console is not reachable for a service worker in this situation, so to see the
actual error you need Brave's own log, from a throwaway profile:

```bash
"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" \
  --user-data-dir=/tmp/lab --load-extension="$HOME/.raindrop-sync/extension" \
  --enable-logging=stderr --v=0 about:blank 2>&1 | grep -iE "native|raindrop"
```

The decisive line is `launch_context.cc:148 Can't find manifest for native messaging host`.

When the host is unreachable the extension backs off: it records the error and the time of the
next attempt in `chrome.storage.local` and doubles the wait from one minute up to fifteen, so
after a fix expect up to fifteen minutes before it retries, or click reload on the card.

---

### Extension greyed out, enable toggle disabled

**Symptom.** The card shows the extension as disabled. Clicking the toggle does nothing, or it
flips back. `brave://extensions` gives no reason.

**Cause.** A CRX poisoned the id. Chromium flagged it as an unrecognised sideload
(`DISABLE_NOT_VERIFIED`, `disable_reasons: [256]`) and enforces that against the signed
allowlist in `Preferences > extensions.install_signature.ids`. Re-enabling is reverted every
time. Reloading the same folder unpacked inherits the flag, because the flag follows the **id**.

**Fix.** The id is dead. Take a new one:

1. `rm ~/.raindrop-sync/key.pem`
2. `./scripts/bootstrap.sh` from the checkout. It mints a new key, prints the new id, re-pins
   the key into `manifest.json`, and rewrites `allowed_origins` in **both** host manifests.
3. In `brave://extensions`, **Remove** the old card - do not just reload it.
4. **Load unpacked** again, and confirm the card shows the new id.

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

(The check recognises `Brave Browser`, `Brave Browser Beta` and `Brave Browser Nightly`.)

**Fix.** Nothing. This is expected and self-healing - the 15-minute agent re-verifies and
re-writes each tick, the write sticks on the first quiet tick, and it always survives once Brave
restarts and *reads* the file. If you want it to land immediately, that is what the extension is
for: it writes through the model instead of around it.

**Do not kill Brave**, and do not "fix" this by adding a Brave-is-running gate. Losing this race
costs only the pending additions, which the retry recovers.

---

### Nothing appears at all

**Symptom.** No errors anywhere, no bookmarks, both appliers apparently idle.

**Causes, in the order worth checking** (the doctor covers most of them in one go):

1. **Nothing is staged.** `python3 -c 'import json;print(len(json.load(open("desired.json"))))'`
   from `~/.raindrop-sync`. The promotion gate is strict - classifying many items and promoting
   none is normal. Check `desired.json` before assuming a bug.
2. **The host log is silent.** `tail ~/.raindrop-sync/log/native_host.log`. No `host start` line
   in the last minute means the extension never invoked the host: see the native-messaging
   entry above. (A `pending` line whose reply carried an `error` is also logged here, for
   example `desired.json unreadable`.)
3. **The extension is running old code.** Unpacked extensions do not hot-reload. If you edited
   `sw.js` and did not bump the version, re-run bootstrap *and* click reload on the card, the
   old service worker is still live. This is the most common cause after a code change.
4. **The launchd agent never fired.** `launchctl print gui/$(id -u)/com.raindrop-sync.apply`.
   An error that the service could not be found means it was never bootstrapped - or bootstrap
   skipped it because `config.json` says `"brave_sync": true`. A loaded agent whose last exit
   code is non-zero ran and failed: read `apply.err.log`. A bare `Operation not permitted`
   there is a TCC denial, which under launchd produces no dialog at all.
5. **Preflight aborted (exit 2).** `apply.out.log` names the reason: `ABORT: Brave Sync is
   enabled. External nodes would corrupt sync metadata and upload to every device. Disable
   bookmark sync or remove this job.`, `ABORT: Bookmarks missing. Refusing to create one.`,
   `ABORT: unexpected version ...`, `ABORT: stored checksum does not match. Something else is
   editing this file.`, and so on. Each is a deliberate refusal, not a crash.
6. **The item was rejected.** Both appliers refuse an entry whose `url` is not a string, has no
   scheme, or has no host (except `file:` URLs), and one whose `folder_path` is empty once
   normalised (split on `/`, segments trimmed, empty segments dropped, one leading
   `Bookmarks Bar` stripped). The host records it once in `extension_applied` with status
   `rejected` and never offers it again; the file writer logs one line and skips it. Fix the
   entry, then clear the row so it is offered again:
   `sqlite3 ~/.raindrop-sync/state.db "DELETE FROM extension_applied WHERE raindrop_id = <id>;"`.
7. **The log you are reading was just rotated.** Each log rotates to `<name>.1` when it passes
   1 MB. `native_host.log` rotates itself; the file writer rotates `apply.out.log` and
   `apply.err.log` at the start of a run, and because launchd opened the handle before the
   rename, that run's lines still land in the `.1` file and the fresh log fills from the run
   after. Check both files.

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

Two related rules: `content_hash` is marked **only after** the item is committed to
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
(`serialize()`), so concurrent callers queue instead of racing, and nothing syncs at worker load
time: the top level only re-arms the alarm, and `onInstalled`, `onStartup` and the alarm are the
only callers. **Do not remove that lock**, and do not add a second entry point that bypasses it.

There is a second duplication route between the two appliers: `chrome.bookmarks` cannot write the
file format's `meta_info` marker, so extension-created nodes are invisible to a GUID-only
ownership check and the file writer would happily re-create them. `apply_brave.py` therefore
matches on **URL as well as GUID**, and both sides compare URLs canonically - lower-case scheme
and host, an empty path read as `/` - because Chromium stores the canonical form and Raindrop
returns whatever was saved. Ownership is tracked differently on each side - the file writer
marks nodes `meta_info {"raindrop_sync":"v1"}`, the extension records its work in
`state.db`'s `extension_applied` table, keyed `(client, raindrop_id)`.

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
# 1. Quit Brave completely (Cmd-Q). Confirm nothing is running, whichever channel you use:
pgrep -x 'Brave Browser( Beta| Nightly)?'        # expect no output

# 2. Pick the newest good backup:
ls -t ~/.raindrop-sync/backups/ | head

# 3. Confirm it parses BEFORE restoring:
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("version",d["version"],
  "roots",sorted(d["roots"]))' ~/.raindrop-sync/backups/Bookmarks.<TIMESTAMP>

# 4. Restore into the profile the installer recorded:
P="$(python3 -c 'import json,os; r=os.environ.get("RAINDROP_SYNC_ROOT", os.path.expanduser("~/.raindrop-sync")); print(json.load(open(r + "/config.json"))["profile"])')"
cp ~/.raindrop-sync/backups/Bookmarks.<TIMESTAMP> "$P/Bookmarks"

# 5. Start Brave and confirm the bar is back.
```

This is why the writer takes its own timestamped backups of **both** `Bookmarks` and
`Bookmarks.bak` before every write, re-opens the copy to confirm it parses, writes atomically
(`mkstemp` on the same volume, fsync, `os.replace`, fsync the directory), then re-reads from
disk and verifies parse, checksum, id uniqueness and GUID uniqueness - restoring the backup and
exiting non-zero if any check fails. If you are modifying the writer, keep every one of those
steps.

---

## Appendix - what bootstrap does, by hand

You do not need this section to install. It is here so the installer is not a black box, and
for the rare case of repairing one piece without re-running the whole thing. Every path below
assumes the default root; substitute yours if you set `RAINDROP_SYNC_ROOT`.

> **Regenerating `key.pem` changes the extension id.** The id is derived from the key, and the
> id is what `allowed_origins` in both host manifests, `config.json` and the card in
> `brave://extensions` all carry. If you mint a new key you must redo every step after A, remove
> the old card, and load unpacked again. Bootstrap never regenerates an existing key; if you
> do it by hand, guard it with `[ -f key.pem ] ||` as below.

### A. The signing key, the id, and `manifest.json`

```bash
cd ~/.raindrop-sync
[ -f key.pem ] || openssl genrsa -out key.pem 2048
chmod 600 key.pem

# 1. the id (32 chars, a-p only): first 16 bytes of sha256(DER public key), nibbles 0-f -> a-p
openssl rsa -in key.pem -pubout -outform DER 2>/dev/null \
  | openssl dgst -sha256 -binary \
  | xxd -p -c 32 | cut -c1-32 | tr '0-9a-f' 'a-p'

# 2. the base64 DER public key - goes into extension/manifest.json as "key"
openssl rsa -in key.pem -pubout -outform DER 2>/dev/null | openssl base64 -A
```

The first command prints something shaped like `abcdefghijklmnopabcdefghijklmnop`. **Yours will
be different** - it is derived from a key you just generated, so no id in this repo is yours,
and none of the examples here are real. `openssl` and `xxd` ship with macOS.

`extension/manifest.json` is the checkout's file with the key added:

```json
{
  "manifest_version": 3,
  "name": "Raindrop Sync",
  "version": "1.1.0",
  "description": "Applies classified Raindrop bookmarks into Brave through the bookmarks API, with no file surgery.",
  "key": "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A...<your base64 DER public key>...IDAQAB",
  "permissions": ["bookmarks", "nativeMessaging", "alarms", "storage"],
  "background": { "service_worker": "sw.js" }
}
```

and `.extension-identity.json` is `{"id": "<the id from command 1>"}`.

### B. The host wrapper

`~/.raindrop-sync/bin/native_host`, mode `755`:

```sh
#!/bin/sh
export RAINDROP_SYNC_ROOT="/Users/you/.raindrop-sync"
exec "/usr/bin/python3" "/Users/you/.raindrop-sync/bin/native_host.py"
```

Brave spawns the host with a minimal environment, so without this the host would fall back to
`~/.raindrop-sync` even when the install lives elsewhere.

### C. The native-messaging host manifests

The extension talks to the host over stdio. Brave finds the host by reading a manifest from a
fixed directory - and this is the part that is not what you would guess:

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
installer. If it is absent on yours, bootstrap creates it, and that path is untested. See
`RESEARCH.md` for the diagnosis and the log line that proves it.

Install into **both** directories - Chrome's because it works, Brave's because a future Brave
may honour its own path:

```bash
EXT_ID="$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.raindrop-sync/.extension-identity.json")))["id"])')"

for D in "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" \
         "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"; do
  mkdir -p "$D"
  cat > "$D/com.raindrop_sync.host.json" <<EOF
{
  "name": "com.raindrop_sync.host",
  "description": "raindrop-brave-sync native messaging host",
  "path": "$HOME/.raindrop-sync/bin/native_host",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://${EXT_ID}/"
  ]
}
EOF
done
```

Four rules the file must obey, all of them silent failures if broken:

- **The filename must equal the `name` field**, `com.raindrop_sync.host.json` and
  `"name": "com.raindrop_sync.host"`.
- **The same string must appear in `extension/sw.js`** as the `HOST` constant. Three places,
  one value.
- **`path` must be absolute** and the target executable: the wrapper from B.
- **`allowed_origins` must carry the trailing slash**: `chrome-extension://<id>/`.

`native_host.py` writes nothing to stdout except protocol frames. A stray `print()` corrupts
the length-prefixed stream and Brave drops the connection without saying why - all diagnostics
go to `~/.raindrop-sync/log/native_host.log`.

### D. The ledger

```bash
~/.raindrop-sync/bin/native_host.py --init-db
```

creates `state.db` with exactly these tables (the host also creates any that are missing the
first time it opens the database, so the schema has one home, `native_host.py`):

```sql
CREATE TABLE IF NOT EXISTS bookmarks(
  raindrop_id INTEGER PRIMARY KEY, title TEXT, link TEXT, content_hash TEXT,
  collection TEXT, tags TEXT, promoted INTEGER DEFAULT 0,
  taxonomy_version INTEGER, classified_at TEXT);
CREATE INDEX IF NOT EXISTS idx_hash ON bookmarks(content_hash);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT,
  classified_count INTEGER, promoted_count INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS extension_applied(
  client TEXT NOT NULL, raindrop_id INTEGER NOT NULL,
  bookmark_id TEXT, url TEXT, status TEXT, applied_at TEXT,
  PRIMARY KEY (client, raindrop_id));
```

`extension_applied.status` is one of `created`, `already_present`, `error`, `rejected`. The
first, second and fourth are terminal; `error` is retried on the next tick.

### E. `config.json`

```json
{
  "root": "/Users/you/.raindrop-sync",
  "python": "/usr/bin/python3",
  "profile": "/Users/you/Library/Application Support/BraveSoftware/Brave-Browser/Default",
  "extension_id": "abcdefghijklmnopabcdefghijklmnop",
  "brave_sync": false,
  "installed_at": "2026-01-01T12:00:00+00:00"
}
```

`profile` is `null` only under `RAINDROP_SYNC_SKIP_PROFILE_CHECK=1`. `apply_brave.py` reads
`profile` from here when `RAINDROP_SYNC_PROFILE` is unset, and falls back to
`Brave-Browser/Default` only if the file is absent or has no profile.

### F. The launchd fallback agent

This is the applier that works when Brave is closed or the extension is off. It edits Brave's
`Bookmarks` file directly, so its changes appear at the next Brave start.

`~/Library/LaunchAgents/com.raindrop-sync.apply.plist`, with your real home directory in place
of `/Users/you` (launchd does not expand `~` or `$HOME`) and your real profile directory:

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
  <dict>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
    <key>RAINDROP_SYNC_ROOT</key><string>/Users/you/.raindrop-sync</string>
    <key>RAINDROP_SYNC_PROFILE</key><string>/Users/you/Library/Application Support/BraveSoftware/Brave-Browser/Default</string>
  </dict>
</dict>
</plist>
```

Load it, run it once, and check it:

```bash
plutil -lint ~/Library/LaunchAgents/com.raindrop-sync.apply.plist
launchctl bootout gui/$(id -u)/com.raindrop-sync.apply 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.raindrop-sync.apply.plist
launchctl kickstart -p gui/$(id -u)/com.raindrop-sync.apply     # run once now
launchctl list | grep raindrop-sync
```

Expected last line - `-` (not currently running), exit status `0`:

```
-	0	com.raindrop-sync.apply
```

`bootout` first makes this idempotent: `bootstrap` refuses a label that is already loaded. The
legacy `launchctl load` and `unload` verbs are not used anywhere in this project; mixing the
two families on one label is a reliable way to confuse yourself.

**Why `/usr/bin/python3` and not Homebrew.** The interpreter path must be absolute and stable.
`/opt/homebrew/bin/python3` is a symlink that moves on `brew upgrade`, which silently breaks the
agent. If you must use Homebrew, name the versioned path
(`/opt/homebrew/opt/python@3.x/bin/python3.x`), never the `bin` symlink, and pass it as
`RAINDROP_SYNC_PYTHON` so the wrapper and the plist agree. These scripts are pure stdlib, so the
system interpreter is strictly the more robust choice here.

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

## Notes on what is per-install, and what is not tested

**Per-install, never shared:** your Raindrop token, `key.pem`, your extension id, your Raindrop
collection ids, `taxonomy.json`, `collection-map.json`, `state.db`, `desired.json`,
`config.json`. Every id and key shown in this document is a placeholder of the right shape.
None of them are real, and none of them are yours.

**Not verified, as of this writing:**

- Raindrop's `/user/stats` and `meta.changedBookmarksDate` gate semantics. It is unconfirmed
  whether a tag-only edit advances that field, so Phase A currently does a full read every run
  rather than trusting a zero-call exit path. The design also calls for a max-staleness override - a forced full read after 7 days - because nothing documents which mutations advance it.
- `/raindrop/{id}/suggest` plan gating (expected to 402/403 on the free plan). Unused either way.
- `meta_info` marker survival is verified against Brave 152 only. **Re-verify it after a major
  Brave upgrade** - marker-based ownership is what makes it safe to place bookmarks into your
  own hand-curated folders, and it is the assumption most likely to be broken by an upstream
  change.
- Brave Beta and Nightly profiles are detected by the same rule as stable, but the install was
  only exercised against stable.

**Deletion propagation is off by default and additive-only.** A Raindrop item that is trashed,
or simply missed by a read race, looks identical to a hard delete. Turning deletion on would
require absence from N consecutive full reads, a re-query of the id, and a check of collection
`-99` (Trash) - none of which is built.

**Safari is not supported and cannot be.** Every route was investigated and refuted; see
`RESEARCH.md`. If you want your classified bookmarks in Safari, export a
`NETSCAPE-Bookmark-file-1` HTML file and import it by hand via Safari > File > Import From,
after exporting your existing bookmarks first. Re-import dedupe behaviour is unknown, so treat
that as a rare deliberate action, never a habit.

---

## Uninstall

Everything the installer put outside the checkout, removed in the reverse order. Bookmarks the
tool created stay in Brave, because nothing here deletes bookmarks; drag the `Raindrop` folder
to the bin yourself if you want it gone.

```bash
# 1. The launchd agent and its plist
launchctl bootout gui/$(id -u)/com.raindrop-sync.apply 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.raindrop-sync.apply.plist

# 2. Both native-messaging host manifests (leave the directories; other tools use them)
rm -f "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.raindrop_sync.host.json"
rm -f "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/com.raindrop_sync.host.json"

# 3. The keychain item (use your RAINDROP_SYNC_KEYCHAIN_SERVICE name if you changed it)
security delete-generic-password -s raindrop-api >/dev/null 2>&1 || true

# 4. The runtime tree (token file, key, ledger, backups, logs) and the Claude skill
rm -rf ~/.raindrop-sync
rm -rf ~/.claude/skills/raindrop-sync
```

Then, by hand:

- `brave://extensions`: **Remove** the Raindrop Sync card.
- The Claude desktop app: delete the scheduled task from step 5, if you created one.
- app.raindrop.io > Settings > Integrations: delete the test token, so the credential this
  install used no longer exists anywhere.

The checkout under `~/src/raindrop-brave-sync` is just code; delete it or keep it.
