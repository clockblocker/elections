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
- [CEC rules and result-table specifications](https://www.garant.ru/products/ipo/prime/doc/402591545/)
- [GIS-Lab: machine-readable commission data and 14 September 2021 snapshot](https://gis-lab.info/qa/cik-data.html)
- [CEC archive-content rules](https://normativ.kontur.ru/document?documentId=127366&moduleId=1)
