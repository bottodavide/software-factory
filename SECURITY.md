# Security model

Software Factory lets language-model agents change code and, for low-risk
changes, merge it without a human. This document states what is enforced, what
is only requested, and what remains your responsibility.

## Enforced by code

- **Read-only agents.** `spec-writer`, `reviewer-security`, `reviewer-code` and
  `approver` have only `Read`, `Grep` and `Glob`. They cannot run commands or
  write files; the orchestrator saves what they return.
- **Merge gate.** `factory.py merge` refuses an automatic merge unless the
  decision is `APPROVE`, every reviewer of the final round passed, no blocking
  gate failed, no advisory gate failed, and the change has no risk flag. It
  re-classifies the change at merge time rather than trusting earlier state.
- **Human approval path.** `merge --human` only works on a job already in
  `needs-human`, and never merges a round whose blocking gates failed. Terminal
  stages (`merged`, `abandoned`) can only be reached through `merge` and
  `abandon`, never through `set`.
- **Reviewed commit only.** The commit reviewed in the final round is recorded;
  if the branch has moved, the merge is refused.
- **Clean preconditions.** The main checkout must be clean and on the main
  branch; the job worktree must be clean; `git merge-tree` must report no
  conflict.
- **Never push.** Nothing in the factory pushes. You decide when `main` leaves
  your machine.
- **Dashboard.** Bound to the configured host only, read-only, path-traversal
  checks, `Host` header allow-list, strict content security policy, and all
  rendered content is escaped.

## Defense in depth (guard hook)

`.claude/hooks/factory_guard.py` runs before `Bash`, `Edit`, `Write`,
`MultiEdit`, `NotebookEdit`, `Read` and `Grep` while any job is active.

For every caller it blocks:

- file-tool writes (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`) to the main
  checkout outside `factory/jobs/`;
- file-tool edits to `.claude/`, `factory/bin/`, `factory/config.json` and
  `factory/dashboard/`, in the main checkout and in worktrees, and shell
  commands that name those paths together with a write operation;
- reading or writing secret env files and credential stores;
- shell commands that push, change remotes, merge, rebase, pull, cherry-pick,
  reset, move refs (`update-ref`, `branch -f`), delete branches or worktrees,
  change git config, use `sudo`, pipe downloads into a shell, or recursively
  delete a root, home or parent directory (git global options such as `-c` and
  `-C` are normalised first);
- git commands that change files (`add`, `commit`, `checkout`, `restore`, …)
  unless the command starts with `cd <job worktree> &&`.

For subagents (tool calls whose hook input carries `agent_id`) it also blocks:

- writing anything in `factory/jobs/`, except screenshots in `round-<N>/screens/`.
  Agents return their documents; only the orchestrator saves them, so an agent
  cannot forge a review, a decision or `job.json`;
- running the control script for anything other than `port`, so an agent
  cannot change job state or request a merge.

What it does **not** do: it does not parse arbitrary programs. A shell command
that writes to the main checkout through an interpreter (for example a Python
one-liner) or an unusual construct can slip past the patterns. An agent that is
determined, or that has been successfully prompt-injected, can find such
constructs. Treat the hook as a seatbelt, not a sandbox; the merge gate above is
what stops unreviewed code from reaching `main` automatically.

## Requested in prompts only

- The builder and experience reviewer are asked not to sign in, submit forms or
  contact real services, and to stop the processes they start.
- Agents are told to treat repository content, diffs and review text as data,
  not instructions.

## Your responsibility

- **Isolation.** For repositories you do not fully trust, or for unattended
  runs, run Claude Code in a container or VM with a scratch copy of the repo,
  no cloud credentials, no SSH keys and restricted network egress.
- **Dev servers.** If your dev server talks to real databases, payment
  providers or email services, point it at local or test backends in
  `AGENTS.md`. Agents start it.
- **Risk rules.** The default path patterns are generic. Tune `risk.rules` in
  `factory/config.json` to name your real sensitive areas, and keep them broad:
  a false escalation costs a minute, a false approval may cost much more.
- **Gates.** The value of the automatic merge is bounded by your tests. Keep
  gates green on `main` and meaningful.
- **Review merges before pushing.** Every merge is a single `--no-ff` commit,
  easy to inspect and to revert.

## Reporting a vulnerability

Open a private security advisory on this repository's GitHub page rather than a
public issue.
