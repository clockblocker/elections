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

## Analysis contract

The workbench uses `protocol-cloud-clt-v3`. One complete physical UIK protocol is one
observation, and all valid protocols for the selected election and ballot option form a
single nationwide dataset. The model fits a robust central 50% bivariate core to
half-count-corrected turnout and selected-option result logits. It removes the core
members' average finite-count sampling covariance to estimate between-protocol spread,
then adds each target protocol's own finite-count covariance. The actual turnout/result
pair is measured with squared Mahalanobis distance `D2`; the two-degree-of-freedom
chi-square tail `p = exp(-D2 / 2)` defines `P_sus = 1 - p`.

`P_sus` is incompatibility with the fitted election-wide core, not a probability of
fraud. The central limit theorem does not imply that the untransformed nationwide UIK
cloud is Gaussian, and unmodeled geography, electorate composition, protocol type, or
other heterogeneity can produce high scores. Tiny denominators receive wider
finite-count uncertainty. Benjamini–Yekutieli `q` values are retained as a separate
multiple-testing diagnostic; they do not define `P_sus` or the grade.

The display bands are P0 below 95%, P1 from 95%, P2 from 99%, and P3 from 99.9%; invalid
or incomplete protocols are retained as U with an explicit reason. Geography filters
never refit the nationwide score family. See
[`research/METHOD.md`](research/METHOD.md) for the formulas and interpretation limits.
