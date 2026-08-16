# Interactive Shpilkin analysis

An evidence-first local workbench for exploring both 2021 Russian State Duma ballots at UIK level. It combines reproducible party-list and single-member source pipelines, a MySQL/SQLAlchemy model, district reconciliation reports, a read-only FastAPI API, and a React/WebGL scatterplot with drill-through and exports.

See [WAYFINDER.md](WAYFINDER.md) for the delivery map and [docs/data-sources.md](docs/data-sources.md) for source provenance and limitations.

## Start the workbench

Prerequisites: Docker Engine with Compose v2 and enough disk for MySQL plus roughly 50 MB of preserved source archives.

```sh
cp .env.example .env
docker compose up --build
```

The browser opens at <http://localhost:45173>, FastAPI at <http://localhost:8000>, and OpenAPI at <http://localhost:8000/docs>. MySQL data persists in the `mysql-data` volume. Stop services with `docker compose down`; add `-v` only when you intentionally want to erase the local database.

Health checks gate startup in dependency order: MySQL, migrated FastAPI, then Vite. `make dev`, `make down`, `make rebuild-db`, `make test`, and `make validate` provide short aliases.

## Rebuild the 2021 dataset

The committed manifest records exact URLs, byte sizes, and SHA-256 checksums. Archives download into ignored `data/raw/`; generated audit reports go to ignored `reports/generated/`.

First provide `data/published-single-member-totals-2021.json`, a local transcription of
the final official OIK protocols. It is intentionally ignored and must contain sourced
candidate totals plus exactly one declared winner for each of all 225 districts. See
[the pipeline reference schema](docs/data-pipeline.md#2021-data-pipeline). Then run the
commands below, or run `make rebuild-data`.

```sh
docker compose run --rm api elections-data migrate
docker compose run --rm api elections-data download
docker compose run --rm api elections-data acquire-single-member
docker compose run --rm api elections-data import-results
docker compose run --rm api elections-data import-single-member
docker compose run --rm api elections-data import-commissions
docker compose run --rm api elections-data resolve-gas-ids
docker compose run --rm api elections-data match
docker compose run --rm api elections-data validate \
  --published-totals data/published-totals-2021.json \
  --single-member-published-totals data/published-single-member-totals-2021.json
docker compose run --rm api elections-data verify-complete
```

Each import is deterministic and reports accepted/rejected counts. Validation writes
enumerated discrepancies, all 225 district outcomes, and separate DEG aggregates.
`verify-complete` re-hashes the source snapshot, writes
`reports/generated/complete-dataset-2021.json`, and exits nonzero until both ballot kinds,
all references, all 225 OIKs, matches, rejects, validations, and source gaps are ready.

## Native development

Use Python 3.11+, Node.js 22+, and MySQL 8.4 (SQLite is supported for tests and a lightweight local database).

```sh
python -m venv backend/.venv
backend/.venv/bin/pip install -e 'backend[dev]'
cd frontend && npm ci
```

Set `DATABASE_URL`, run `backend/.venv/bin/elections-data migrate`, then start the API with `uvicorn elections.api.app:app --app-dir backend --reload`. Start the client from `frontend/` with `npm run dev`. Set `VITE_API_URL` when FastAPI is not at `http://localhost:8000`.

## Verification

```sh
cd backend && .venv/bin/pytest && .venv/bin/ruff check elections tests
cd ../frontend && npm test -- --run && npm run build
```

Large raw inputs, database files, and generated exports are deliberately not committed. The UI excludes phone numbers and personal email addresses from commission metadata.
