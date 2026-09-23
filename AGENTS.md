# AGENTS.md

Repo-specific guidance for OpenCode / AI sessions. Everything here was verified against the current code; omit nothing, add no fluff.

## Environment (Windows)

- Use the project venv: `.venv\Scripts\python` — there is **no usable system Python** (`python` on PATH is the Windows Store stub, no `py` launcher).
- The console mangles UTF-8 Chinese. When a script prints or reads Chinese, run `python -X utf8 ...`; edit files with file tools, not shell `echo`.

## Commands

- Tests: `.venv\Scripts\python -m pytest tests -q` — 465 tests (412 + 53 evaluation/isolation), ~56s. Single test: `... -m pytest tests/test_scoring.py::test_transform_noul_noise_floor -q`. Run from the repo root (`pytest.ini` sets `pythonpath = .`, `testpaths = tests`).
- App: `.venv\Scripts\streamlit run app.py` (dev mode).
- Benchmark (offline, deterministic): `.venv\Scripts\python scripts\evaluate.py --fixtures` (real mode `--real --yes-run-live-api` + `TYPESAFE_API_KEY` is human-only, never run by CI; per case it analyzes **only** that case's TA target via `only_indices` — N cases = N requests — `--report` writes the aggregate comparable via `--compare`, `--raw-report` stores raw model outputs separately).
- Portable build: `.venv\Scripts\python -m PyInstaller --noconfirm --clean SignalLens.spec` → `.venv\Scripts\python scripts/frozen_smoke.py` → `.venv\Scripts\python scripts/package_portable.py`. Needs `TYPESAFE_API_KEY` in the gitignored `.env`; without a key the UI shows a friendly hint and does not crash.
- Real-API smoke test — **the only script that calls Jev** (5 constructed messages, then fully cached): `.venv\Scripts\python -X utf8 scripts\smoke_test.py`.

## Testing rules (hard)

- Tests must **never** call the real Jev API — mock `client.system_one` (pattern: `FakeClient` in `tests/test_analyzer.py`).
- Tests must not read the real `.env` and must not depend on `.jev_cache` — use `tmp_path` fixtures.
- CI (`.github/workflows/tests.yml`) runs `pytest tests -q` on ubuntu-latest / Python 3.11 with **no API key**. The `pytest` status check is required on `main`.

## Architecture

Single Streamlit app, flat modules, no package:

- `parser.py` — chat text → messages; **format-isolated** parsing via `detect_format()` (`wechat_blocks` / `legacy_colon` / `time_name` / `unknown`). In `wechat_blocks` mode a message ends only on a valid sender candidate + a next-line **fullmatch** timestamp — no legacy `speaker: content` / `time name` guessing inside BODY, so long technical bodies (code, JSON, `已知现状：`, URLs) never create fake participants. Legacy colon mode remains a separate mode; legacy `speaker: content` / `time name` branches isolated from URL schemes and in-body time substrings; participant detection reads **only** parsed `raw_speaker` (no repetition requirement); non-text media placeholder filtering (`content_type` text/media/mixed, WeChat media filenames stripped, real Unicode emoji preserved, voice duration → local-only `duration_seconds`)
- `merge.py` — local chunk append / fingerprint / dedup (0 Jev API). Fingerprints and chunk info are local metadata only: they must never enter Jev state, cache keys, or default report bodies
- `privacy.py` — local masking before anything leaves the machine
- `context_builder.py` — **Context Builder v2** (pure logic, zero Jev calls): turn-aware, bounded context selection. A turn = consecutive messages of the same speaker; from each TA target the builder walks turns backwards and keeps the most recent complete turns (esp. the nearest me-turn the TA is replying to) under three simultaneous budgets — `CONTEXT_MAX_TURNS=8` / `CONTEXT_MAX_MESSAGES=12` / `CONTEXT_MAX_CHARS=4000` (chars ≈ sum of message text lengths, no tokenizer). Replaces the old mechanical `context[-5:]`. Never truncates inside a message (P0: the message immediately before the target is always kept whole, even past the char budget); the target itself is never budget-limited; no future messages ever enter the window; no semantic/keyword retrieval — turn + recency + budget only. Output is flattened back to chronological `list[dict]`; internal turn metadata never enters Jev state
- `analyzer.py` — one Jev `system_one` call per TA message carrying all 9 questions (2 Choice + 5 Score + 2 Noul; never split the questions of one message). `build_state()` is the final outbound allowlist (only normalized role, redacted text, optional time, and the analysis rule may leave the process; parser metadata such as `raw_speaker` must stay local) **and** delegates context selection to `context_builder.select_context`; the result entry's `"context"` is the exact window sent to Jev (UI shows what Jev saw). Cache is consulted first: only misses go to a **bounded** ThreadPoolExecutor (`JEV_MAX_WORKERS`/`SIGNALLENS_JEV_CONCURRENCY`, default 4, clamped 1..8), with worker-local `TypeSafeClient` instances (the SDK does not promise thread safety) that are closed afterwards; results are reassembled in the original target order. Pure-media messages (`content_type == "media"`) are never targets: zero API calls, excluded from every statistic
- `evaluation.py` + `evaluation/` + `scripts/evaluate.py` — **offline evaluation harness**: deterministic benchmark over 34 fully fictional cases (`evaluation/cases.json`, categories A–Z). Each case states expectations (choice allowlists, score `[min,max]` ranges, Noul probability bounds), `must_not_infer` anti-overclaim contracts (romantic_from_care_alone / romantic_from_late_night / distancing_from_slow_reply / …, thresholds centralized in `MUST_NOT_INFER_RULES`), acceptable ambiguity, and optional context checks (no future-message leakage; required turn contents must be in the Context Builder v2 window). `evaluate_case`/`evaluate_cases` do constraint checking only — no Jev call, no second LLM judging Jev. Aggregates: per-case pass/fail, per-constraint pass/fail, dimension failure counts, false-positive categories (romantic / distancing / special_attention overclaim / warmth-estimation / context_misunderstanding / future_message_leakage / media_guessing). `scripts/evaluate.py --fixtures` runs offline against `evaluation/fixtures/baseline_v2.2.json` (synthetic results for CI/self-test, **not** real Jev output); `--real --yes-run-live-api` + `TYPESAFE_API_KEY` is the human-only live mode (double gate; CI never triggers it); `--compare` diffs baseline vs candidate **by human-defined constraints only** — model output drift is never called improvement. `evaluation/reports/` is gitignored
- `scoring.py` — v2 weighted aggregation; **all weights and thresholds are centralized at the top of the file**
- `storage.py` — SQLite result cache
- `report.py` — Markdown / JSON / summary export
- `ui_helpers.py` — pure display helpers only (short labels, media badges, message filtering, overview layout data); **no scoring/business logic may live here**
- `media.py` — `MediaAsset`, upload/clipboard limits, conservative placeholder→image binding. **Image binaries must never enter Jev state, SQLite, or reports**
- `rich_paste.py` — rich-paste component wrapper + `assets_from_uploader` fallback bridge
- `vision.py` — vision interface stub, **disabled by default** (no vendor chosen yet)
- `components/rich_paste/index.html` — vanilla-JS Streamlit custom component for the Clipboard Probe. Note: on Streamlit 1.64 the component value **cannot** reliably reach Python (verified), so the Probe is **self-contained inside the iframe**; do not re-litigate the protocol without new evidence
- `tools/clipboard_probe/` — probe report formatting + the real-device WeChat test checklist
- `paths.py` — single place for mutable data locations: dev mode keeps the legacy repo paths (`.jev_cache/cache.db`, `.env`, `.media_cache/`, `logs/`); portable (frozen or `SIGNALLENS_DATA_DIR`) puts everything under `data/` next to the exe (`cache.sqlite3`, `settings.env`, `media_cache/`, `logs/`, `runtime.json`). A non-writable data dir raises a friendly `DataDirError` — it must never fall back to AppData/TEMP
- `settings_store.py` — local config (API Key only) with priority: process env > `data/settings.env` > dev-mode repo `.env`. Never logs/prints/returns the key itself
- `portable_launcher.py` + `SignalLens.spec` — PyInstaller **onedir** Windows portable entry: exe-relative data dir, port 8765~8785 bound to 127.0.0.1 only, single instance validated by PID + port + `/healthz`, bounded startup poll + `webbrowser.open`, child Streamlit always reaped, console kept on purpose (no secrets printed)
- `launcher/` — Windows `install.bat` / `start.bat` (developer/fallback tools, script-relative paths, single-instance guard, localhost only); the normal-user path is the portable ZIP
- `app.py` — Streamlit UI: 4 stages (input → confirm → analyze → results), all API calls happen only inside `run_analysis`. Results are rendered through **server-side lazy navigation** (`st.segmented_control` + if/elif), never `st.tabs`: tabs execute every tab's Python on each rerun, which kept the script "running" for seconds after results were visible. “全部消息” is paginated (25/page); reports are built only when the report view is open. `analysis_state` is an explicit state machine (idle/pending/running/complete/error/interrupted) — a fresh rerun that still sees `running` means the previous run was interrupted, and is recovered automatically

## Hard constraints (change only with explicit user approval)

- Do not alter the v2 aggregation formula in `scoring.py` or the semantics of the Jev questions.
- Changing the Jev question set **requires bumping `SCHEMA_VERSION` in `analyzer.py`** (currently `chat-signal-v2.2`): the cache key hashes it, so old entries invalidate naturally. Never delete `.jev_cache`. The same bump is required when the `conversation_context` selection semantics change (v2.2 = Context Builder v2 replaced the mechanical `context[-5:]`; the 9 questions themselves are unchanged since v2.1).
- Never send media placeholders (`[图片]`, `[视频]`, `[动画表情]`, `[语音]`, `[文件]`) to Jev as analyzable text: Jev has no image input and the copied text contains no actual media. Pure-media messages must stay non-targets; in context they become neutral markers whose content must never be guessed. No vision model, no OCR, no `.dat`/`.mp4` reading.
- Never send `raw_speaker`, raw nicknames, chunk metadata, fingerprints, media binaries, or other parser-only fields to Jev. Keep the outbound allowlist enforced in `build_state()` and covered by tests.
- **Before changing the Jev questions, Context Builder, or scoring, run the benchmark first** (`python scripts/evaluate.py --fixtures`, or a saved real-mode report) and report baseline vs candidate via `--compare`. The evaluation harness is the quality gate for algorithm changes; its pass rate is a regression signal on human-defined cases, **not** a scientific accuracy claim, and must never be packaged as one.
- Report export must stay **zero-API**: pure functions over already-computed results/stats, no generative LLM.
- `storage.Cache` must keep **short-lived connections** (`_connect()` per operation). A long-lived `sqlite3.Connection` breaks under Streamlit reruns with a cross-thread `ProgrammingError` — this was a real bug; do not reintroduce it.

## Git

- Identity (global + repo-local): `xinian5216 <59920440+xinian5216@users.noreply.github.com>`. Verify `git config user.name` / `user.email` before committing; never commit under a generic `noreply` identity.
- Create ordinary commits with **local git only** — never via GitHub REST/GraphQL API. (API use is fine for issues, releases, CI queries, repo settings.)
- `main` is protected: `pytest` check required, force pushes and deletions blocked. Never `git push --force`. If a history rewrite is explicitly approved: temporarily allow force pushes → `git push --force-with-lease=<ref>:<old-sha>` → restore protection immediately.
- Never commit: `.env`, `.jev_cache/`, real chat text or real nicknames, exported reports, `*.db`. All sample chat content in `tests/` and `README.md` must stay fictional.
- If a secret ever reaches a commit: stop, rotate it, and clean history before any push (see `SECURITY.md`).

## Releases

- `VERSION` is the release version source. Keep it aligned with the `vX.Y.Z` tag.
- `.github/workflows/build-windows-portable.yml` owns release packaging: it runs tests, builds the Windows Portable app, performs the frozen smoke test, and produces the ZIP plus `.sha256` sidecar.
- `workflow_dispatch` uploads a temporary Actions artifact only. Pushing a `v*` tag creates or updates the GitHub Release and attaches the generated Portable artifacts; do not hand-build or attach a source ZIP as the application release.
