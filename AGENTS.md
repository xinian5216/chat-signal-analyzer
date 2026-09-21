# AGENTS.md

Repo-specific guidance for OpenCode / AI sessions. Everything here was verified against the current code; omit nothing, add no fluff.

## Environment (Windows)

- Use the project venv: `.venv\Scripts\python` — there is **no usable system Python** (`python` on PATH is the Windows Store stub, no `py` launcher).
- The console mangles UTF-8 Chinese. When a script prints or reads Chinese, run `python -X utf8 ...`; edit files with file tools, not shell `echo`.

## Commands

- Tests: `.venv\Scripts\python -m pytest tests -q` — 211 tests, ~10s. Single test: `... -m pytest tests/test_scoring.py::test_transform_noul_noise_floor -q`. Run from the repo root (`pytest.ini` sets `pythonpath = .`, `testpaths = tests`).
- App: `.venv\Scripts\streamlit run app.py`. Needs `TYPESAFE_API_KEY` in the gitignored `.env`; without a key the UI shows a friendly hint and does not crash.
- Real-API smoke test — **the only script that calls Jev** (5 constructed messages, then fully cached): `.venv\Scripts\python -X utf8 scripts\smoke_test.py`.

## Testing rules (hard)

- Tests must **never** call the real Jev API — mock `client.system_one` (pattern: `FakeClient` in `tests/test_analyzer.py`).
- Tests must not read the real `.env` and must not depend on `.jev_cache` — use `tmp_path` fixtures.
- CI (`.github/workflows/tests.yml`) runs `pytest tests -q` on ubuntu-latest / Python 3.11 with **no API key**. The `pytest` status check is required on `main`.

## Architecture

Single Streamlit app, flat modules, no package:

- `parser.py` — chat text → messages; participant/nickname detection; non-text media placeholder filtering (`content_type` text/media/mixed, WeChat media filenames stripped, real Unicode emoji preserved)
- `privacy.py` — local masking before anything leaves the machine
- `analyzer.py` — one Jev `system_one` call per TA message carrying all 9 questions (2 Choice + 5 Score + 2 Noul). Pure-media messages (`content_type == "media"`) are never targets: zero API calls, excluded from every statistic
- `scoring.py` — v2 weighted aggregation; **all weights and thresholds are centralized at the top of the file**
- `storage.py` — SQLite result cache
- `report.py` — Markdown / JSON / summary export
- `ui_helpers.py` — pure display helpers only (short labels, media badges, message filtering, overview layout data); **no scoring/business logic may live here**
- `media.py` — `MediaAsset`, upload/clipboard limits, conservative placeholder→image binding. **Image binaries must never enter Jev state, SQLite, or reports**
- `rich_paste.py` — rich-paste component wrapper + `assets_from_uploader` fallback bridge
- `vision.py` — vision interface stub, **disabled by default** (no vendor chosen yet)
- `components/rich_paste/index.html` — vanilla-JS Streamlit custom component for the Clipboard Probe. Note: on Streamlit 1.64 the component value **cannot** reliably reach Python (verified), so the Probe is **self-contained inside the iframe**; do not re-litigate the protocol without new evidence
- `tools/clipboard_probe/` — probe report formatting + the real-device WeChat test checklist
- `launcher/` — Windows `install.bat` / `start.bat` (script-relative paths, single-instance guard, localhost only)
- `app.py` — Streamlit UI: 4 stages (input → confirm → analyze → results tabs), all API calls happen only inside `run_analysis`

## Hard constraints (change only with explicit user approval)

- Do not alter the v2 aggregation formula in `scoring.py` or the semantics of the Jev questions.
- Changing the Jev question set **requires bumping `SCHEMA_VERSION` in `analyzer.py`** (currently `chat-signal-v2.1`): the cache key hashes it, so old entries invalidate naturally. Never delete `.jev_cache`.
- Never send media placeholders (`[图片]`, `[视频]`, `[动画表情]`, `[语音]`, `[文件]`) to Jev as analyzable text: Jev has no image input and the copied text contains no actual media. Pure-media messages must stay non-targets; in context they become neutral markers whose content must never be guessed. No vision model, no OCR, no `.dat`/`.mp4` reading.
- Report export must stay **zero-API**: pure functions over already-computed results/stats, no generative LLM.
- `storage.Cache` must keep **short-lived connections** (`_connect()` per operation). A long-lived `sqlite3.Connection` breaks under Streamlit reruns with a cross-thread `ProgrammingError` — this was a real bug; do not reintroduce it.

## Git

- Identity (global + repo-local): `xinian5216 <59920440+xinian5216@users.noreply.github.com>`. Verify `git config user.name` / `user.email` before committing; never commit under a generic `noreply` identity.
- Create ordinary commits with **local git only** — never via GitHub REST/GraphQL API. (API use is fine for issues, releases, CI queries, repo settings.)
- `main` is protected: `pytest` check required, force pushes and deletions blocked. Never `git push --force`. If a history rewrite is explicitly approved: temporarily allow force pushes → `git push --force-with-lease=<ref>:<old-sha>` → restore protection immediately.
- Never commit: `.env`, `.jev_cache/`, real chat text or real nicknames, exported reports, `*.db`. All sample chat content in `tests/` and `README.md` must stay fictional.
- If a secret ever reaches a commit: stop, rotate it, and clean history before any push (see `SECURITY.md`).

## Releases

- Releases are manual: hand-built `SignalLens-vX.Y.Z.zip` (source + tests only; no `.git`, `.venv`, `.jev_cache`, `*.db`) plus a `.sha256` sidecar, attached to an annotated tag.
