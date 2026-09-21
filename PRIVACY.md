# Privacy

## What SignalLens does locally

SignalLens is a **local-first** application:

- Parsing happens locally.
- Privacy redaction (phone numbers, emails, ID numbers, IP addresses, URLs,
  secrets, card numbers) happens locally, **before** anything leaves your
  machine.
- Result caching (SQLite, `.jev_cache/`) happens locally and is gitignored.
- Report export (Markdown / JSON / summary) happens locally.
- No generative LLM is used for summaries — all text is produced by
  deterministic templates from the existing structured results.

## What leaves your machine

Only the **processed analysis state** (the target message plus at most five
preceding context lines, after local redaction) is sent to the
**TypeSafe Jev API** for structured judgment.

## What SignalLens does NOT claim

- It does not read minds and does not know anyone's true feelings.
- Results describe **observable textual interaction signals only**.
- The "interaction closeness signal index" is not a probability that someone
  likes you, and must not be used to make decisions about another person.

## Your responsibilities

- Review the current TypeSafe data-processing policy yourself; this project
  cannot make privacy promises on TypeSafe's behalf.
- Never commit your `.env` file or API key.
- Exported reports may contain chat text if you enable that option — store
  them like any other private document.
