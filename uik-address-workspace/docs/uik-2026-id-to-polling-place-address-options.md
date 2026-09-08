# Reliable `UIK_2026_id` to polling-place address options

Checked 8 September 2026. This report uses only first-party public sources:
official CEC and regional/local-government sites, their live application code and
API responses, and the repository's preserved copies of those responses.

## Decision

Build the 2026 address map offline from a layered set of official sources. Use
current regional APIs where they provide a direct UIK-number lookup, then parse
the current official precinct lists that administrations are legally required to
publish. Use the CEC commission directory as a continuously refreshed verifier
and gap-filler, not as the sole source today.

Do not make a CEC or regional-site request in the user-facing request path. The
runtime endpoint should read a versioned, validated local map. This avoids making
availability depend on an undocumented upstream API, the repository proxy, or a
regional website.

## What `UIK_2026_id` actually means here

There is no field literally named `UIK_2026_id` in this workspace.

The 2026 State Duma backbone has 91,247 rows. Each row has:

- `uik_classifier_id`: a nonempty UUID from the CEC's election-specific
  classifier. All 91,247 UUIDs are distinct in the current backbone.
- `subject_code` and `uik_number`: also unique as a pair across all 91,247 rows.
- `uikKey` in the upstream 2026 shards: the explicit cross-election identity
  assumption `"{subject_code}:{uik_number}"`.

These semantics are implemented in
[`backbone.py`](../src/uik_address/backbone.py) and documented in the
[`proper-data/2026` README](../../proper-data/2026/README.md#what-is-official-and-what-is-assumed).

For the address table, the canonical key should therefore be:

```text
(subject_code, uik_number)
```

Keep `uik_classifier_id` as 2026 election provenance and as a consistency check,
but do not require address sources to expose it. Official regional APIs, legal
acts, spreadsheets and tables normally expose the subject and precinct number.

The CEC itself has two different UUID namespaces. For Tula UIK 1627:

- the 2026 classifier endpoint returns `uik_classifier_id`
  `357bee94-1f80-4d53-aed2-a57c24c27782`;
- the commission directory returns organization ID
  `f6c5e326-ed4d-4c6f-848f-0049762b6db9`.

Passing the classifier UUID to `reports/42?commissionOrgId=...` returns 404.
There is no demonstrated direct classifier-UUID to commission-organization-UUID
bridge. The safe bridge is the exact `(subject_code, uik_number)` lookup.

## Ranked options

| Rank | Source route | Expected coverage | Reliability | Joinability | Operational risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Current official administration/election-commission precinct lists (XLSX, HTML, DOCX, PDF and amendments) | High for domestic ordinary UIKs; publication is required by law | High when the document is current, official and amendment-complete | Excellent: exact subject + UIK number | Medium: heterogeneous formats, OCR and amendment handling | Primary nationwide build route |
| 2 | Official regional APIs with direct UIK-number lookup | High inside supported regions; low nationally without more adapters | High while current; field-level and election-date validation is possible | Excellent | Medium: undocumented endpoints and region-specific behavior | Highest-priority adapters, beginning with Moscow |
| 3 | CEC `commissionOrg` directory plus `parents`/report 42 | Intended nationwide, but extremely sparse in the current snapshot | High for rows actually returned; current official CEC data | Good via exact subject + number; no UUID bridge | Medium-high: proxy, arithmetic challenge, undocumented schema, upstream churn | Monitor, verify and fill gaps; not primary today |
| 4 | Regional registered-address search applications | Potentially good within a region, but designed for individual addresses rather than enumerating UIKs | High for interactive answers | Indirect; enumeration may be expensive or impossible | High for bulk use | Use only where it exposes an enumerable or number-based route |
| 5 | Historical CEC routes or prior-election addresses | Unknown/stale | Insufficient for a 2026-only product | Sometimes exact by subject + number | High correctness risk | Reject as authoritative output |

## Option 1: current official precinct lists

This is the best systematic route because publication is not accidental. Article
15(1) of Federal Law No. 20-FZ requires local administrations to publish precinct
lists no later than 45 days before voting, including precinct numbers and
boundaries, UIK locations, polling-room locations, and telephone numbers. See the
[current text on the official legal-information portal](https://ips.pravo.gov.ru/api/ips/legislation/document?baseid=None&hash=8453e73f27fc26eb25b115fe9894ec023653cdd28e3f87f23e278c36d29bb54a)
and the [official publication record](https://publication.pravo.gov.ru/document/0001201402240003).

The format and publisher vary, but the required fields align directly with this
project's target schema.

### Proven complete example: Jewish Autonomous Oblast

The official regional commission publishes an XLSX named
[`Перечень избирательных участков на 15.06.2026.xlsx`](http://jewish-aut.izbirkom.ru/edinyy-den-golosovaniya/20-09-2026/%D0%9F%D0%B5%D1%80%D0%B5%D1%87%D0%B5%D0%BD%D1%8C%20%D0%B8%D0%B7%D0%B1%D0%B8%D1%80%D0%B0%D1%82%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D1%85%20%D1%83%D1%87%D0%B0%D1%81%D1%82%D0%BA%D0%BE%D0%B2%20%D0%BD%D0%B0%2015.06.2026.xlsx).
It states the 20 September 2026 election date and provides UIK number, address and
phone columns. The repository has the exact 200 response at SHA-256
`4e8e06b2bef38567af5abbd7ee0b28ab3a4536fa69cfda70557ebfcf670b533a`.

A read-only parse found 160 distinct numbered address records. All 160 numbers
join the subject-79 Duma backbone, with no missing or extra numbers. This is
100% exact regional coverage from one already-cached official file.

### High-coverage DOCX example: Mari El

The official Mari El commission's
[precinct-list page](http://mari-el.izbirkom.ru/formirovanie-uchastkovykh-izbiratelnykh-komissiy/izbiratelnye-uchastki/izb-uch.php)
links a structured
[2026 DOCX](http://mari-el.izbirkom.ru/formirovanie-uchastkovykh-izbiratelnykh-komissiy/izbiratelnye-uchastki/S12-UIK-14_08_26.docx).
It was retrieved with HTTP 200 on 8 September 2026 at SHA-256
`96b19aecaf8f46cbd3b238f8e168e16df8b99e130d76776ad083e75ba2629207`.
The document says the list is current as of 16 June 2026 and separates `Место
нахождения участковой избирательной комиссии` from `Адрес помещения для
голосования`.

A read-only table parse matched 491 of the 494 current subject-12 Duma backbone
numbers (99.39%), and every matched row had a polling-room address. UIKs 528,
529 and 530 were absent, so they must remain gaps until a newer amendment or
another current official source confirms them. This is both strong evidence for
the DOCX route and evidence that an explicit 2026 date does not eliminate the
need to process later changes.

### High-coverage XLSX example: Magadan Oblast

The official Magadan election commission publishes
[`perechen_UIK.xlsx`](http://magadan.izbirkom.ru/20092026/ii/doc/perechen_UIK.xlsx)
under its 20 September 2026 election section. The cached HTTP 200 response has
SHA-256 `8319e9de44dfd77880395c34d7d7277e777a3cc752fc254a8770f88763f8fffb`.
A read-only parse found 80 unique numbered records, all of which join current
subject-49 backbone UIKs exactly. This covers 80 of 93 backbone UIKs (86.02%),
with no extras; UIKs 87 through 99 remain absent and must not be inferred.

### Current PDF example: central Saint Petersburg

The [official Central District administration election page](https://www.gov.spb.ru/gov/terr/reg_center/vybor/),
updated 21 August 2026, links both the base act, a 13 July 2026 amendment, and a
current precinct list. The linked
[July 2026 PDF](https://www.gov.spb.ru/static/writable/ckeditor/uploads/2026/08/21/54/%D0%90%D0%B4%D1%80%D0%B5%D1%81%D0%BD%D0%BE%D0%B5_%D0%BF%D1%80%D0%BE%D1%81%D1%82%D1%80%D0%B0%D0%BD%D1%81%D1%82%D0%B2%D0%BE_%D0%BD%D0%B0_%D0%B8%D1%8E%D0%BB%D1%8C_2026_%D0%B3%D0%B0%D0%B7%D0%B5%D1%82%D0%B0.pdf)
has SHA-256
`f6e68c9464e157715e39d62d61f9737f0970dd3814a53638a778b67a4fa6d389`.
Its text is machine-readable and repeatedly labels `Адрес помещения участковой
избирательной комиссии и помещения для голосования`. Sample UIKs 2180, 2200 and
2233 all join the current subject-78 Duma backbone exactly.

### Current HTML examples and the freshness caveat

The official [federal territory Sirius election page](https://sirius.gov.ru/vybory/)
publishes boundaries, the UIK location and a separately labelled polling-room
location for UIKs 62-01 through 62-06. Those numbers correspond to current
backbone keys `23:6201` through `23:6206`.

The official [Primorsko-Akhtarsk administration table](https://prahtarsk.ru/tik/oik23/adresa_yhsastkov_ik/)
also exposes UIK number, boundaries, voting centre/address and phone in HTML.
Numbers 4009, 4010 and 4011 join current subject 23; number 4035 does not. This
is a useful warning: official origin alone does not establish 2026 freshness.
Every source must be date/version-gated and intersected with the current backbone.

### The parser, rather than discovery, is currently the bottleneck

The current [`regional` manifest](../work/regional/manifest.json) contains 8,227
artifacts with 7,797 unique nonempty response hashes. Of the successful artifacts,
3,227 are currently marked as unsupported documents across 84 subjects: 1,275
PDF, 916 DOC, 740 DOCX, 157 RTF, 119 ZIP, 17 XLSX and 3 XLS. Not all are precinct
lists, but the complete subject-79 XLSX demonstrates that valuable current address
sources are already present among them.

Prioritize adapters by likely yield:

1. XLS/XLSX tables with explicit UIK-number and address columns.
2. DOCX tables with explicit polling-room labels.
3. Text-layer PDFs with repeated UIK/address headings.
4. Labelled HTML lists and tables.
5. OCR only for a small, manually approved set of image PDFs; never silently
   promote uncertain OCR output.

Base acts and later amendments must be treated as one version chain. A parser
must not publish a base document while ignoring a newer amendment linked from the
same official page.

## Option 2: official regional number APIs

Moscow has the strongest confirmed direct implementation. Its official home page
offers search by registered address, passport or precinct number. The site's own
current JavaScript calls:

```text
GET https://www.mosgorizbirkom.ru/pollingstation-search-service/api/findByNumber
    ?number={uik_number}&config=1
```

The `config=1` election configuration is required: omitting it returned HTTP
400 in the current probe.

See the [official Moscow election-commission page](https://www.mosgorizbirkom.ru/).
For UIK 146 the endpoint returned an object with `number`,
`votingInstitutionName`, `localityVotingInstitution`,
`votingInstitutionStreet`, `votingInstitutionHouse`, phone and coordinates. It
also returned `votingDate: 2026-09-20T00:00:00`. The response SHA-256 was
`28a472e8107c7f4ce8566d8733b697b86c0897f5ea563cef8c1bc7b44febbaa9`.

A systematic 25-number sample across the 1,442 current Moscow backbone rows
resolved 24 exact UIK numbers, each with a polling address and the 2026 voting
date. Special UIK 9002 returned 404. This is strong evidence for ordinary Moscow
UIKs, but not a claim of complete coverage: run and retain a full reconciliation
before release, and keep a separate gap path for special/temporary UIKs.

The official [Novosibirsk voter form](https://www.izbirkomnso.ru/cabinet_voter/polling_station/)
is also a first-party regional application. It describes a five-level registered-
address query and states that the result contains the polling-place address and
UIK phone. Its application is address-oriented, however, not a confirmed direct
UIK-number endpoint. It ranks below direct number APIs for bulk map construction.

For each regional API adapter, require exact echo of the requested UIK number and,
where present, a 2026 election date/configuration. Archive raw bodies by hash and
rate-limit crawling. An endpoint that returns a current interactive result is not
an implied promise of stable bulk service.

## Option 3: the current CEC commission directory

The official CEC gateway JavaScript at
[`/service/gateway/assets/index-668f033c.js`](http://apps.cikrf.ru/service/gateway/assets/index-668f033c.js)
implements both of the relevant paths:

```text
GET /commissionClassifiers/voterCommissions
    ?subjectRF={subject_code}
    &commissionNumber={uik_number}
    &electionsId=587813923

GET /commissionOrg/search
    ?page=1&perPage=10
    &commissionNumber={uik_number}
    &subjectRfCodes={subject_code}
    &commissionTypes=5
```

The first endpoint reliably identifies the election-specific UIK, including the
same classifier UUID stored in the backbone. It does not carry the physical
polling address. When the second endpoint has a result, its organization UUID can
be passed to:

```text
GET /commissionOrg/parents/{commission_org_external_id}
GET /reports/42?commissionOrgId={commission_org_external_id}
```

Those responses can contain `customVotingRoomAddress` or the fully composed
`votingRoom`, plus polling-room name and phone. For Tula UIK 1627 this chain
returned the voting room at `ул. Школьная, д. 1, здание гимназии №1`.

Current coverage is the blocker. The workspace's
[`coverage.json`](../work/coverage.json) has only 2 of 91,247 rows with an accepted
`uik_voting_address`, both in Tula. Fresh exact probes for Vladivostok UIK 840,
Moscow UIK 146 and Izobilny UIK 456 all returned `empty: true` from
`commissionOrg/search`, even though the classifier endpoint returned the 2026 UIK
for each. Treat `empty` as “not published by this route”, not “the UIK does not
exist”.

The API requires the same short arithmetic challenge and API-key headers used by
the official front end. From this environment, CEC HTTP requires the repository's
configured proxy; no proxy credential or API key was logged or persisted during
these probes. The API and challenge are undocumented public implementation
details. Accordingly:

- crawl or query it only in a bounded, cached background job;
- refresh daily as election day approaches because publication is visibly in
  progress;
- never depend on it synchronously for the user-facing endpoint;
- do not issue 91,247 exact requests merely to prove that most rows are empty;
- use exact subject + number results to cross-check regional sources and flag
  address conflicts.

## Rejected shortcuts

### Treating `uik_classifier_id` as a commission organization ID

This fails. `commissionOrg/parents/{classifier_uuid}` returned an empty array for
tested current classifiers, while report 42 returned 404. Experimental query
parameters such as `commissionClassifierId` on `commissionOrg/search` were
ignored and produced the same unfiltered response. There is no safe UUID join to
implement from the observed protocol.

### Historical CEC voter-service routes

The old `/iservices/voter-services/...` paths return generic HTML rather than the
former JSON protocol in current proxy-backed probes. Even a working old response
would not prove a 2026 polling-room assignment. Do not use it for 2026 output.

### Carrying a 2024 address forward

The repository's 2024 baseline supplies `uikTvd` and hierarchy identity, not a
current polling-room address. UIK numbers can be retained while physical rooms
move, and special UIKs change. A prior-election address may be retained only as a
diagnostic comparison, never as a published 2026 value without current official
confirmation.

### Inverting a home-address classifier

Enumerating every residential address to discover all UIKs would be expensive,
incomplete and hard to prove exhaustive. Use home-address search for the separate
`home address -> UIK` operation, not to build `UIK -> polling room`, unless a
regional application exposes a direct enumerable UIK-number route like Moscow.

## Required ingestion and release rules

Store one candidate row per source observation, then resolve candidates only
after validation. Minimum fields:

```text
subject_code
uik_number
uik_classifier_id
commission_org_external_id (nullable, CEC-specific)
uik_commission_address
uik_voting_address
uik_voting_phone
source_url
source_type
source_effective_date
retrieved_at
sha256
match_method
```

Validation rules:

1. Require an exact current-backbone `(subject_code, uik_number)` match. Never
   guess a subject from a globally repeated UIK number.
2. Keep the commission office and polling room separate. Prefer an explicitly
   labelled polling-room field; do not copy the commission address unless the
   source explicitly says both locations are the same.
3. Require a 2026 date, current 2026 election configuration, or an official page
   whose amendment chain proves that the record is current. A present-day HTTP
   200 alone is insufficient.
4. Parse all linked amendments newer than the base list before publishing.
5. If two current official sources disagree after address normalization, publish
   no winner automatically. Emit a conflict for review; source freshness and an
   explicit polling-room label are evidence, not permission to hide a conflict.
6. Record coverage by subject and source. Missing values remain missing.
7. Keep special/temporary UIKs and the 312 abroad UIKs as explicit coverage
   classes. Domestic regional APIs and ordinary local-administration lists cannot
   be assumed to cover them.

Suggested release gates:

- 100% of published rows join one current Duma backbone row exactly;
- 100% have an official source URL, retrieval time and content hash;
- zero unresolved conflicts in the user-facing subset;
- subject-level coverage is displayed, not summarized only as a national total;
- an address value is served only when the source passed the 2026 freshness gate.

## Concrete implementation order

1. Add an XLS/XLSX adapter and use the cached subject-79 file as the first
   end-to-end fixture. It should yield 160/160 exact addresses.
2. Add a DOCX-table adapter and a text-layer-PDF adapter, with strict header/label
   recognition and explicit unresolved outcomes.
3. Add the Moscow `findByNumber` adapter and reconcile all 1,442 current Moscow
   keys at a conservative rate. Investigate special UIK 9002 separately.
4. Add source-page version-chain metadata so a base act and all later amendments
   are processed together.
5. Rerun document triage over the 3,227 cached unsupported successful artifacts,
   ranked by 2026/UIK/address terms and likely regional coverage.
6. Refresh the CEC directory daily, accepting only exact subject + number matches;
   use it to verify addresses and fill genuine gaps as publication expands.
7. Build separate official-source adapters for temporary/special and abroad UIKs;
   do not hold ordinary domestic coverage hostage to those distinct cases.

This sequence can produce reliable regional coverage immediately while preserving
a path to nationwide coverage. Waiting for the federal `commissionOrg` directory
alone cannot.

## Probe record

All probes were bounded GET/POST requests following the official front-end flow.
No authentication response, API key, proxy URL or proxy credential was saved in
this report.

| Probe | Result |
| --- | --- |
| CEC challenge + authenticated API | Challenge and solve returned 200; ephemeral API key used only in memory |
| CEC `(25, 840)` organization search | 200, `empty: true`, SHA-256 `205e6573891bd64b9cece910c2d4363c671f40c73c05e04dc51794726dfa785a` |
| CEC `(25, 840, election 587813923)` classifier | 200, one exact UIK; SHA-256 `33d125f4200459719b0c848cab5bbc2f80c8774557011d3e1d5623569829f02e` |
| CEC classifier UUID passed to `/commissionOrg/parents` | 200, empty array; SHA-256 `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e26c4752f47f02e7619e158` |
| CEC classifier UUID passed to report 42 | 404; SHA-256 `a2b7a3029d46a4a09c83632373851af20c3158268c7c7e85c5e64cc08c1ea1e7` |
| CEC `(71, 1627)` organization search | 200, one exact row; SHA-256 `9100906c54329bce53e019ba697c06819d003678fd9df3eb211c8f73036dcd41` |
| CEC UIK 1627 report 42 | 200 with explicit `votingRoom`; SHA-256 `3eb1abb8183e8fa8b9438804217ca580f08b595a08ec6cb1890f8d729b80721c` |
| Moscow UIK 146 number lookup | 200, exact UIK and 2026 polling room/date; SHA-256 `28a472e8107c7f4ce8566d8733b697b86c0897f5ea563cef8c1bc7b44febbaa9` |
| Moscow systematic sample | 24/25 distinct current backbone numbers returned exact 2026 polling addresses; special UIK 9002 returned 404 |
| Saint Petersburg July 2026 PDF | 200, 16 text-readable pages; SHA-256 `f6e68c9464e157715e39d62d61f9737f0970dd3814a53638a778b67a4fa6d389` |
| Jewish Autonomous Oblast XLSX | 200; 160/160 distinct current backbone numbers matched; SHA-256 `4e8e06b2bef38567af5abbd7ee0b28ab3a4536fa69cfda70557ebfcf670b533a` |
| Magadan Oblast XLSX | 200; 80/93 current backbone numbers matched, no extras; SHA-256 `8319e9de44dfd77880395c34d7d7277e777a3cc752fc254a8770f88763f8fffb` |
| Mari El DOCX | 200; 491/494 current backbone numbers matched with voting addresses; SHA-256 `96b19aecaf8f46cbd3b238f8e168e16df8b99e130d76776ad083e75ba2629207` |
