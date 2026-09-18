# Elections scheduled for 20 September 2026

This directory is a pre-election snapshot of every campaign returned by the public
current-election service of the Central Election Commission of Russia for the voting
date `2026-09-20`. It includes federal, regional, district, local, settlement,
referendum, recall, and other direct-vote campaigns exposed by that calendar.

The data answers two separate questions without conflating them:

1. Which UIKs does the official classifier attach to each campaign and district?
2. Which candidates or party associations does the official registry attach to that
   campaign and district?

## Layout

- `elections/region-*.json` — campaign metadata and official calendar status, with
  `campaignScope` explicitly normalized as `federal`, `regional`, or `municipal`
  (`other_or_unclassified` is reserved for an unknown official level).
- `candidates/region-*.json` — all disclosed candidates, including candidates who
  withdrew, lost registration, or were refused registration. `ballotEligible` is true
  only when the current registration label is literally `зарегистрирован`.
- `associations/region-*.json` — electoral associations from report 236. A missing
  registration decision is marked `not_marked_registered`, never guessed to mean
  registered or rejected.
- `candidate-sets/region-*.json` — normalized candidate or association sets. Direct
  candidates are grouped by official district number; proportional-list members are
  retained as list members and are not mislabeled as direct ballot choices.
- `uiks/region-*.json` — UIK-to-campaign and UIK-to-candidate-set assignments, with
  the election-specific official classifier UUID and response hash for every path.
- `declarations/index.json` — the report-77 catalog of candidate income and property
  declarations, with hashes for both the downloaded ZIP response and extracted file.
- `declarations/files/<financial-report-id>/` — the original PDF or XLSX document
  extracted from each ZIP response. The ID directory prevents filename collisions.
- `declarations/party-lists/` — consolidated CEC PDFs for registered federal party
  lists: income/property, foreign property, and large transactions.
- `declarations/regions/<region-code>/` — documents published by each regional
  election commission. Candidate declarations and `выявленные факты
  недостоверности` are classified separately in every regional index.
- `declarations/corpus.json` — a metadata-only union of the central report-77,
  federal party-list, and regional indexes. It references the original files in
  place and records unresolved regional coverage instead of treating silence as an
  empty result.
- `campaign-finance/regions/<region-code>/` — exact official 2026 State Duma
  election-fund reports plus normalized candidate/party snapshots. Region `0` is
  the central CEC financing section; the other directories are regional commission
  and district-commission publications discovered through official routes and site
  search. Unrecognized documents are retained with an explicit `unparsed` status
  rather than guessed values.
- `docs/declarations/` — the generated, collision-safe document tree used by the
  SQLite database. Ordinary documents are hard-linked when the filesystem permits
  (copied otherwise); ZIP sources are expanded and only their member files appear
  here.
- `elections.sqlite3` — the generated query database. It contains normalized regions,
  territories, election kinds, election levels, systems, elections, districts, all
  candidate registrations, declaration files, audited candidate/file links,
  campaign-finance documents, and parsed fund snapshots.
- `build_election_database.py` — reproducibly rebuilds both generated outputs. Paths
  in the database are relative to this directory.
- `regional-declaration-sources.json` — the 89-region official-site catalog used by
  the regional crawler. Vetted 2026 State Duma pages are distinguished from guarded
  search fallbacks so historical elections and staff disclosures are not ingested.
- `status-dictionaries.json` — the official nomination, registration, and election
  status vocabularies captured with the snapshot.
- `metadata.json` and `coverage.json` — interpretation rules, provenance, coverage,
  and validation totals.

## What is official and what is assumed

An assignment of an election-specific UIK classifier to a campaign is official-source
data. The stable `uikKey` is an explicit identity assumption: classifier records from
different campaigns are treated as the same existing commission when their CEC region
code and UIK number agree. The 2024 presidential hierarchy is used only to label such
keys as matched, new/renumbered, or not assigned; it never supplies a 2026 contest or
candidate assignment.

Candidate and association rows are retained even when they are not current ballot
options. Their raw Russian status and normalized status code make that distinction
machine-readable. The official portal warns that registration fields for a party list
can be absent after cancellation or annulment, so an absent association registration
mark has `ballotEligibility: null` rather than a fabricated boolean.

When the official classifier and candidate registry disagree on a district number,
the mapping is left unresolved unless both expose exactly one district. That one
narrow fallback is marked `assumed_unique_district_number_mismatch`, with both
official numbers retained in `candidateMappingEvidence`; it is also enumerated in
`coverage.json` rather than being presented as an official district-number match.

The CEC classifier describes Crimea, Sevastopol, Donetsk, Luhansk, Zaporizhzhia, and
Kherson as subjects of the Russian Federation. Records for region codes `93`–`98`
carry a separate `territorialStatus` marking that these territories are internationally
recognized as part of Ukraine, with references to UN General Assembly resolutions
[68/262](https://docs.un.org/A/RES/68/262) and
[ES-11/4](https://docs.un.org/A/RES/ES-11/4).

## Reproduction

Raw responses are content-addressed under the ignored
`data/raw/cik-current-2026-09-20/` directory. Re-run the resumable crawl and then
generate the checked-in shards:

```sh
proper-app/.venv/bin/python proper-data/2026/current2026.py crawl
proper-app/.venv/bin/python proper-data/2026/current2026.py generate
proper-app/.venv/bin/python proper-data/2026/current2026.py crawl-declarations
proper-app/.venv/bin/python proper-data/2026/crawl_cik_party_declarations.py
proper-app/.venv/bin/python proper-data/2026/crawl_regional_declarations.py \
  --catalog proper-data/2026/regional-declaration-sources.json
proper-app/.venv/bin/python proper-data/2026/crawl_campaign_finance.py
proper-app/.venv/bin/python proper-data/2026/reparse_campaign_finance.py
proper-app/.venv/bin/python proper-data/2026/build_declaration_corpus.py
proper-app/.venv/bin/python proper-data/2026/build_election_database.py
```

The database builder's Python readers are included in the `crawler` optional
dependencies in `proper-app/research/pyproject.toml`. Image-only PDFs additionally
require the `tesseract` executable with Russian language data (the Debian packages are
`tesseract-ocr` and `tesseract-ocr-rus`).

`candidate_details` resolves each candidate's election kind, level, system, territory,
district, and region. `candidate_document_paths` is the direct candidate-to-file query
surface. For example:

```sql
SELECT c.full_name, c.election_kind, c.region_name, c.territory_name,
       p.category, p.document_path, p.match_method, p.confidence
FROM candidate_details AS c
JOIN candidate_document_paths AS p ON p.candidate_id = c.id
WHERE c.full_name = 'Иванов Иван Иванович';
```

Candidate/document links are many-to-many. Exact full-name content matches receive the
highest confidence. Unique Russian name-stem or surname/initial matches and explicit
consolidated district scope are retained with lower confidence and a human-readable
evidence field. ZIP members retain both the source archive hash and their member path;
links always point to the extracted member, never to the ZIP container.

Use `crawl --refresh` for a later snapshot. Candidate registrations can change before
voting, so a retrieval timestamp is part of the evidence, not incidental metadata.
The declaration command is independently resumable. Exact ZIP responses are retained
in the ignored `data/raw/cik-current-2026-declarations/` content-addressed store, while
the extracted documents and their portable index are written here.

The party-list and regional crawlers use the same proxy settings and resumable,
content-addressed response storage. Regional output is deliberately conservative:
a completed request with no matching documents is not evidence that a commission
has published none, and failures or page-limit truncation remain explicit in each
index and in `declarations/corpus.json`.

Campaign-finance discovery starts from the central CEC financing section and uses
the same vetted regional host catalog, bounded traversal, RU proxy, retry policy,
and content-addressed response store. It searches the official sites for both fund
receipts/spending disclosures and final financial-report terminology, and recognizes
common State Duma route aliases such as `vybory-deputatov-gd`. Query the
latest parsed observation for each fund without confusing an absent publication with
a zero balance:

```sql
SELECT entity_name, as_of_date, received_total, spent_total, returned_total
FROM latest_campaign_fund_snapshots;
```

The network-free reparser dispatches each preserved file to the spreadsheet,
DOCX-table, legacy Office, text-PDF, scanned-PDF OCR, or recursive ZIP adapter. Every
document records its semantic classification, template family, adapter, archive
members, validation findings, and parse status. Invalid numeric rows remain in
`campaign_fund_snapshots` for audit but are excluded from the latest-snapshot view.

The decision to keep the declaration corpus as an index over source-owned documents
is recorded in [ADR 0001](docs/adr/0001-index-declaration-documents-in-place.md).
