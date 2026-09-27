# ax

Show remaining Codex and Claude Code limits and switch accounts by email.
Requires `uv`, Python 3.14+, the Codex CLI or Claude Code, and macOS or Linux.

Run from the project folder to create the command link:

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/ax.py" ~/.local/bin/ax
export PATH="$HOME/.local/bin:$PATH"
```

Add the `export` line to your shell configuration to keep it after a restart.

| Command | Action |
| --- | --- |
| `ax` | Show saved accounts and remaining limits. `*` marks the active account. |
| `ax codex` | Show only Codex accounts. |
| `ax claude` | Show only Claude Code accounts. |
| `ax codex login` | Save another Codex account without changing the active account. |
| `ax codex login --device-auth` | Sign in to Codex with a device code. |
| `ax claude login` | Save another Claude Code account without changing the active account. |
| `ax add` | Save the current accounts. |
| `ax user@example.com` | Activate a saved account in each tool that has it. |
| `ax claude user@example.com` | Activate a saved account in one tool. |

Close Codex and Claude Code before switching. Then run:

```sh
ax user@example.com
codex
claude
```

You can also run `uv run ax.py` with the same arguments.
At zero percent, `ax` shows the reset date in the host time zone.

Keep `config.toml` beside the script.
Do not share credential files.

## Codex

By default, active credentials are in `~/.codex/auth.json`.
Saved accounts are in `~/.codex/cx/accounts/`.
`CODEX_HOME` overrides the configured Codex folder.

Set file storage in the Codex configuration:

```toml
cli_auth_credentials_store = "file"
```

## Claude Code

On macOS, active credentials are in the Keychain item `Claude Code-credentials`.
On Linux, they are in `~/.claude/.credentials.json`.
The active account is in `~/.claude.json`.
Saved accounts are in `~/.claude/ax/accounts/`.
`CLAUDE_CONFIG_DIR` overrides the configured Claude Code folder.
