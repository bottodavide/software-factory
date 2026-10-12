# Software Factory

An autonomous feature pipeline for Claude Code. Type `/factory <feature>` and a
team of agents writes a spec, builds it on its own branch, runs your quality
gates, reviews the change from three angles, and either merges it into `main`
or hands it to you with a precise question.

Agents do the judgement. A small deterministic control script does everything
that must not depend on judgement: state, worktrees, gates, risk detection and
the merge itself. A guard hook keeps agents inside their lane while jobs run.

```
feature
  └─► spec-writer ──► builder ──► quality gates ──► reviewers ──► approver ──► merge to main
                        ▲              │ fail         │ CHANGES      │ ESCALATE
                        └──────────────┴──────────────┘              └─► needs you
                         (at most max_rework_rounds)
```

## Why it is built this way

| Concern | How it is handled |
| --- | --- |
| Agents drifting out of scope | Each agent gets context only from files and has one job. Read-only agents have no write or shell tools at all. |
| "The reviewer said it's fine" | Tests, lint, type-check, build, secret scan and static analysis run as deterministic gates before any reviewer is spawned. A failing gate goes straight back to the builder. |
| Risky changes merged unattended | The control script classifies every change by path and size. Any risk flag (auth, payments, dependencies, CI/deploy, database, secrets, the factory itself, large diffs, deleted tests) makes an automatic merge impossible, whatever the approver says. |
| Reviewing one thing, merging another | The commit reviewed is recorded; the merge is refused if the branch moved afterwards. |
| Agents touching your working copy | Every job runs in its own git worktree outside the repo. While a job is active, the guard hook keeps file edits out of the main checkout and blocks pushes, raw merges, resets, ref changes, credential access and edits to the factory's own guardrails. |
| Agents grading their own work | Subagents cannot write job files or drive the control script: they return documents, and only the orchestrator saves them and changes state. |
| Parallel work | One Claude session per job. Job numbers and backlog items are claimed atomically; ports are allocated dynamically. |
| Visibility | A local dashboard shows every job, every round and every verdict, and renders all the files agents produced. |

## The agents

| Agent | Model | Tools | Produces |
| --- | --- | --- | --- |
| `spec-writer` | opus | Read, Grep, Glob | `spec.md`: requirements, checkable acceptance criteria, assumptions |
| `builder` | opus | Read, Grep, Glob, Bash, Edit, Write | commits on `factory/<job-id>`, `build.md` with evidence per criterion |
| `reviewer-security` | opus | Read, Grep, Glob | `review-security.md` |
| `reviewer-code` | sonnet | Read, Grep, Glob | `review-code.md` |
| `reviewer-experience` | sonnet | Read, Grep, Glob, Bash | `review-experience.md` (UX and visual design; runs the app) |
| `approver` | opus | Read, Grep, Glob | `decision.md`: `APPROVE` or `ESCALATE` |

Agents return their documents as their final message; the orchestrator saves
them. Models are set in each agent's frontmatter.

## Requirements

- Claude Code with subagents and project skills
- git 2.38 or newer, Python 3.10 or newer (standard library only)
- macOS, Linux or WSL (the control script uses POSIX file locks)
- Optional, used automatically when present: `ruff`, `pytest`, `gitleaks`,
  `semgrep`, `pip-audit`, Playwright (for screenshots)

## Install into your repository

1. Copy `.claude/` and `factory/` into your repository root. If you already
   have `.claude/settings.json`, merge the `hooks` and `permissions` blocks.
2. Add the runtime folders to `.gitignore`:
   ```
   /factory/jobs/
   /factory/.backlog.lock
   ```
3. Write an `AGENTS.md` (or `CLAUDE.md`) with at least `## Setup`,
   `## Dev server` and `## Validation` sections. Agents carry nothing
   repo-specific; they read everything from there. See
   [`examples/AGENTS.example.md`](examples/AGENTS.example.md).
4. Commit on `main`, then check readiness:
   ```bash
   python3 factory/bin/factory.py doctor
   ```
5. Start Claude Code in the repository and run `/factory <feature>`.

## Commands

```
/factory <feature>                run a new job through the loop
/factory next                     run the first unchecked item in factory/backlog.md
/factory approve <job-id>         merge a job that is waiting for you
/factory rework <job-id> <note>   send a waiting job back to the builder with your note
/factory abandon <job-id>         drop a job, its branch and worktree
/factory status                   list jobs
```

For parallel work, open several Claude sessions in the same repository and start
a job in each.

The control script can also be used directly:

```
python3 factory/bin/factory.py status | doctor | serve | show <id> | port
```

## Configuration

`factory/config.json` (anything omitted falls back to the defaults in
`factory/bin/factory.py`):

| Key | Default | Meaning |
| --- | --- | --- |
| `main_branch` | `main` | Branch jobs start from and merge into |
| `worktree_root` | `../{repo}-factory` | Where job worktrees go; must be outside the repo |
| `max_rework_rounds` | `2` | Times the builder can be sent back before escalating |
| `dev_host` | `127.0.0.1` | Host for dev servers and the dashboard |
| `reviewers` | `security, code, experience` | Review lenses; each needs a `reviewer-<name>` agent |
| `gates.commands` | `[]` | Explicit gate list; empty means auto-detect |
| `gates.timeout_seconds` | `900` | Per-gate timeout |
| `gates.sast_config` | `p/default` | Ruleset passed to semgrep when installed |
| `risk.max_changed_files` | `20` | Above this, a human decides |
| `risk.max_changed_lines` | `500` | Above this, a human decides |
| `risk.rules` | see script | Path patterns per risk category (`fnmatch`, case-insensitive; `*` spans directories) |

### Quality gates

With `gates.commands` empty, the script detects:

- **Node**: `lint`, `typecheck`/`type-check`, `test` and `build` scripts from
  `package.json`, run with npm, pnpm or yarn according to the lockfile;
  `npm audit` (advisory) when dependencies change.
- **Python**: `ruff check` on changed files and `pytest` when available;
  `pip-audit` (advisory) when dependencies change.
- **Always**: a clean-worktree check and a built-in secret scan of added lines;
  `gitleaks` and `semgrep` when installed.

Blocking gates send the job back to the builder. Advisory gates never block the
loop, but a failure prevents an automatic merge.

Explicit gates replace auto-detection:

```json
"gates": {
  "commands": [
    { "name": "unit", "run": "npm test -- --run" },
    { "name": "types", "cmd": ["npx", "tsc", "--noEmit"] },
    { "name": "audit", "run": "npm audit --audit-level=critical", "blocking": false }
  ]
}
```

Gates must be green on `main`, otherwise every job inherits the failure.

## Dashboard

```bash
python3 factory/bin/factory.py serve            # picks a free port
python3 factory/bin/factory.py serve --port N   # or choose one
```

It binds to `dev_host` only, serves files from `factory/jobs/` read-only, and
rejects requests with an unexpected `Host` header. Leave it open while jobs run.

## Job files

```
factory/jobs/<job-id>/
  job.json                  state, written only by the control script
  spec.md  build.md  decision.md  rework-<k>.md
  round-<N>/
    diff.patch  commits.txt  changes.json  gates.json  risk.json  logs/
    review-security.md  review-code.md  review-experience.md
    screens/                screenshots from the experience reviewer
```

## Security model

Read [`SECURITY.md`](SECURITY.md) before using this on code that matters. In
short: the read-only agents are restricted by their tool lists, the merge gate
is enforced by code, and the guard hook is defense in depth for the agents that
do have a shell. For untrusted repositories, run Claude Code inside a container
or VM with no credentials it does not need.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT. See [`LICENSE`](LICENSE).
