# Privacy

## What SignalLens does locally

SignalLens is a **local-first** application:

- Parsing happens locally.
- Privacy redaction (phone numbers, emails, ID numbers, IP addresses, URLs,
  secrets, card numbers) happens locally, **before** anything leaves your
  machine.
- Result caching (SQLite) happens locally and is gitignored. The database stores
  structured Jev results under a SHA256 key; it does not store chat state as
  plaintext.
- Report export (Markdown / JSON / summary) happens locally.
- No generative LLM is used for summaries — all text is produced by
  deterministic templates from the existing structured results.

## What leaves your machine

Only the **processed analysis state** is sent to the **TypeSafe Jev API** for
structured judgment: the target message, at most five preceding messages,
normalized roles (`me` / `them`), optional timestamps, and the analysis rule,
all after local redaction. Parser-only metadata such as the original sender
nickname (`raw_speaker`) is not sent.

The TypeSafe API key is used only to authenticate requests directly to
TypeSafe. It is never written to chat content, the analysis cache, reports, or
logs. If the user chooses to save it, it is stored only in the local settings
file.

## What SignalLens does NOT claim

- It does not read minds and does not know anyone's true feelings.
- Results describe **observable textual interaction signals only**.
- The "interaction closeness signal index" is not a probability that someone
  likes you, and must not be used to make decisions about another person.

## Your responsibilities

- Review the current TypeSafe data-processing policy yourself; this project
  cannot make privacy promises on TypeSafe's behalf.
- Never commit your `.env` file or API key.
- Exported reports may contain locally redacted chat text if you enable that
  option — store them like any other private document.
