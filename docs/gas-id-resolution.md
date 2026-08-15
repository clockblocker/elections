# GAS commission-ID resolution research

Research date: 15 August 2026.

## Conclusion

The official GAS result hierarchy and the official GAS commission directory use different
identifier namespaces. Result pages expose election-scoped node IDs through `tvd`; commission
directory responses expose commission IDs through JSON node `id` and repeat them as
`action=ik&vrn=<id>`. No official source-provided crosswalk between those namespaces was found.

Consequently, the stored parent result responses can be parsed deterministically into individual
UIK *result* nodes, but they do not establish an exact commission `cik_uik.iz_id`. Traversing the
separate commission tree and joining by region, TIK wording, or UIK number would be a structural
or name match, not an exact identifier match. A resolver claiming exact identity must fail closed
unless a result artifact explicitly supplies a commission-directory ID or link.

## Sources and method

The investigation used only repository data/source code and preserved publisher responses:

- The checked-in acquisition manifest and the local files it authenticates: the 2021 result CSV
  (`b65fac633d642d496f52dc046fc4c1063b70b331cece7edcdfa8d76e63305a5d`) and 14 September
  2021 commission snapshot (`b4f4b68eecd2d51f365e9e2380181798eb74456e62e1aafa3226cb0f08f6827e`).
  See [`data/source-manifest.json`](../data/source-manifest.json).
- An [official GAS result response preserved on 7 October 2021](https://web.archive.org/web/20211007075141id_/http://www.vybory.izbirkom.ru/region/izbirkom?action=show&root=1000058&tvd=22220002523303&vrn=100100225883172&prver=0&pronetvd=null&region=22&sub_region=22).
- A second official GAS result response preserved in Common Crawl, identified below by exact WARC
  record coordinates.
- GIS-Lab's preserved original GAS commission HTML and JSON responses. GIS-Lab describes
  `cik_uik.iz_id` as the ID used by GAS and `url` as the commission information page, including an
  `action=ik&vrn=<iz_id>` example in its [source schema documentation](https://gis-lab.info/qa/cik-data.html#toc14).
  The original-response archive is
  [`cik_20210914_orig.7z`](https://gis-lab.info/data/cik/cik_20210914_orig.7z), SHA-256
  `10343b6ce5c6ada2276832520fa79460df896aecaa67a7ed48816707763de542`.
- The extractor that produced the commission snapshot. Its source shows the exact GAS request
  construction and records each `ikTree` child `id` as `iz_id`; see
  [`cik.py`](https://github.com/old-bibigon/parse-cik/blob/master/cik.py#L266-L304).

The former GAS result and regional commission hosts returned NXDOMAIN during this research, so
live requests could not fill preservation gaps. No national data, database, report, or temporary
download was added to Git.

## Official result navigation

A stored `result_records.source_url` is a parent commission page. In the local database, 96,325
result rows reduce to 2,877 distinct non-null source URLs. The URL's `vrn=100100225883172` is the
election ID; `tvd` identifies the selected node in that election's result tree.

The preserved Altai response embeds a `tvdTreeJson` object. Its selected TIK is:

```json
{"id":22220002523303,"text":"Алтайская ","isUik":false}
```

Its child for UIK 592 is:

```json
{
  "id": 9229002199809,
  "text": "УИК №592",
  "href": "region/izbirkom?action=show&root=222000022&tvd=9229002199809&vrn=100100225883172&prver=0&pronetvd=null&region=22&sub_region=22",
  "isUik": true
}
```

The same response's JavaScript loads unopened branches with:

```text
/region/izbirkom?action=tvdTree&tvdchildren=true&vrn=<election-vrn>&tvd=<result-node-id>
```

These fields are sufficient to enumerate UIK result pages. They are not commission-directory
fields: the response has no `action=ik` link, no commission `vrn`, and no occurrence of UIK 592's
commission ID `9229002166846`. The genuine compact result-tree extract is committed as
[`altai-result-tik-node.json`](../backend/tests/fixtures/gas/altai-result-tik-node.json), with
provenance in the adjacent [fixture README](../backend/tests/fixtures/gas/README.md).

## Official commission-directory navigation

The separate commission directory is traversable by region:

1. `action=ikTree&region=<region-code>` (the preserved extractor stores this initial response as
   `orig/<region>/ik/-1_childs.js`) returns the regional commission and its TIK children.
2. `action=ikTree&region=<region-code>&vrn=<tik-commission-id>&onlyChildren=true` returns UIK
   children as `{id, text, ...}` JSON objects.
3. Selecting a node navigates to `action=ik&vrn=<node-id>`.

This flow is shown both by the [extractor source](https://github.com/old-bibigon/parse-cik/blob/master/cik.py#L266-L304)
and by the preserved official response. For Altai commission TIK `22220001926860`, the first UIK
child is:

```json
{
  "id": "9229002166846",
  "text": "Участковая избирательная комиссия №592",
  "children": false
}
```

The corresponding official page is
`http://www.altai-terr.vybory.izbirkom.ru/region/altai-terr?action=ik&vrn=9229002166846`.
That page embeds the same ID in its initial `ikTree` request and labels the page “Участковая
избирательная комиссия №592”. The complete TIK-child response is committed as
[`altai-commission-tik-children.json`](../backend/tests/fixtures/gas/altai-commission-tik-children.json).

The region code therefore reaches the commission directory, but it does not choose the TIK that
corresponds to a result parent. The two hierarchies supply neither a shared TIK ID nor an explicit
parent cross-reference. Examples illustrate why TIK selection would require language-dependent
name interpretation:

| Region | Result `tvd` / label | Commission-tree ID / label |
|---|---|---|
| Altai | `22220002523303`, `Алтайская` | `22220001926860`, `Алтайская районная ТИК` |
| Adygea | `2012000461473`, `Адыгейская` | `4014001134922`, `ТИК города Адыгейска` |
| Moscow region | `25020003107322`, `Балашихинская городская` | `4504001942014`, `ТИК города Балашиха` |
| Sverdlovsk | `26620002596607`, `Верхнепышминская городская` | `4664011264186`, `Верхнепышминская городская ТИК` |

The right-hand rows are plausible comparison candidates from the commission snapshot, not an
officially asserted result-to-commission crosswalk.

## Verified identifier samples

Two preserved 2021 official result responses cover two regions and eight UIKs. In every sample,
the result leaf ID and commission ID differ. The parent responses contain no commission link.

| Region / parent | UIK | Result leaf `tvd` | Commission `iz_id` |
|---|---:|---:|---:|
| Altai / Алтайская | 592 | `9229002199809` | `9229002166846` |
| Altai / Алтайская | 593 | `9229002199810` | `9229002166848` |
| Altai / Алтайская | 594 | `9229002199811` | `9229002166850` |
| Altai / Алтайская | 595 | `9229002199812` | `9229002166852` |
| Krasnodar / Геленджикская | 901 | `4234008392567` | `4234008257083` |
| Krasnodar / Геленджикская | 902 | `4234008392568` | `4234008257085` |
| Krasnodar / Геленджикская | 903 | `4234008392569` | `4234008257090` |
| Krasnodar / Геленджикская | 904 | `4234008392570` | `4234008257092` |

The Krasnodar official result record is reproducible through the Common Crawl 2021-43 index:

```sh
curl -G 'https://index.commoncrawl.org/CC-MAIN-2021-43-index' \
  --data-urlencode 'url=http://www.vybory.izbirkom.ru/region/izbirkom?action=show&root=1000066&tvd=22320002829015&vrn=100100225883172&prver=0&pronetvd=null&region=23&sub_region=23&type=0&report_mode=null' \
  --data-urlencode 'output=json'
```

Its index metadata is timestamp `20211016015127`, digest
`MVGK5GRWYCI5TBIJWQWGKKYG2MZAD75M`, WARC
`crawl-data/CC-MAIN-2021-43/segments/1634323583408.93/warc/CC-MAIN-20211016013436-20211016043436-00155.warc.gz`,
offset `149088923`, length `14121`. The WARC record can be fetched from
`https://data.commoncrawl.org/<WARC-path>` with byte range `149088923-149103043`.

The non-constant sequences also rule out a justified arithmetic conversion. Across all 69,506
currently heuristic-linked local result rows, neither the stored URL's `tvd` nor its election
`vrn` equals the linked commission's `gas_vybory_id` once.

## Preservation gap: individual UIK result pages

No individual UIK result response was located for an exact-content check. Wayback availability
was checked for the Altai UIK 592 leaf with no `type`, `type=0`, and `type=242`; Common Crawl
2021-39, 2021-43, and 2021-49 exact queries also had no record. A 2016 Tatarstan leaf checked as a
second historical case was likewise absent.

This means the research cannot assert that *all possible* individual UIK result pages never
contained an `action=ik` link. It can assert that both inspected parent response shapes do not,
and it cannot supply a genuine individual-page fixture containing such a link. Tests must not
invent an anchor and present it as official evidence.

## Commission-snapshot integrity caveat

Not every imported `cik_uik.iz_id` is an official GAS ID. The raw snapshot contains 2,298
negative UIK/TIK `iz_id` values (2,284 UIKs and 14 TIKs). The snapshot extractor explicitly
creates these synthetic negative values when a reserve-tree entry cannot be joined to a real
commission, and skips parsing IDs below `-10` as fake; see
[`cik.py`](https://github.com/old-bibigon/parse-cik/blob/master/cik.py#L369-L440).

Of 98,866 raw UIK/TIK rows, 96,568 have a canonical `action=ik` URL whose `vrn` equals the
positive `iz_id`. The current importer retains 98,864 UIK/TIK rows and labels the negative
synthetic values as `gas_vybory_id`. Exact matching and coverage reports should exclude or
explicitly classify non-positive synthetic values; they are not externally verifiable GAS
commission identities.

Nor is `(region, parsed UIK number)` an exact substitute. The current database has 576 duplicate
`(commission.region, commission.number)` groups covering 2,742 rows. Names such as Udmurt
`№1/01`, `№1/02`, and similar variants are reduced to the same integer. An exact result must come
from an explicit source identifier, not from this lossy key.

## Resolver implications

- Deduplicate and cache the 2,877 parent result requests; parse their `tvdTreeJson` or
  `tvdTree` responses to enumerate UIK result nodes.
- Treat result-tree `id`/`tvd`, election `vrn`, and commission-directory `id`/`vrn` as distinct
  typed identifiers.
- Resolve only when a result-side artifact explicitly supplies an `action=ik&vrn=<id>` link or an
  equally explicit commission-ID field. Validate that the ID is positive and exists exactly once
  in the selected commission snapshot.
- Do not promote exact TIK/UIK label matching, numeric UIK matching, address matching, or an
  inferred arithmetic relation to “exact GAS identity”. Those may remain audited heuristics.
- Classify the researched parent shape as `no_explicit_commission_id`, not as a parser failure.
- Preserve the exact result artifact and the exact commission-link artifact separately if a future
  or previously unpreserved response exposes a bridge.

On the evidence available here, the exact-confidence coverage obtainable from the inspected
parent result shape is zero. That is a coverage limitation, not a reason to weaken the identity
criterion.
