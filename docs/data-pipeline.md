# 2021 data pipeline

Run commands from any directory after installing the backend with
`pip install -e 'backend[dev]'`. Set `DATABASE_URL` to the MySQL URL used by the
application. The CLI also accepts `--database-url` before the subcommand.

```sh
elections-data migrate
elections-data download
elections-data acquire-single-member
elections-data import-results
elections-data import-single-member
elections-data import-commissions
elections-data resolve-gas-ids
elections-data match
elections-data validate \
  --published-totals data/published-totals-2021.json \
  --single-member-published-totals data/published-single-member-totals-2021.json
elections-data verify-complete
```

The single-member official reference is deliberately not fabricated or committed by this
workflow. Before the validation step, create the local ignored file
`data/published-single-member-totals-2021.json` by transcribing the final official OIK
protocols with source URLs. It must cover every candidate and exactly one declared winner
in each of all 225 OIKs. The minimal shape is:

```json
{
  "totals": [
    {
      "oik_code": "1",
      "level": "oik",
      "metric": "candidate_votes",
      "option_name": "Candidate full name",
      "value": 12345,
      "source_url": "https://official.example/protocol/1"
    },
    {
      "oik_code": "1",
      "level": "oik",
      "metric": "winner",
      "option_name": "Winning candidate full name",
      "value": 1,
      "source_url": "https://official.example/protocol/1"
    }
  ]
}
```

Accounting totals and `uik_count` reference rows may also be included and are reconciled
when present. Candidate names must match the preserved ballot roster exactly. A missing
file, unknown OIK, missing candidate total, absent/ambiguous winner, or missing source URL
keeps the final report not ready. `make rebuild-data` runs the complete sequence and fails
up front if the local reference file is absent; override its location with
`SINGLE_MEMBER_TOTALS=/workspace-relative/path.json`.

The committed source manifest pins the preserved party-list CSV and 14 September
2021 GIS-Lab commission snapshot by URL, byte size, and SHA-256. Downloads go to
the Git-ignored `data/raw/` directory. An existing changed file fails verification;
`--force` is required to replace it.

`acquire-single-member` crawls the dated CEC preservation snapshot at no more than one
request per second. It writes unchanged responses below
`data/raw/duma-2021-single-member-cec/` and writes the checksummed provenance and gap
manifest to `reports/generated/single-member-snapshot.json`. The command is resumable:
files whose size and SHA-256 still match the manifest are parsed for further discovery but
are not downloaded again. A changed local payload stops the run; use `--force` only when
intentionally replacing the whole snapshot.

The command returns a non-zero status when any of the 225 expected OIKs lacks a
checksum-preserved payload (status `preserved` or `redirected`) or when the gap report contains
an unavailable, malformed, or inconsistent page. Redirected responses remain reported as gaps.
The incomplete manifest is still written for inspection. `--allow-gaps` is available for exploratory runs. Verify an existing
snapshot without network access with:

```sh
elections-data acquire-single-member --verify-only
```

Verification re-hashes every preserved payload and checks OIK completeness. Both raw
payloads and the generated run manifest are intentionally Git-ignored; the committed
acquisition plan is the reproducible recipe.

`import-single-member` accepts checksum-preserved payloads (status `preserved` or
`redirected`), verifies their byte size and SHA-256 again, parses the transposed CEC HTML tables, and stores district-scoped
candidate votes without changing party-list rows. Parser failures and source gaps remain
explicit in its machine-readable output; redirected responses remain listed as source
gaps even when their preserved bytes can be imported. Rerunning it replaces records per source artifact
and cannot duplicate candidates, protocols, or votes.

Imports are restartable per source artifact. Every normalized result retains its
artifact, source row, raw row, and original result URL. Malformed rows are retained
in `import_rejects`. Commission raw JSON excludes phone, fax, and email fields;
those contacts are never part of the exploration contract.

Matching first uses a stable GAS Vybory ID. The fallback uses UIK number plus
normalized region/address and parent-TIK evidence. Every candidate and score is
stored in `match_evidence`; a tie is classified `ambiguous`, never silently chosen.
The default audit is `reports/generated/matches.json`.

`resolve-gas-ids` starts from each stored result URL, downloads every distinct parent
result table once, follows the explicit UIK result link, and accepts an identity only from
the commission link whose request is `action=ik&vrn=<commission-id>`. The `vrn` on an
`action=show` result URL remains an election identifier and is never used as a commission
identifier. Responses and their checksums are preserved below
`data/raw/gas-id-resolution/`; per-result evidence is stored in `gas_id_resolutions` and
the machine/human audits are written to `reports/generated/gas-id-resolution.json` and
`.md`. Conflicts and IDs missing from the selected commission snapshot fail closed.
Research against preserved official responses found no result-to-commission link in the
sampled 2021 result pages, so those pages remain explicitly unresolved; see
`docs/gas-id-resolution.md`. The importer also leaves negative GIS-Lab `iz_id` values out
of `gas_vybory_id`, because the extractor generated those locally as fallback identities
rather than receiving them from GAS.

Resume the national run without rebuilding or re-downloading successful responses:

```sh
docker compose run --rm api elections-data resolve-gas-ids
```

Verify and parse a fully cached run without network access with:

```sh
docker compose run --rm api elections-data resolve-gas-ids --offline
```

Use `--max-parent-urls 3` for a bounded smoke test. Timeouts, retries, request spacing,
and bounded parallelism are configurable with `--timeout`, `--retries`, `--rate-limit`,
and `--concurrency`. `make resolve-gas-ids` is the resumable Docker shorthand.

Validation checks the two ballot-accounting identities, party-vote totals,
registered-voter bounds, duplicate or missing UIK identities, and unresolved
matches. It aggregates TIK, region, and national totals with DEG in a separate
bucket. By default it loads `data/published-totals-2021.json`, transcribed from the
CEC final national protocol, and compares all 12 accounting values and all 14 party
totals. The API reports `reconciled=false` when no published totals exist, any
validation errors remain, or any result has not been validated. Known exceptions
remain enumerated in `reports/generated/validation.json`; they are not suppressed.

Single-member validation additionally checks candidate sums, missing candidates and UIKs,
all 12 accounting rollups through TIK and OIK, official candidate totals, and declared
winners. Official single-member reference rows are scoped with `oik_code`; winner rows use
`metric="winner"` (or `declared_winner`), the candidate's `option_name`, and `value=1`.

The expected party-list import is 96,325 records and 1,348,550 vote rows; the expected
single-member model has exactly 225 district ballots. Import, validation, and
`verify-complete` print machine-readable counts for automation. The final report records
both ballot kinds, source checksums, unresolved records, rejects, validation state, and the
225-district readiness gate. It also re-verifies snapshot checksums and reports acquisition
coverage and every unresolved source gap. The command exits with status 2 while `ready` is
false, but still writes `reports/generated/complete-dataset-2021.json` for diagnosis.
