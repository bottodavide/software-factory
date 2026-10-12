---
name: factory
description: Run the Software Factory. Use when the user types /factory <feature>, /factory next, /factory approve <job-id>, /factory rework <job-id> <note>, /factory abandon <job-id> or /factory status. The session that runs it orchestrates one job through spec, build, gates, review, approval and merge.
---

# Software Factory: orchestrator

You orchestrate one job. You never write code, specs, reviews or decisions
yourself: agents produce them and **return them to you as their final message**,
and you save each one to the job folder exactly as returned. Every state change,
evidence pack and merge goes through the control script, never raw git:

```
FX="python3 factory/bin/factory.py"     # run from the main checkout
```

The control script prints JSON. Exit code 2 means a precondition failed (read the
message), exit code 3 means the job was moved to `needs-human` (stop and report).

## The loop

```
feature
  └─► spec-writer ──► builder ──► gates ──► reviewers (parallel) ──► approver ──► merge
                        ▲           │ fail        │ any CHANGES           │ ESCALATE
                        └───────────┴─────────────┘ (max rework rounds)  └─► needs-human
```

| Step     | Who                      | Saved as                                    |
| -------- | ------------------------ | ------------------------------------------- |
| spec     | `spec-writer`            | `spec.md`                                   |
| build    | `builder` (named)        | `build.md` + commits on `factory/<job-id>`  |
| gates    | `$FX evidence`           | `round-N/{gates,risk,changes}.json`, `diff.patch`, `logs/` |
| review   | `reviewer-<lens>` for each lens | `round-N/review-<lens>.md`           |
| approve  | `approver`               | `decision.md`                               |
| merge    | `$FX merge`              | merge commit on the main branch             |

The review lenses are the keys of `reviews` in `job.json` (by default
`security`, `code`, `experience`, from `reviewers` in `factory/config.json`);
each lens has an agent named `reviewer-<lens>`.

`max_rework_rounds` in `factory/config.json` (default 2) caps how many times the
builder is sent back. The script does the counting: `$FX evidence` and `$FX check`
report `rework_allowed`.

## Rules

- The main checkout belongs to the user. Agents work only in the job worktree.
  While any job is active, a guard hook makes the main checkout read-only except
  `factory/jobs/`, and blocks pushes, raw merges and edits to the factory itself.
  If the guard blocks something, do not look for a way around it: report it.
- Agents get context from files, never from this conversation. Pass them only
  the context line below plus the step-specific sentence.
- Save each agent's returned text verbatim with the Write tool. Do not edit,
  summarise or "fix" it. If the first line is not in the required format, send
  the agent one message asking it to return the document again in the required
  format; if it fails twice, set the job to `needs-human`.
- Never push. Never merge with git directly. Never edit `job.json` by hand.
- Never run `$FX merge --human` unless the user typed `/factory approve` for that
  job in this conversation. Agents cannot run the control script at all (except
  `port`) and cannot write job files; the guard enforces both.
- Several jobs can run at once, one per Claude session. You only touch your job.

**Context line** (starts every agent prompt), from the JSON of `$FX init`/`$FX show`:
`Job: <id>. Job folder: <repo>/factory/jobs/<id>/. Worktree: <worktree>. Branch: <branch>. Base: <base>.`

## /factory <feature>

1. **Create.** `$FX init "<feature verbatim>"`. Keep the JSON it prints.
2. **Setup.** In the worktree, run the `## Setup` commands from `AGENTS.md`
   (or `CLAUDE.md`), each as `cd <worktree> && <command>`. On failure:
   `$FX set <id> stage=needs-human "note=setup failed: <short reason>"` and stop.
3. **Spec.** Spawn `spec-writer`: `<context line> Feature: <feature verbatim>`.
   Save its reply to `<job folder>/spec.md`. It must start with `# `.
4. **Build.** `$FX set <id> stage=build`. Spawn `builder` with **name
   `builder-<id>`**: `<context line> Build round 1.` Save its reply to
   `<job folder>/build.md`, then go to **Build check**.

### Build check

First line of `build.md`:
- `STATUS: READY` → **Round N** with N = previous round + 1.
- `STATUS: BLOCKED` → **Needs human**.

### Round N

1. `$FX set <id> stage=review round=<N> reviews=pending`
2. `$FX evidence <id> <N>` runs the quality gates and writes the evidence pack.
3. If `blocking_failed` is true, do **not** spawn reviewers. Mark every lens
   `skipped` (`$FX set <id> reviews.<lens>=skipped …`, one assignment per lens)
   and go to **Rework** with the reason "quality gates failed".
4. Otherwise spawn every `reviewer-<lens>` **in one message** so they run in parallel:
   `<context line> Review round <N>. Evidence: <job folder>/round-<N>/.`
   As each one returns, save its reply to `round-<N>/review-<lens>.md` and run
   `$FX set <id> reviews.<lens>=<PASS|CHANGES>` straight away.
5. **Wait for all.** `$FX check <id> <N>` must report `"complete": true` before
   you decide anything. Never resume the builder mid-round.
6. Decide: `all_pass` true → **Approve**; otherwise → **Rework**.

### Rework

- If `rework_allowed` is true (from `$FX evidence` or `$FX check`):
  `$FX set <id> stage=build`, then resume the same
  builder with `SendMessage` to `builder-<id>`:
  `Round <N> found problems. Read <job folder>/round-<N>/ (gates.json, logs/ and every review with VERDICT: CHANGES), fix them, commit, and return the full build.md for round <N+1>.`
  If `SendMessage` is unavailable, spawn a fresh `builder-<id>` with the same
  sentence after the context line. Save the reply to `build.md` → **Build check**.
- Otherwise write `decision.md` yourself:
  `ESCALATE` + blank line + `Review rounds exhausted. Outstanding problems in round-<N>/: <gates and/or reviewers>.`
  → **Needs human**.

### Approve

`$FX set <id> stage=approve`. Spawn `approver`:
`<context line> Final round: <N>. Evidence: <job folder>/round-<N>/.`
Save its reply to `decision.md`. First line:
- `APPROVE` → `$FX merge <id>`. Exit 0: merged. Exit 3: the script found a reason
  a human must look (risk flag, moved branch, dirty main checkout, conflict) and
  already set `needs-human`; report its reason.
- `ESCALATE` → **Needs human**.

### Needs human

`$FX set <id> stage=needs-human` (if not already). Tell the user the job id, the
worktree path, and quote the reason: the `## For the human` section of
`decision.md`, the `## Blocked` section of `build.md`, or the script's message.

## /factory next

`$FX next` claims the first unchecked backlog item and creates its job (it is
step 1 above). Continue from step 2 with the JSON it prints. If it reports no
unchecked items, say so and stop.

## /factory approve <job-id>

The user has read the job and accepts it, including any risk flags.
`$FX merge <job-id> --human`. The script refuses unless the job is `needs-human`,
and still refuses if blocking gates failed, the branch moved since review, the
worktree is dirty, the main checkout is busy, or there is a conflict.

## /factory rework <job-id> <note>

The job must be `needs-human`.
1. Save the note verbatim to `<job folder>/rework-<k>.md` (`k` = next unused number).
2. `$FX set <id> stage=build rework_base=<N>` (`N` = the job's last round). This
   gives the rework a fresh budget of `max_rework_rounds`.
3. Spawn a fresh `builder` named `builder-<id>`:
   `<context line> Rework: read <job folder>/rework-<k>.md and the job history, address the note, commit, and return the full build.md for round <N+1>.`
4. Continue from **Build check**.

## /factory abandon <job-id>

Confirm with the user first; this deletes the branch and worktree.
Then `$FX abandon <job-id>`.

## /factory status

`$FX status`, then point the user to the dashboard: `python3 factory/bin/factory.py serve`.

## Reporting

One short line per transition, for example
`004-export-csv: round 2 · gates PASS · security PASS, code CHANGES, experience PASS → rework`.
When the job ends, say whether it merged or needs a human, and why.
