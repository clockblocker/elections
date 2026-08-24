# Russian federal election data

## Scope

Nationwide votes from 2000 through 2024:

| Type | Years |
|---|---|
| State Duma | 2003, 2007, 2011, 2016, 2021 |
| President | 2000, 2004, 2008, 2012, 2018, 2024 |
| Constitutional amendments vote | 2020 |

The 2020 event was officially an *All-Russian vote* (`общероссийское голосование`), not a referendum.

The CEC archive reaches back to 12 December 1993. The first event in this project's scope is the presidential election of 26 March 2000.

## First target: 2021 State Duma election

Voting took place on 17–19 September 2021. There were two ballots and therefore two result datasets:

1. **Federal party list:** 12 ballot-accounting fields plus votes for each of 14 parties.
2. **Single-member district:** the same accounting fields plus votes for each candidate in the district.

Results follow this hierarchy:

```text
Russia
└── federal subject
    └── single-member district / district commission (OIK)
        └── territorial commission (TIK)
            └── precinct commission (UIK)
```

The archive primarily presents results as parameterized HTML tables. In the lowest-level reports, columns are UIKs and rows are accounting fields, parties, or candidates. Remote electronic voting (`Дистанционное электронное голосование`, DEG) is a separate column. The CEC specification also requires final results to be published as open-data exports.

CEC-defined result forms:

| Form | Contents |
|---|---|
| 1.28 | Federal party-list totals |
| 1.29 | Federal party-list summary by subordinate commission |
| 1.31 | Single-member district totals |
| 1.32 | Single-member district summary by subordinate commission |

Recommended normalized model:

```text
precincts(election, region, district, tik, uik, deg_flag, accounting fields...)
votes(election, ballot_type, region, district, tik, uik, option, votes)
```

A long `votes` table avoids schema changes when parties and candidates differ between elections.

## Preserved 2021 single-member source

The acquisition seed for the single-member ballot is the Internet Archive capture from
**28 September 2021 at 19:02:57 UTC** of the official CEC/GAS Vybory form 1.31/1.32
results index (`vrn=100100225883172`, `type=463`). This is preferred to the mutable
live portal: the publisher is still the CEC, while the response is addressed through a
dated preservation service. Links discovered in that response are rewritten through the
same Wayback timestamp before they are fetched.

`data/single-member-sources-2021.json` is the committed acquisition plan. It explicitly
enumerates OIK numbers 1 through 225, pins the election identifier and capture timestamp,
limits discovery to the preservation host, and sets a ten-request-per-second crawl rate.
The national seed is only a discovery page. For production UIK acquisition, the
checksum-pinned party-list CSV supplies the actual `(region, OIK, TIK, UIK, URL)`
inventory and `--party-list-archive` turns each distinct TIK URL into a `type=463`
single-member result seed. OIK coverage is credited only when an OIK-scoped response is
preserved; opaque CEC `root` identifiers are never treated as OIK numbers.

Raw responses are stored byte-for-byte below
`data/raw/duma-2021-single-member-cec/`. The generated
`reports/generated/single-member-snapshot.json` is the parser boundary and contains:

- an explicit `expected_oiks` list and `coverage.by_oik` entries for all 225 districts;
- each payload's OIK/TIK/UIK hints, original and final URL, retrieval time, relative raw
  path, byte size, media type, HTTP status, and SHA-256;
- for obfuscated 2021 pages, the archived font URL, path, size, and SHA-256 needed to
  reproduce visible digits without running publisher JavaScript;
- `unavailable`, `malformed`, `redirected`, and `inconsistent` gap records instead of
  silently dropping source material.

The hierarchy hints are acquisition metadata, not normalized election results. Candidate
names, accounting fields, and vote values must be parsed from verified raw payloads by the
single-member importer. A Wayback redirect is preserved and checksummed but also remains
visible in the gap report because it may point at a different capture time.

## UIK and TIK leadership

Leadership is not included in the result tables. It is published separately in the CEC commission directory.

A nationwide GIS-Lab extraction dated **14 September 2021**—three days before voting began—is available as CSV, SQLite, and source HTML. It contains:

- UIK/TIK identifiers and parent-child relationships;
- commission name, type, address, and contacts;
- each member's full name and role;
- the organization or party that nominated the member.

Relevant tables:

```text
cik_uik(id, iz_id, parent_id, type_ik, region, name, address, ...)
cik_people(id, ik_id, fio, post, party)
```

Commission types are `uik` and `tik`. Chairpersons have `post = 'Председатель'`. Join `cik_people.ik_id` to `cik_uik.id`.

### Caveat

The membership snapshot predates voting by three days. Check later appointment/replacement decisions for last-minute changes. The current CEC directory is mutable and should not be treated as a historical snapshot.

## Sources

- [CEC/GAS Vybory portal](http://www.cikrf.ru/gas/)
- [Frozen CEC single-member results index (28 September 2021)](https://web.archive.org/web/20210928190257/http://www.vybory.izbirkom.ru/region/region/izbirkom?action=show&root=1&tvd=100100225883177&vrn=100100225883172&region=0&global=1&sub_region=0&prver=0&pronetvd=0&vibid=100100225883177&type=463)
- [CEC rules and result-table specifications](https://www.garant.ru/products/ipo/prime/doc/402591545/)
- [GIS-Lab: machine-readable commission data and 14 September 2021 snapshot](https://gis-lab.info/qa/cik-data.html)
- [CEC archive-content rules](https://normativ.kontur.ru/document?documentId=127366&moduleId=1)
