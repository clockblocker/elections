# House-to-polling-address map

This workspace builds a time-versioned, evidence-preserving map from Russian
building addresses to UIKs and physical polling-place addresses. It does not
contact election authorities for a bulk export. Instead it combines:

1. the public CEC address classifier;
2. the official GAR/FIAS building-address universe;
3. independently hosted regional and archived classifiers; and
4. official precinct-boundary documents for later gap filling.

The checked-in code contains no downloaded personal data. Apartments and
rooms are intentionally out of scope until evidence shows that a building can
be split between UIKs.

The immediate product target is a user-facing, 2026-only address lookup. Its
backend resolves a supplied address through the live CEC gateway, pins the 2026
State Duma election, and joins the returned `(subjectRf, UIK number)` to the
repository's 2026 UIK voting-address dataset. The endpoint contract and failure
semantics are recorded in
[`docs/2026-user-address-endpoint.md`](docs/2026-user-address-endpoint.md).

## What is implemented

- Streaming GAR XML/ZIP ingestion with a bounded-memory SQLite hierarchy index.
- Canonical, deterministic GAR building identities.
- A configurable archived 2019–2023 CEC JSON lookup adapter.
- A live 2026 CEC gateway probe with SOCKS/HTTP proxy support and ephemeral
  challenge authentication.
- The recovered 2016–2018 CEC hierarchical classifier adapter.
- A resumable legacy-tree crawler that preserves every response verbatim.
- Time-versioned address-to-UIK evidence and separate commission/voting addresses.
- Content-addressed raw response storage and coverage reporting.

## Bootstrap

Python 3.11 or newer is sufficient. The live probe uses `requests[socks]` for
the same proxy route as the repository's other CEC crawlers.

```sh
cd house-polling-address-map-workspace
python3 -m venv .venv
.venv/bin/pip install -e .
house-polling-map --database data/map.sqlite3 --raw-dir data/raw init
```

Import an official GAR export, optionally one region at a time:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  import-gar /path/to/gar.zip --region 54 --index-database data/gar-54.sqlite3
```

Inspect coverage:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw coverage
house-polling-map --database data/map.sqlite3 --raw-dir data/raw coverage --region 54
```

Export the latest resolved mappings without losing their evidence pointers:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  export-jsonl data/address-to-polling-place.jsonl
```

Probe a captured current CEC JSON protocol without discarding the raw response:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  probe-cec protocol.json 'Новосибирская область, Новосибирск, Красный проспект, 18'
```

Probe the verified 2026 gateway. `PROPER_DATA_PROXY_URL` is read from the
environment or the repository's ignored `.env`; pass `--proxy-url` to override
it. Public response bodies are preserved, while challenge responses containing
the temporary API key are deliberately excluded:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  probe-cec-2026 protocols/cec-2026.json \
  'Приморский край Владивосток Советский район проспект 100-летия Владивостока дом 100 В квартира 1'
```

This verifies address → 2026 election → UIK number. The federal gateway's
commission-organization result was empty in the initial canaries, so a regional
or documentary source is still required for many physical voting-room addresses.

Resolve a bounded batch of imported GAR buildings (start with a canary):

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  resolve-gar protocols/cec-2019-2023.json --region 54 --limit 25 --delay 1
```

Each invocation visits an address at most once. A later invocation retries
addresses whose newest outcome is `failed`; resolved, ambiguous, and no-match
outcomes are not silently repeated.

The `resolve-gar` recipe is recovered from CEC's archived 2019–2023 first-party
JavaScript and is no longer the current live protocol. Use `probe-cec-2026` for
live canaries; the 2026 probe is not yet wired into the nationwide batch
resolver.

The protocol file makes the unstable HTTP request shape explicit:

```json
{
  "suggest_url": "https://cec-host.example/address/search/{query}/",
  "method": "GET",
  "headers": {"Accept": "application/json"},
  "static_parameters": {},
  "resolve_url": "https://cec-host.example/committee/address/{address_id}",
  "committee_url": "https://cec-host.example/committee/subjcode/{region_code}/num/{committee_number}"
}
```

Do not substitute the 2026 URLs into this older schema: the live protocol now
uses GraphQL plus a separate authenticated election gateway.

Resume the enumerable legacy tree against a reachable origin or replay server:

```sh
house-polling-map --database data/map.sqlite3 --raw-dir data/raw \
  crawl-legacy-tree --crawl-id cec-2018 \
  --base-url http://www.cikrf.ru --request-limit 100 --delay 1
```

Remove `--request-limit` only after inspecting a successful canary. Completed
tree and result requests are not fetched again when the command is restarted.

## Verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Data invariants

- `region_code + UIK number` is the stable station key used by this project.
- A polling-place address is not silently replaced with a commission address.
- Failed, ambiguous and no-match resolutions are first-class outcomes.
- Every resolved mapping points to a preserved source response and retrieval time.
- Address/UIK assignments are observations, not timeless facts.

See [the recovered CEC protocol](docs/cec-lookup-protocol.md) for the historical
request sequence and its evidentiary limits.
