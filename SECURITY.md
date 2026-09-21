# Security Policy

## Reporting a Vulnerability

Please **do not** report security issues in public GitHub Issues.

If GitHub Security Advisory is enabled for this repository, use it:

- Repository → **Security** tab → **Report a vulnerability**

That gives maintainers a private channel to triage and fix the issue before
public disclosure.

## Never Post Secrets or Private Chat Content

- **Never** post your TypeSafe API key, tokens, or any credential in an Issue,
  Pull Request, or Discussion.
- **Never** post raw private chat logs in an Issue. Use synthetic, fictional
  examples when reporting parsing bugs.

## If You Believe Your API Key Leaked

1. Immediately revoke / rotate the key on the TypeSafe side
   (<https://console.typesafe.ai/>) and generate a new one.
2. Update your local `.env` (which is gitignored and must never be committed).
3. If the key was committed to a public repository, treat it as compromised
   even after deleting the file — Git history retains it.

## Scope

SignalLens runs entirely on your machine. This policy covers the application
code in this repository only; the TypeSafe Jev API service is operated by
TypeSafe and is out of scope.
