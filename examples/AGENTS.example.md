# <Project name>

One paragraph: what this project is, who uses it, and the main stack
(for example "Node 22 + TypeScript API with a React front end, PostgreSQL").

## Setup

Commands that prepare a fresh checkout. The orchestrator runs them in every new
job worktree, so they must work without prompts.

```bash
npm ci
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env.local   # test values only, never real credentials
```

## Dev server

How to start the app on a given host and port. Agents get a free port from
`python3 factory/bin/factory.py port` and substitute `{host}` and `{port}`.

```bash
npm run dev -- --host {host} --port {port} --strictPort
# or: .venv/bin/python -m uvicorn app.main:app --host {host} --port {port}
```

Backends the dev server must use: local database or test containers, payment
provider in test mode, email disabled. Never point agents at production.

## Screenshots

Optional. A command that saves a screenshot of a URL at a given viewport:

```bash
npx --no-install playwright screenshot --viewport-size={width},{height} {url} {file}
```

## Validation

The commands that must pass before a change is done. Keep them green on `main`.

```bash
npm run lint
npm run typecheck
npm test -- --run
npm run build
.venv/bin/python -m pytest -q
```

## Conventions

- Code style, naming, folder structure, how errors are handled.
- Where shared components, helpers and design tokens live.
- Commit messages: short, imperative, one line.

## Sensitive areas

Paths an agent must not change unless the spec explicitly says so, and that
should always reach a human: authentication, billing, database migrations,
infrastructure. Mirror them in `risk.rules` in `factory/config.json`.
