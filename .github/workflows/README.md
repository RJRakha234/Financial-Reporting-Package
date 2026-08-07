# Claude GitHub workflows

Two workflows wire this repository up to the
[Claude Code GitHub Action](https://github.com/anthropics/claude-code-action).

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `claude.yml` | `@claude` in an issue or PR comment, a PR review, or a new issue's title/body | Claude answers, edits code and pushes commits, replying in a comment on the same issue or PR |
| `claude-code-review.yml` | Every non-draft PR opened, reopened, marked ready, or pushed to | Claude reviews the diff and leaves inline comments on the PR |

## Setup

Both workflows are inert until two things exist. They need repository admin
access to configure.

**1. Install the Claude GitHub App**

Install <https://github.com/apps/claude> on this repository. The workflows rely
on its Contents, Issues and Pull requests read/write permissions.

**2. Add the authentication secret**

Add a repository secret named `ANTHROPIC_API_KEY`, holding a key from the
[Claude Console](https://console.anthropic.com):

> Settings → Secrets and variables → Actions → New repository secret

To bill against a Claude subscription (Pro, Max, Team, Enterprise) instead of
API credits, run `claude setup-token` locally, store the result as
`CLAUDE_CODE_OAUTH_TOKEN`, and change the `anthropic_api_key:` line in both
workflows to:

```yaml
claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Then mention `@claude` in any issue or PR comment to test it.

## Notes on this repository's setup

- **Dependencies are pre-installed.** `claude.yml` sets up Python 3.11 and
  installs both `fincheck/` and `nifty50-signal-engine/[dev]` before Claude
  starts, so Claude can run `pytest`, `ruff` and `mypy` and verify its own
  changes rather than only reasoning about them. That step is
  `continue-on-error` — a dependency failure degrades Claude to read-only
  reasoning instead of killing the run.
- **Project context is passed via `--append-system-prompt`.** It tells Claude
  what the two subprojects are, how to test each one, and the two invariants
  that matter most: `nifty50-signal-engine` is alert-only and must never gain
  order-placement capability, and all datetimes must stay timezone-aware. If
  you later add a root `CLAUDE.md`, move that guidance there — Claude reads it
  automatically on every run, and it will apply to local sessions too.
- **The review workflow skips fork PRs.** GitHub withholds secrets from
  fork-triggered runs, so the job would fail on a missing key. The `if`
  condition skips them cleanly instead.
- **Only users with write access can trigger Claude**, and bot actors are
  rejected by default so Claude cannot be looped by another automation.

## Cost control

Each run consumes GitHub Actions minutes plus Claude API tokens. Already in
place: a `timeout-minutes` cap on both jobs, and `concurrency` groups so
repeated pushes to a PR cancel the superseded review (`claude.yml` queues
rather than cancels, so an in-flight commit is never lost). To cap work per run
further, add `--max-turns N` to `claude_args`.

## Removing this

Delete both workflow files, then delete the `ANTHROPIC_API_KEY` secret. Leave
the GitHub App installed if any other Claude feature uses it.
