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
proper-app/.venv/bin/python proper-data/2026/build_declaration_corpus.py
```

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
