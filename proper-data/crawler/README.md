# Official GAS 2021 State Duma crawler

This is the operator guide for the file-first nationwide crawl of official 2021 State
Duma protocols. The crawler does not use `2021.csv.zip`, third-party result data,
commission personnel data, a database, or arithmetically guessed IDs.

The primary historical origin is `http://old.izbirkom.ru`. Failure from
`www.vybory.izbirkom.ru` is not treated as a missing archive. Wayback is an explicit
fallback; every observation records `live-official` or `wayback` provenance.

## Scope and promotion gate

The election VRN is `100100225883172`.

| Type | Page contents | Output |
| --- | --- | --- |
| 233 | party table, UIKs in columns below one TIK | `tic/233`, extracted `uik/242` |
| 242 | direct party protocol for one UIK | `uik/242` |
| 464 | candidate table, UIKs in columns below one TIK | `tic/464`, extracted `uik/463` |
| 463 | direct candidate protocol for one UIK | `uik/463` |

The number is never trusted alone. Promotion requires a decoded protocol table,
accounting rows, the expected UIK-column/direct shape, the right party/candidate
contents, the Duma VRN, identities supported by hierarchy evidence, plausible
dimensions, and successful reconciliation where the official tables permit it.
Navigation, challenges, errors, malformed pages, duplicate conflicts, and mismatched
ballots are rejected or reported; conflicts are never silently resolved.

## Storage, rate, and restart guarantees

The default ignored raw root is:

```text
data/raw/gas-duma-2021/manifest.json            atomic request checkpoint
data/raw/gas-duma-2021/sha256/ab/<full-hash>    exact response bytes
```

The manifest records requested/final URL, retrieval time, status, content type, byte
length, SHA-256, elapsed time, retry count, source host, provenance, and failures.
Bodies and indexes are published atomically. A restart verifies the indexed body hash
before skipping it; `--refresh` overrides this. Error responses remain observations.

All workers and retries share one monotonic scheduler. At the default 10 RPS launches
are about 100 ms apart, not burst once per second. Concurrency defaults to six. 429,
502, 503, 504, timeouts, and connection failures receive capped exponential jitter;
`Retry-After` is honored and unhealthy-server delay pauses the scheduler globally.
Permanent 4xx responses are not retried indefinitely.

Every network command accepts `--rate`, `--concurrency`, `--timeout`, `--retries`,
`--backoff-initial`, `--backoff-max`, and `--jitter`. All crawler requests share a
browser-compatible request profile. Set `PROPER_DATA_PROXY_URL` in the ignored root
`.env` (or export it in the process environment) to route every HTTP and HTTPS request
through one HTTP or SOCKS proxy; use `socks5h://` when DNS must also traverse the
proxy. Never place proxy credentials or access keys in tracked files, logs, fixtures,
or generated files.

## 1. Discover exact hierarchy IDs

The saved official nationwide page contains the CEC and federal-subject entry points.
Descendants come from the official endpoint:

```text
/region/izbirkom?action=tvdTree&tvdchildren=true&vrn=100100225883172&tvd=<exact-tvd>
```

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py discover \
  --root-html data/raw/duma-2021-single-member-cec/index/a52134a1b6d1e88209d6.html \
  --output reports/generated/gas-duma-2021/hierarchy.json
```

Use `--region 22 --region 93` for a smaller discovery. Output includes region/TIK/UIK
counts, exact UIK-to-TIK relations, and unresolved load-on-demand nodes.

## 2. Non-mutating dry run

The normal plan uses two TIK-column requests per TIK. This supplies every UIK while
marking its derivation `extracted-tic-column`. `--direct-uik` also plans direct UIK
protocols as duplicate official observations for stronger reconciliation.

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py plan \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --rate 10 --concurrency 6 \
  --output reports/generated/gas-duma-2021/plan.json
```

It performs no network work. It prints hierarchy counts, request classes and estimate,
rate/concurrency, raw and manifest paths, resume command, and output layout. Do not
start a national run while `hierarchy.complete` is false.

## 3. Multi-region probe

The checked-in probe covers Crimea and Altai Krai, multiple TIKs, both ballots, and a
direct UIK page. Preserved live pages retain their original timestamp; exact Wayback
fallbacks are marked explicitly.

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py probe \
  --spec proper-data/crawler/probe-2021-duma.json \
  --raw-dir data/raw/gas-duma-2021-probe \
  --report reports/generated/gas-duma-2021/probe.json
```

Require `"success": true`. Manually inspect a party table, candidate table, and direct
UIK page before the nationwide run.

## 4. Start, resume, and retry

Start and resume are the same idempotent command:

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py crawl \
  --plan reports/generated/gas-duma-2021/plan.json \
  --raw-dir data/raw/gas-duma-2021 \
  --report reports/generated/gas-duma-2021/crawl.json
```

Retry only absent, failed, or non-2xx observations:

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py crawl \
  --plan reports/generated/gas-duma-2021/plan.json \
  --raw-dir data/raw/gas-duma-2021 --only-failures \
  --report reports/generated/gas-duma-2021/retry.json
```

Progress emits completed/total, effective rolling RPS, status counts, retries, valid
tables, and ETA. The raw manifest is the checkpoint after interruption.

## 5. Offline generation and validation

Decode, transpose, pair the two ballots, and reconcile UIK sums to TIK totals without
refetching:

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py build \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --crawl-report reports/generated/gas-duma-2021/crawl.json \
  --raw-dir data/raw/gas-duma-2021 \
  --output reports/generated/gas-duma-2021/protocols.json
```

Then regenerate TypeScript offline:

```sh
backend/.venv/bin/python proper-data/crawler/generate_typescript.py \
  reports/generated/gas-duma-2021/protocols.json \
  --output proper-data/2021-duma
```

The generator sorts deterministically, writes atomically, removes stale generated
files, emits legal identifiers and autogenerated warnings, and retains `direct` versus
`extracted-tic-column`. The logical layout is:

```text
proper-data/2021-duma/uik-to-tik.ts
proper-data/2021-duma/protocol/types.ts
proper-data/2021-duma/protocol/tic/{233,464}/{id}.ts
proper-data/2021-duma/protocol/uik/{242,463}/{id}.ts
```

Create the machine-readable regional coverage report:

```sh
backend/.venv/bin/python proper-data/crawler/duma2021.py validate \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --crawl-report reports/generated/gas-duma-2021/crawl.json \
  --protocols reports/generated/gas-duma-2021/protocols.json \
  --output reports/generated/gas-duma-2021/coverage.json
```

It reports discovered TIK/UIK counts, party/candidate UIK coverage, known-TIK coverage,
missing types, failed URLs/error classes, live/Wayback counts, and reconciliation status.

## Required checks before commit

```sh
backend/.venv/bin/python -m unittest discover -s proper-data/crawler/tests -v
backend/.venv/bin/ruff check proper-data/crawler
frontend/node_modules/.bin/tsc --project proper-data/tsconfig.json --noEmit
git diff --check
git grep -nEi '(https?://[^/[:space:]]+:[^/@[:space:]]+@|BEGIN (RSA |OPENSSH )?PRIVATE KEY|api[_-]?key|proxy[^[:space:]]*password)' -- proper-data/crawler proper-data/2021-duma
```

Generate twice into temporary directories and compare byte-for-byte for a full
determinism check. Unit tests use fake clocks and local fixtures, never the live archive.

The older focused tools (`extract_tree.py`, `probe_matrix.py`, `cdx_lookup.py`,
`fetch_cdx.py`, both decoders, and `export_uik_results.py`) remain useful for individual
pages. `duma2021.py` is the nationwide entry point.

## Historical Duma reconnaissance (1993–2016)

`historical.py` is the controlled, non-nationwide entry point for 1993, 1995, 1999,
2003, 2007, 2011, and 2016. It shares `data/raw/.gas-rate-limit/` with `duma2021.py`, so
separate processes reserve smoothly spaced slots from the same monotonic schedule.
The OS releases the file lock when a process dies; a terminated process can waste one
slot but cannot strand the lock or create a burst.

Always inspect a dry run before network access. Discover the checked-in inventory and
show exact archived-official entry URLs:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py plan \
  --source-mode archive-only --rate 10 --concurrency 2 \
  --output reports/generated/gas-duma-history/archive-entry-plan.json
```

Probe one election or all known elections. The same command resumes from verified raw
hashes after interruption:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py probe \
  --year 2011 --source-mode archive-only \
  --report reports/generated/gas-duma-history/2011-entry.json

backend/.venv/bin/python proper-data/crawler/historical.py probe \
  --source-mode archive-only \
  --report reports/generated/gas-duma-history/all-entries.json
```

Use `--source-mode live-only` to prohibit Wayback or `archive-fallback` to plan both
classes. A live-host timeout is an observation about that route, not proof that the
election is absent.

The checked-in controlled matrix selects exact national, region, OIK, TIK, hierarchy,
static, and workbook requests. `probe-spec` honors `--source-mode`; exact live-only
hierarchy endpoints are omitted from archive-only plans when no capture has been
verified. To choose different regions or TIKs, copy the JSON and retain only exact
evidence-derived URLs. Never manufacture TVDs arithmetically.

```sh
backend/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-core-probes.json --dry-run

backend/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-core-probes.json
```

Resume with the second command. Retry only absent, failed, or non-2xx observations:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json --source-mode live-only \
  --raw-dir data/raw/gas-duma-history-live --only-failures \
  --report reports/generated/gas-duma-history/core-retry.json
```

After hierarchy probes succeed, build the deterministic protocol sample spec offline.
The checked-in selection registry chooses Kamchatka and Moscow, two TIKs per region,
every applicable contest, and up to three UIKs per TIK (all UIKs when fewer exist):

```sh
backend/.venv/bin/python proper-data/crawler/historical.py make-sample-spec \
  --hierarchy-report reports/generated/gas-duma-history/live-core-probes.json \
  --selections proper-data/crawler/historical-protocol-selections.json \
  --output reports/generated/gas-duma-history/live-protocol-probe-spec.json

backend/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec reports/generated/gas-duma-history/live-protocol-probe-spec.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-protocol-probes.json --dry-run

backend/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec reports/generated/gas-duma-history/live-protocol-probe-spec.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-protocol-probes.json
```

Resume with the same command. Retry only failed identities with `--only-failures`.
Validate election/type identity, hierarchy versus UIK headers, aggregate arithmetic,
and direct UIK/TIK rows against the paired aggregate report without network access:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py validate-samples \
  --probe-report reports/generated/gas-duma-history/live-protocol-probes.json \
  --output reports/generated/gas-duma-history/live-protocol-validation.json
```

Decode or classify preserved responses with no network access. Add
`--direct-protocol` for a verified leaf/TIK protocol table:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py classify \
  --input data/raw/gas-duma-history-live/sha256/84/84891d562e218ea23c2e0bbc1a58a6b3517d36077b6104fc75f1c0bef82238f8 \
  --direct-protocol \
  --output reports/generated/gas-duma-history/decoded-2011-aleut.json
```

Generate only verified sample TypeScript protocols, then validate the checked-in
availability matrix and evidence hashes:

```sh
backend/.venv/bin/python proper-data/crawler/historical.py generate-samples \
  --probe-report reports/generated/gas-duma-history/live-protocol-probes.json \
  --output-root proper-data

backend/.venv/bin/python proper-data/crawler/historical.py validate-matrix \
  --matrix proper-data/historical-duma-availability.json
```

The human summary is `proper-data/HISTORICAL-DUMA-AVAILABILITY.md`; the JSON matrix is
authoritative for readiness, missing elements, report semantics, and evidence hashes.
