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
PYTHONPATH=src ../proper-app/.venv/bin/python -m uik_address.cli assemble
```

## Outputs

- `work/uik-addresses-2026-public.csv` — the requested 11-column handoff.
- `work/uik-addresses-2026.csv` — audit-friendly UTF-8-with-BOM CSV with match and provenance fields.
- `work/coverage.json` — exact matched/address/phone counts.
- `work/backbone.jsonl` — Duma-only UIK-to-TIK hierarchy.
- `work/cec/` and `work/regional/` — normalized contacts, crawl summaries,
  manifests, and content-addressed raw responses.
- `work/run-summary.json` — combined run result.

The regional parser intentionally publishes only explicit fields in CSV, JSON,
simple tables, or labelled HTML blocks. PDFs, office files, and ambiguous prose
are archived and reported as unresolved instead of being guessed into the CSV.

## Matching policy

UIKs are joined only by exact subject code and UIK number. TIKs first use exact
subject code and TIK number, then a normalized name only if the candidate is
unique inside the subject. Conflicting source values are flagged in the match
method and the higher-quality/fresher source wins deterministically.

The CEC commission index is useful but currently incomplete. The regional pass is
therefore a necessary second route, not an optional source of truth. `coverage.json`
is the release gate: missing fields stay empty and are never presented as complete.
