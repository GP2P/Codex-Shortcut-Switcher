# Security

This project copies Codex `auth.json` files between local account profiles. Treat the state directory as sensitive.

## Expected Sensitive Files

Do not publish or share:

- `~/.codex/auth.json`
- `~/.codex-shortcut-switcher/`
- any copied `auth.json` file

The repository `.gitignore` excludes common local credential paths, but check `git status` before publishing.

Only use this tool with accounts you own or are explicitly authorized to use, and follow OpenAI's terms, policies, and usage rules.

## Usage Lookup

`list --usage` sends the current access token to the ChatGPT usage endpoint. It intentionally does not use refresh tokens, because refresh tokens rotate and can invalidate other copied auth files.

## Reporting

If you find a bug that can expose tokens or write credentials with loose permissions, open a GitHub issue without including secrets.
