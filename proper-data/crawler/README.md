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
`--backoff-initial`, `--backoff-max`, and `--jitter`. `HTTP_PROXY` and `HTTPS_PROXY`
are honored by `urllib`. Never place proxy credentials or access keys in this repo,
commands copied into it, logs, fixtures, or generated files.

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
npm exec tsc -- --project proper-data/tsconfig.json --noEmit
git diff --check
git grep -nEi '(https?://[^/[:space:]]+:[^/@[:space:]]+@|BEGIN (RSA |OPENSSH )?PRIVATE KEY|api[_-]?key|proxy[^[:space:]]*password)' -- proper-data/crawler proper-data/2021-duma
```

Generate twice into temporary directories and compare byte-for-byte for a full
determinism check. Unit tests use fake clocks and local fixtures, never the live archive.

The older focused tools (`extract_tree.py`, `probe_matrix.py`, `cdx_lookup.py`,
`fetch_cdx.py`, both decoders, and `export_uik_results.py`) remain useful for individual
pages. `duma2021.py` is the nationwide entry point.
