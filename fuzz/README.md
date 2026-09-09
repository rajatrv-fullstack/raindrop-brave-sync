# Fuzz harness

Two [atheris](https://github.com/google/atheris) targets, run by the Fuzz workflow on every push
that touches `src/` and for ten minutes every week. Each target states a contract and fails the
moment an input breaks it.

| Target | What it feeds | Contract |
| --- | --- | --- |
| `fuzz_native_host.py` | Arbitrary JSON messages to `native_host.handle()`, plus corrupt or truncated `desired.json` and `reset_folders.json` on disk | Always returns a JSON-serialisable dict, never raises, and the host still answers `ping` afterwards |
| `fuzz_bookmarks.py` | Generated bookmark trees (every Unicode plane, lone surrogates in titles) and arbitrary bytes as the `Bookmarks` file | `checksum()` is total and deterministic; `preflight()` returns a document or exits 2, raises nothing else, and never modifies the file |

## Running locally

With libFuzzer available (Linux, or macOS with an LLVM that ships it):

```bash
pip install --require-hashes -r .github/requirements/fuzz.txt
python fuzz/fuzz_bookmarks.py -max_total_time=60
```

Without it, the same file runs a short random smoke loop through a small shim (`_fdp.py`) so
the harness can be checked on any machine:

```bash
python fuzz/fuzz_native_host.py
```

To replay a crashing input from a CI artifact: `python fuzz/<target>.py crash-<hash>`.

## Findings so far

- `preflight()` raised `UnicodeEncodeError` instead of aborting when a URL held a lone surrogate. Fixed in 1.0.1.
