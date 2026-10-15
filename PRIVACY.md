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
structured judgment: the target message and a bounded recent context window
(default: up to 8 turns / 12 messages / approximately 4000 text characters;
the nearest preceding message is retained whole),
normalized roles (`me` / `them`), optional timestamps, and the analysis rule,
all after local redaction. Parser-only metadata such as the original sender
nickname (`raw_speaker`) is not sent.

The TypeSafe API key is used only to authenticate requests directly to
TypeSafe. It is never written to chat content, the analysis cache, reports, or
logs. If the user chooses to save it, it is stored only in the local settings
file.

## Optional experimental requests

All experiments are off by default and run only after the user starts analysis.

- Related-history retrieval can add up to three earlier messages (1200 characters)
  from the current import when a literal back-reference matches. It never reads
  friend names, profile notes or stored summaries, and never adds future messages.
- Image assistance sends only a processed static image after the user binds it,
  generates the final upload preview and explicitly approves that preview.
  Crop/masking, orientation correction, resizing and metadata removal happen
  locally. Text redaction does not cover text or identities visible in an image;
  review the preview carefully. The request also contains locally redacted causal
  conversation context. Unapproved images and image binaries never enter the
  text-analysis state, SQLite cache, reports, logs or friend history. Images are
  sent to TypeSafe only in the separate approved experimental image request.
- Optional semantic event review sends bounded, redacted source messages for up
  to eight existing candidates. It does not send event fingerprints, friend
  identifiers, nicknames or audit notes, and never confirms a candidate itself.
- Image and event caches store only typed judgments under hashed request keys.
  They contain no image bytes or copied request state. Visual and semantic
  findings remain session-local; reports and friend snapshots store only
  aggregate request/completion counts for these optional features.
- Image requests enable only the required experimental image header. No
  debugging/data-sharing opt-in is enabled by this application.

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
