# UIK address workspace

This standalone workspace builds a 2026 State Duma CSV with the hierarchy that is
already authoritative in `proper-data/2026`, then adds only contact data that can
be matched conservatively to that hierarchy.

The output columns cover the requested fallback chain: subject, TIK name/number,
TIK address and phone, UIK number, UIK commission address/phone, and the voting
premises address/phone. Every matched contact and hierarchy row retains its source
URL, source type, retrieval time, and content hash.

## Run

From this directory, using the repository's existing Python environment:

```bash
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli run \
  --max-pages-per-region 30
```

`PROPER_DATA_PROXY_URL` is used when present. Pass `--proxy-url` to override it.
Responses are cached by content hash, so interrupted crawls can be resumed; use
`--refresh` only when a fresh network snapshot is required.

The stages can also be run separately:

```bash
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli backbone
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli crawl-cec \
  --unfiltered --all-backbone-subjects
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli crawl-regional
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli crawl-moscow
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli reparse-regional
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli assemble
```

For incremental enrichment, target one or more catalog regions without discarding
the cached results for the others:

```bash
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli crawl-regional --region-code 42
```

Already-preserved artifacts can be reparsed without network access after adding
or improving a parser:

```bash
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli reparse-regional \
  --region-code 79
```

## Outputs

- `work/uik-addresses-2026-public.csv` — the consumer handoff. `src` is the
  official URL that specifically backs `uik_voting_address`; `legacy_status` is
  `current-2026`, `legacy-only`, `unverified`, `missing`, or `conflicted`.
- `work/uik-addresses-2026.csv` — audit-friendly UTF-8-with-BOM CSV with match and provenance fields.
- `work/coverage.json` — exact matched/address/phone counts.
- `work/gaps.csv` — one row per TIK, ranked by UIKs still lacking both a polling
  address and a TIK fallback.
- `work/backbone.jsonl` — Duma-only UIK-to-TIK hierarchy.
- `work/cec/`, `work/regional/`, and `work/moscow/` — normalized contacts, crawl summaries,
  manifests, and content-addressed raw responses.
- `work/run-summary.json` — combined run result.

The regional parser intentionally publishes only explicit fields in CSV, JSON,
simple tables, labelled HTML blocks, and freshness-gated 2026 XLSX/DOCX precinct
lists. Text PDFs are accepted only when a 2026 document repeatedly labels one
location as both the UIK and voting room. Other PDFs, legacy Office, undated
Office, and ambiguous prose are archived and reported as unresolved instead of
being guessed into the CSV. XLSX/DOCX parsing requires explicit precinct-number
and address columns; commission locations are not copied into the polling-address
field.

## Matching policy

UIKs are joined only by exact subject code and UIK number. TIKs first use exact
subject code and TIK number, then a normalized name only if the candidate is
unique inside the subject. Conflicting source values are flagged in the match
method and the higher-quality/fresher source wins deterministically in the audit
CSV. Conflicting values are blanked from the public CSV so user-facing consumers
fail closed until the conflict is resolved.

The CEC commission index is useful but currently incomplete. The regional pass is
therefore a necessary second route, not an optional source of truth. `coverage.json`
is the release gate: missing fields stay empty and are never presented as complete.

The Moscow pass calls the Moscow City Election Commission's official number lookup
for each subject-77 backbone UIK. A response is publishable only when its returned
UIK number matches exactly, its `votingDate` is `2026-09-20`, and it contains an
explicit polling-place address. HTTP errors and rejected records remain in the
hashed manifest and are resumed without refetching verified responses.
