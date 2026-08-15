# Interactive Shpilkin analysis

An evidence-first local workbench for exploring 2021 Russian State Duma party-list results at UIK level. It combines a reproducible source pipeline, a MySQL/SQLAlchemy model, validation and matching reports, a read-only FastAPI API, and a React/WebGL scatterplot with drill-through and exports.

See [WAYFINDER.md](WAYFINDER.md) for the delivery map and [docs/data-sources.md](docs/data-sources.md) for source provenance and limitations.

## Start the workbench

Prerequisites: Docker Engine with Compose v2 and enough disk for MySQL plus roughly 50 MB of preserved source archives.

```sh
cp .env.example .env
docker compose up --build
```

The browser opens at <http://localhost:5173>, FastAPI at <http://localhost:8000>, and OpenAPI at <http://localhost:8000/docs>. MySQL data persists in the `mysql-data` volume. Stop services with `docker compose down`; add `-v` only when you intentionally want to erase the local database.

Health checks gate startup in dependency order: MySQL, migrated FastAPI, then Vite. `make dev`, `make down`, `make rebuild-db`, `make test`, and `make validate` provide short aliases.

## Rebuild the 2021 dataset

The committed manifest records exact URLs, byte sizes, and SHA-256 checksums. Archives download into ignored `data/raw/`; generated audit reports go to ignored `reports/generated/`.

```sh
docker compose run --rm api elections-data download
docker compose run --rm api elections-data import-results
docker compose run --rm api elections-data import-commissions
docker compose run --rm api elections-data match
docker compose run --rm api elections-data validate \
  --published-totals data/published-totals-2021.json \
  --ballot-id 1
```

Each import is deterministic and reports accepted/rejected counts. Matching preserves every candidate and its evidence; ambiguous records are not silently selected. Validation writes enumerated discrepancies and separate DEG aggregates. The UI does not claim reconciliation until published totals have been loaded and validation finishes without errors.

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
