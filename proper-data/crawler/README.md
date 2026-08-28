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
| 220 | official OIK candidate registry | `districts.ts`, `districts/region-*` |
| CEC Resolution 61/467-8 appendix | immutable 24 Sep 2021 list of 225 winners | district `isElected` and `winnerSource` |
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
data/raw/gas-duma-2021/manifest.json            compacted request index
data/raw/gas-duma-2021/manifest.journal.ndjson  per-response checkpoints
data/raw/gas-duma-2021/sha256/ab/<full-hash>    exact response bytes
```

The index records requested/final URL, retrieval time, status, content type, byte
length, SHA-256, elapsed time, retry count, source host, provenance, and failures.
Each observation is appended to an fsynced journal; the journal is periodically
compacted into an atomically replaced manifest. A restart replays both and verifies
the indexed body hash before skipping it; `--refresh` overrides this. Error responses
remain observations.

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
proper-app/.venv/bin/python proper-data/crawler/duma2021.py discover \
  --root-html data/raw/duma-2021-single-member-cec/index/a52134a1b6d1e88209d6.html \
  --output reports/generated/gas-duma-2021/hierarchy.json
```

Use `--region 22 --region 93` for a smaller discovery. Output includes the complete
region → district/OIK → TIK → UIK topology, commission names and TVDs, and
unresolved load-on-demand nodes. Discovery then fetches each TIK's official
navigation page, requires its one exact `ОИК №…` breadcrumb, follows one such
link per OIK, and extracts the exact type 220 candidate-registry link from that OIK
page. Types 233 and 464 also come from exact official navigation. Require `complete`,
`report_links_complete`, and `candidate_registry_links_complete` to be true.

## 2. Non-mutating dry run

The normal plan uses two TIK-column requests per TIK, one type 220 registry per
district, and the archived official appendix to CEC Resolution 61/467-8 (5,944
requests nationally: 5,718 results, 225 registries, and one immutable winner source).
This supplies every UIK while marking its derivation `extracted-tic-column`.
`--direct-uik` also plans direct UIK protocols as duplicate official observations for
stronger reconciliation.

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py plan \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --rate 10 --concurrency 6 \
  --output reports/generated/gas-duma-2021/plan.json
```

It performs no network work. It prints hierarchy counts, request classes and estimate,
rate/concurrency, raw and manifest paths, resume command, and output layout. Do not
start a national run while `ready` is false or `url_sources` contains `synthesized`.

## 3. Multi-region probe

The checked-in probe covers Crimea and Altai Krai, multiple TIKs, both ballots, and a
direct UIK page. Preserved live pages retain their original timestamp; exact Wayback
fallbacks are marked explicitly.

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py probe \
  --spec proper-data/crawler/probe-2021-duma.json \
  --raw-dir data/raw/gas-duma-2021-probe \
  --report reports/generated/gas-duma-2021/probe.json
```

Require `"success": true`. Manually inspect a party table, candidate table, and direct
UIK page before the nationwide run.

## 4. Start, resume, and retry

Start and resume are the same idempotent command:

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py crawl \
  --plan reports/generated/gas-duma-2021/plan.json \
  --raw-dir data/raw/gas-duma-2021 \
  --report reports/generated/gas-duma-2021/crawl.json
```

Retry only absent, failed, or non-2xx observations:

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py crawl \
  --plan reports/generated/gas-duma-2021/plan.json \
  --raw-dir data/raw/gas-duma-2021 --only-failures \
  --report reports/generated/gas-duma-2021/retry.json
```

Progress emits completed/total, effective rolling RPS, status counts, retries, valid
tables, and ETA. The raw manifest is the checkpoint after interruption.

## 5. Offline generation and validation

Decode, transpose, pair the two ballots, reconcile UIK sums to TIK totals, and join
single-member result labels to registered official candidates by district and exact
normalized full name without refetching:

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py build \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --crawl-report reports/generated/gas-duma-2021/crawl.json \
  --raw-dir data/raw/gas-duma-2021 \
  --output reports/generated/gas-duma-2021/protocols.json
```

If the build reports hierarchy UIKs absent from a TIK table, fetch their exact direct
protocol pages. Rebuild with that report so agreeing direct observations gain direct
provenance and official error documents appear in final coverage.

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py recover-gaps \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --protocols reports/generated/gas-duma-2021/protocols.json \
  --raw-dir data/raw/gas-duma-2021 \
  --report reports/generated/gas-duma-2021/direct-recovery.json

proper-app/.venv/bin/python proper-data/crawler/duma2021.py build \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --crawl-report reports/generated/gas-duma-2021/crawl.json \
  --additional-crawl-report reports/generated/gas-duma-2021/direct-recovery.json \
  --raw-dir data/raw/gas-duma-2021 \
  --output reports/generated/gas-duma-2021/protocols.json
```

Then regenerate TypeScript offline:

```sh
proper-app/.venv/bin/python proper-data/crawler/generate_typescript.py \
  reports/generated/gas-duma-2021/protocols.json \
  --output proper-data/2021-duma --shard-by-region --shard-size 250
```

The generator sorts deterministically, writes atomically, removes stale generated
files, emits legal identifiers and autogenerated warnings, and retains `direct` versus
`extracted-tic-column`. Single-member vote maps use candidate `vibid` keys, while the
separate catalog supplies names, nominating entities, and registration status. The
raw build report also retains the live type 220 mandate status for auditing. Because
that status changes when mandates are vacated or later reassigned, published
`isElected` is joined from the immutable 24 September 2021 CEC resolution; every
district retains both independently hashed sources. Nationwide
output uses deterministic regional batches to stay within TypeScript's union-size
limit. The logical layout is:

```text
proper-data/2021-duma/uik-to-tik.ts
proper-data/2021-duma/uik-to-tik/region-{region}.ts
proper-data/2021-duma/districts.ts
proper-data/2021-duma/districts/region-{region}.ts
proper-data/2021-duma/protocol/types.ts
proper-data/2021-duma/protocol/tic/{233,464}/region-{region}.ts
proper-data/2021-duma/protocol/uik/{242,463}/region-{region}-part-{batch}.ts
```

Generation refuses to publish 2021 Duma files unless all district gates pass:

1. exactly 225 unique district numbers `1…225` and OIK TVDs;
2. every TIK and UIK has one region and district/OIK assignment;
3. every result candidate matches one registered type 220 candidate;
4. every district has exactly one official election-date winner from CEC Resolution
   61/467-8, matched to one registered type 220 candidate;
5. that official winner equals the unique highest district vote total; and
6. every type 220 source is present and SHA-256 hashed with live/Wayback provenance.

The winner gate also requires the official CEC appendix itself to be present, hashed,
and parsed as a conflict-free set of districts `1…225`; generation records its
Wayback provenance in each district's `source.winnerSource`.

Create the machine-readable regional coverage report:

```sh
proper-app/.venv/bin/python proper-data/crawler/duma2021.py validate \
  --hierarchy reports/generated/gas-duma-2021/hierarchy.json \
  --crawl-report reports/generated/gas-duma-2021/crawl.json \
  --additional-crawl-report reports/generated/gas-duma-2021/direct-recovery.json \
  --protocols reports/generated/gas-duma-2021/protocols.json \
  --output proper-data/2021-duma/coverage.json
```

It reports discovered TIK/UIK counts, party/candidate UIK coverage, known-TIK coverage,
missing types, failed URLs/error classes, live/Wayback counts, and reconciliation status.

## Required checks before commit

```sh
proper-app/.venv/bin/python -m unittest discover -s proper-data/crawler/tests -v
proper-app/.venv/bin/ruff check proper-data/crawler
proper-app/frontend/node_modules/.bin/tsc --project proper-data/tsconfig.json --noEmit
git diff --check
git grep -nEi '(https?://[^/[:space:]]+:[^/@[:space:]]+@|BEGIN (RSA |OPENSSH )?PRIVATE KEY|api[_-]?key|proxy[^[:space:]]*password)' -- proper-data/crawler proper-data/2021-duma
```

Generate into a temporary directory with the same shard flags and compare the generated
protocols, relation shards, and root mapping byte-for-byte. Unit tests use fake clocks
and local fixtures, never the live archive.

Use `--request-class election-winners-docx` to replay only the immutable winner
request into a separate report. In that split-report workflow, pass that report once
with `--additional-crawl-report` to `build` and `validate`; the normal national crawl
already includes it and must not add it twice.

## Completed national run (2026-08-28)

The current checkpoint discovered the complete official 85-region → 225-district/OIK
→ 2,859-TIK → 96,323-UIK hierarchy. All 5,718 TIK result pages and all 225 type 220
registries returned HTTP 200; the registries contain 2,296 candidates. The archived
official CEC appendix contains all 225 election-date winners and has SHA-256
`b8744c5796c6d5ee6ca05421a0d66aeb4f0098271a4039aea290c60af29ea20e`.
All six district gates pass, with zero reconciliation or duplicate conflicts.
Generated coverage is 96,307 UIKs with both ballots and 96,309 with candidate results.
Sixteen hierarchy UIKs across nine regions are absent from at least one TIK table;
their 32 direct URLs produced two agreeing candidate protocols and 30 official error
documents, all preserved and listed in `proper-data/2021-duma/coverage.json`.

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
proper-app/.venv/bin/python proper-data/crawler/historical.py plan \
  --source-mode archive-only --rate 10 --concurrency 2 \
  --output reports/generated/gas-duma-history/archive-entry-plan.json
```

Probe one election or all known elections. The same command resumes from verified raw
hashes after interruption:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py probe \
  --year 2011 --source-mode archive-only \
  --report reports/generated/gas-duma-history/2011-entry.json

proper-app/.venv/bin/python proper-data/crawler/historical.py probe \
  --source-mode archive-only \
  --report reports/generated/gas-duma-history/all-entries.json
```

Use `--source-mode live-only` to prohibit Wayback or `archive-fallback` to plan both
classes. A live-host timeout is an observation about that route, not proof that the
election is absent.

## Nationwide historical Duma crawls (2003–2016)

`historical_nationwide.py` promotes the verified 2003, 2007, 2011, and 2016 formats
into the same restartable hierarchy → plan → crawl → build → validate workflow used
for 2021. Election identities and the verified report-type semantics are checked in
`historical-nationwide.json`. Every request uses the shared global rate scheduler and
the raw manifest remains the atomic restart checkpoint.

For each `YEAR` in `2003 2007 2011 2016`, run:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py discover \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --output reports/generated/gas-duma-YEAR/hierarchy.json

proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py plan \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --hierarchy reports/generated/gas-duma-YEAR/hierarchy.json \
  --output reports/generated/gas-duma-YEAR/plan.json

proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py crawl \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --plan reports/generated/gas-duma-YEAR/plan.json \
  --report reports/generated/gas-duma-YEAR/crawl.json

proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py build \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --hierarchy reports/generated/gas-duma-YEAR/hierarchy.json \
  --crawl-report reports/generated/gas-duma-YEAR/crawl.json \
  --output reports/generated/gas-duma-YEAR/protocols.json

proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py recover-gaps \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --hierarchy reports/generated/gas-duma-YEAR/hierarchy.json \
  --protocols reports/generated/gas-duma-YEAR/protocols.json \
  --report reports/generated/gas-duma-YEAR/direct-recovery.json

proper-app/.venv/bin/python proper-data/crawler/generate_historical_typescript.py \
  reports/generated/gas-duma-YEAR/protocols.json \
  --output proper-data/YEAR-duma --shard-size 250

proper-app/.venv/bin/python proper-data/crawler/historical_nationwide.py validate \
  --year YEAR --raw-dir data/raw/gas-duma-YEAR \
  --hierarchy reports/generated/gas-duma-YEAR/hierarchy.json \
  --protocols reports/generated/gas-duma-YEAR/protocols.json \
  --recovery-report reports/generated/gas-duma-YEAR/direct-recovery.json \
  --output proper-data/YEAR-duma/coverage.json
```

Discovery refuses promotion unless the recursive hierarchy is complete and every TIK
has an evidence-linked official URL for every contest in that election. URLs exposed
by TIK navigation are retained verbatim. When the older 2003 interface does not expose
its verified leaf report types there, the URL is derived only from that TIK's exact
hierarchy ID, the unique UIK root below it, and checked-in election/type semantics;
the plan records `official-hierarchy-derived` provenance. Historical
accounting rows are separated by their official `Число ...` labels rather than the
2021-specific fixed row count. The offline build transposes each TIK's UIK columns,
reconciles every aggregate row, and reports missing hierarchy UIKs without silently
dropping them. Generated TypeScript is deterministically sharded by region in the
same logical layout as 2021.

Historical generation rebuilds commission ancestry from the saved tree on every
offline build. Every UIK/TIK/relation carries the official region code, region TVD,
and region name. The 2003 and 2016 single-member trees additionally carry the exact
OIK TVD/name and district number: generation requires 225 unique OIK TVDs and the
complete `1..225` number set, joined only from the `ОИК №…` breadcrumb whose link
targets that OIK in a hashed official TIK result page. The party-only 2007/2011 trees
and all presidential trees emit no invented OIK; their relation has `district: null`.
Intermediate navigation groupings in those trees are never promoted to districts.

### Completed nationwide historical run (2026-08-26)

All four requested elections completed recursive discovery, result acquisition,
failure-only retries, offline transposition, reconciliation, direct gap recovery,
regional TypeScript generation, and coverage reporting. Every planned TIK result URL
ultimately produced a validated HTTP 200 result table.

| Year | Regions | TIKs | Hierarchy UIKs | Complete UIKs | Partial UIKs | Missing from columns | Reconciliation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 2003 | 89 | 2,757 | 95,396 | 95,090 both contests | 121 | 185 | 324 displayed aggregate-row differences across 19 TIKs |
| 2007 | 86 | 2,750 | 96,246 | 96,193 party | 0 | 53 | 5 displayed aggregate-row differences across 3 TIKs |
| 2011 | 84 | 2,746 | 95,400 | 95,225 party | 0 | 175 | passed in every TIK |
| 2016 | 85 | 2,820 | 96,889 | 96,869 both contests | 3 | 17 | passed in every TIK |

Recovery fetched every missing contest/UIK endpoint. They returned official navigation
documents rather than protocol tables, so none was promoted. The coverage JSON files
retain the failed recovery URL, response hash, status, and validation reason. The 2003
and 2007 reconciliation differences are also preserved exactly as displayed by the
official TIK tables; generation does not rewrite either the aggregate or UIK values.

## Nationwide Russian presidential crawls (2004–2024)

The same restartable nationwide workflow covers every Russian presidential election
for which the official GAS archive exposes a recursive TIK/UIK hierarchy: 2004, 2008,
2012, 2018, and 2024. Their checked-in VRNs, hierarchy roots, and verified presidential
report types (`227` TIK columns and `226` direct UIK protocols) are in
`historical-nationwide.json`. The 2000 election remains a separate static legacy
archive and is not represented as a GAS UIK/TIK protocol crawl.

For each `YEAR` in `2004 2008 2012 2018 2024`, use the historical nationwide commands
above with `gas-president-YEAR` in the raw/report paths and generate into
`proper-data/YEAR-president`. Each plan must be `ready`; every acquired table must
validate as the configured presidential contest before the offline build can promote
it. Gap recovery, reconciliation, deterministic TypeScript generation, coverage
reporting, and the final checks are identical to the Duma workflow.

The 2024 archive exposes both TIK aggregate and direct UIK protocols as type 226 but
withholds the usual type-227 UIK-column tables. The crawler therefore requests the
exact official type-226 URL once for every TIK, classifies it as a direct TIK
aggregate, and uses hierarchy-derived type-226 direct recovery for all 94,214
discovered precincts. Generated 2024 TIK and UIK protocols both retain the literal
source report type 226; their `level` discriminant distinguishes them. Permanent
HTTP-200 navigation/error documents are retained as gaps; transient non-2xx
responses are failure-only retried and their full observation history remains in the
raw manifest.

### Completed nationwide presidential run (2026-08-27)

All five GAS-backed presidential elections completed recursive discovery, result
acquisition, failure-only retries, offline protocol construction, direct gap recovery,
regional TypeScript generation, and coverage reporting. Every hierarchy and TIK is
retained even when the official archive does not publish its underlying UIK protocol.

| Year | Discovered regions | TIKs | Hierarchy UIKs | Complete UIKs | Missing official UIK protocols | Reconciliation differences |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2004 | 91 | 2,757 | 95,847 | 95,779 | 68 | 361 |
| 2008 | 85 | 2,750 | 96,625 | 96,612 | 13 | 0 |
| 2012 | 85 | 2,746 | 95,424 | 95,415 | 9 | 0 |
| 2018 | 87 | 2,778 | 97,715 | 97,695 | 20 | 0 |
| 2024 | 91 | 2,909 | 94,214 | 91,946 | 2,268 | 1,380 |

All five builds have zero parser/build errors and zero duplicate conflicts. The 2004
differences include official aggregate-only TIK pages and the 2024 differences reflect
comparison against the available 91,946-UIK subset; neither aggregate nor precinct
values are rewritten to force agreement. Exact missing URLs, response hashes, status,
validation result, and per-region reconciliation status are in each generated
`coverage.json`.

## Official identity catalogs for historical nationwide elections

The nationwide workflow also crawls the exact official GAS identity reports linked
from the saved election/OIK navigation pages. These requests are part of the normal
restartable plan and use the same hashed response manifest:

| Elections | Registry evidence | Winner evidence | Generated catalog |
| --- | --- | --- | --- |
| 2003/2016 Duma single-member | type 220 candidate registry | 225 OIK type 223/463 result pages | `districts.ts` and regional shards |
| 2003/2007/2011/2016 Duma party | type 236 association registry; 2003 also follows 32 type 303 detail links | type 236 ballot/mandate fields | `parties.ts` |
| 2004/2008/2012/2018/2024 president | type 221 candidate registry | national type 226 result | `candidates.ts` |

Single-member and presidential vote maps use
`gas:candidate-vibid:<candidate-vibid>`. Modern party maps use
`gas:vrnio:<vrnio>`; 2003 uses `gas:association-vibid:<vibid>`. The only
non-GAS identity is the literal ballot choice `special:against-all`, present in the
2003 Duma and 2004 presidential results. Display names and nomination metadata live
in the catalogs rather than being repeated in every protocol.

Publication is refused unless every registry and winner source has an exact official
URL, SHA-256, retrieval time, final URL, and live/Wayback provenance; every
non-exception result label matches a registered identity by exact normalized name
(and an exact terminal DOB only for the three same-name candidates in 2016 district
38); UIK and TIK key sets agree except for the configured partial official maps; and
the unique highest official result agrees with the winner identity.

The known source-state exceptions are bounded explicitly. Two duplicated 2003
candidate VIBIDs account for four history rows. Four 2003 result labels conflict with
the type-220 registry in districts 39, 57, 60, and 126; they use stable
`special:official-result-label:<district>` keys and retain the raw label, reason, and
exact OIK/TIK/UIK occurrence counts in the district catalog. Sixteen 2003 OIK winner
pages expose their unique maximum label only as an obfuscated ordinal; the winner is
therefore selected from summed, identity-keyed official TIK totals and accepted only
when that total equals the unique maximum numeric total on the hashed OIK page. The
exact district/ordinal pairs are checked in with the election configuration.

Districts 162/181/207 elected `Против всех`; 12 further 2003 winner flags and 19
2016 winner flags are absent from the mutable registry state, and the literal
registry flag is retained separately when the hashed winner source supplies the
normalized catalog winner. Finally, 14 named 2004 TIK pages publish only a strict
subset of the presidential candidate rows. Those aggregate maps are retained
verbatim, their exact TIK set and present/missing identity keys are gated, and no
values are filled from UIK protocols.

Official registry sizes are checked before generation: presidential registries contain
11/15/14/38/15 rows with 6/4/5/8/4 registered candidates for 2004/2008/2012/2018/2024;
Duma party registries contain 32/14/7/22 associations with 23/11/7/14 federal-ballot
associations for 2003/2007/2011/2016; and the consolidated 2003/2016 single-member
catalogs contain 3,027/2,446 candidate identities across exactly 225 districts.

The checked-in controlled matrix selects exact national, region, OIK, TIK, hierarchy,
static, and workbook requests. `probe-spec` honors `--source-mode`; exact live-only
hierarchy endpoints are omitted from archive-only plans when no capture has been
verified. To choose different regions or TIKs, copy the JSON and retain only exact
evidence-derived URLs. Never manufacture TVDs arithmetically.

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-core-probes.json --dry-run

proper-app/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-core-probes.json
```

Resume with the second command. Retry only absent, failed, or non-2xx observations:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec proper-data/crawler/historical-probes.json --source-mode live-only \
  --raw-dir data/raw/gas-duma-history-live --only-failures \
  --report reports/generated/gas-duma-history/core-retry.json
```

After hierarchy probes succeed, build the deterministic protocol sample spec offline.
The checked-in selection registry chooses Kamchatka and Moscow, two TIKs per region,
every applicable contest, and up to three UIKs per TIK (all UIKs when fewer exist):

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py make-sample-spec \
  --hierarchy-report reports/generated/gas-duma-history/live-core-probes.json \
  --selections proper-data/crawler/historical-protocol-selections.json \
  --output reports/generated/gas-duma-history/live-protocol-probe-spec.json

proper-app/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec reports/generated/gas-duma-history/live-protocol-probe-spec.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-protocol-probes.json --dry-run

proper-app/.venv/bin/python proper-data/crawler/historical.py probe-spec \
  --spec reports/generated/gas-duma-history/live-protocol-probe-spec.json \
  --source-mode live-only --rate 5 --concurrency 2 \
  --raw-dir data/raw/gas-duma-history-live \
  --report reports/generated/gas-duma-history/live-protocol-probes.json
```

Resume with the same command. Retry only failed identities with `--only-failures`.
Validate election/type identity, hierarchy versus UIK headers, aggregate arithmetic,
and direct UIK/TIK rows against the paired aggregate report without network access:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py validate-samples \
  --probe-report reports/generated/gas-duma-history/live-protocol-probes.json \
  --output reports/generated/gas-duma-history/live-protocol-validation.json
```

Decode or classify preserved responses with no network access. Add
`--direct-protocol` for a verified leaf/TIK protocol table:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py classify \
  --input data/raw/gas-duma-history-live/sha256/84/84891d562e218ea23c2e0bbc1a58a6b3517d36077b6104fc75f1c0bef82238f8 \
  --direct-protocol \
  --output reports/generated/gas-duma-history/decoded-2011-aleut.json
```

Generate only verified sample TypeScript protocols, then validate the checked-in
availability matrix and evidence hashes:

```sh
proper-app/.venv/bin/python proper-data/crawler/historical.py generate-samples \
  --probe-report reports/generated/gas-duma-history/live-protocol-probes.json \
  --output-root proper-data

proper-app/.venv/bin/python proper-data/crawler/historical.py validate-matrix \
  --matrix proper-data/historical-duma-availability.json
```

The human summary is `proper-data/HISTORICAL-DUMA-AVAILABILITY.md`; the JSON matrix is
authoritative for readiness, missing elements, report semantics, and evidence hashes.
