# Proper election app

The clean application boundary for the election research workbench. It imports the
evidence-linked TypeScript shards in `../proper-data`, serves a Bun API, stores normalized
records in PostgreSQL, renders a React research interface, and keeps the analytical
method independently reproducible in Python.

The first release is deliberately limited to the **2021 State Duma federal party-list
ballot at physical UIKs**. Remote electronic voting (DEG) is outside the model and is
neither merged into nor compared with physical precincts.

## Native setup (no Docker)

Requirements: Bun 1.4+, PostgreSQL 16+, and Python 3.11+.

```sh
cd proper-app
cp .env.example .env
bun install
createdb elections
bun run db:migrate
bun run db:import
bun run dev
```

The repository's crawler and research environment lives at `proper-app/.venv`. To
recreate it:

```sh
python3 -m venv .venv
.venv/bin/pip install -e 'research[crawler,dev]'
```

The React client runs at `http://localhost:5173` and proxies `/api` to the Bun server at
`http://localhost:3001`. PostgreSQL is expected to run as a native OS service.

## Layout

- `backend/` — Bun importer and read-only HTTP API.
- `frontend/` — React turnout/result workbench.
- `db/` — PostgreSQL migrations.
- `research/` — versioned Python reference analysis and methodology.
- `scripts/` — native environment checks.

## Commands

```sh
bun run test
bun run typecheck
bun run build
bun run db:migrate
bun run db:import
.venv/bin/python -m unittest discover -s research/tests -v
```

The import is idempotent and processes one generated shard at a time. The 732 MB source
tree is never loaded as one JavaScript module.
