# 2000 presidential election official-data reconnaissance

## Crawl seed

The archived official election page identifies the election as **“Выборы Президента
Российской Федерации”**, with voting on **26 March 2000**, and exposes election VRN
`10010001189753`. Its official report links identify CEC/root TVD
`10010001189756`.[Official CEC/GAS election page (Wayback capture)](https://web.archive.org/web/20150329021902/http://izbirkom.ru/region/adygei?action=show&vrn=10010001189753)

Use these exact seed values:

| Field | Value |
| --- | --- |
| Election slug | `2000-president` |
| Election VRN | `10010001189753` |
| CEC/root TVD | `10010001189756` |
| Root tree | `http://old.izbirkom.ru/tvdTree?vrn=10010001189753&tvd=10010001189756` |
| Candidate registry | `type=221`, election-scoped `vibid=10010001189753` |
| Direct commission protocol | `type=226`, commission-scoped `vibid=<commission TVD>` |
| Lower-commission column report | `type=227`, linked by GAS but not populated by the live archive |

The archive's own links establish the three report types. The campaign page links its
candidate list as `type=221`, its result as `type=226`, and its summary table as
`type=227`.[Official CEC/GAS election page (Wayback capture)](https://web.archive.org/web/20150329021902/http://izbirkom.ru/region/adygei?action=show&vrn=10010001189753)
The archived `type=226` result is a normal presidential protocol: it has accounting
rows, eleven candidate rows, and “Против всех”.[Official CEC/GAS type 226 result](https://web.archive.org/web/20130604091722/http://www.adygei.vybory.izbirkom.ru/region/region/adygei?action=show&root=1&tvd=10010001189756&vrn=10010001189753&region=1&global=&sub_region=1&prver=0&pronetvd=null&vibid=10010001189756&type=226)

## Endpoint shapes

The national entry page should be probed first with the standard GAS parameters:

```text
http://old.izbirkom.ru/region/izbirkom?action=show&global=1&vrn=10010001189753&region=0&prver=0&pronetvd=0
```

The canonical national result URL assembled from the official archive's exact VRN,
TVD, VIBID, and report type is:

```text
http://old.izbirkom.ru/region/region/izbirkom?action=show&root=1&tvd=10010001189756&vrn=10010001189753&region=0&global=1&sub_region=0&prver=0&pronetvd=null&vibid=10010001189756&type=226
```

The archived copy uses the regional hostname and slug
`www.adygei.vybory.izbirkom.ru/region/region/adygei` even while displaying the CEC
national result. Therefore the crawler must not infer scope from host or path slug;
scope comes from VRN, TVD, breadcrumb/commission name, and report contents. Follow
official links where possible, then canonicalize the host to `old.izbirkom.ru` only as
the transport fallback.[Official CEC/GAS type 226 result](https://web.archive.org/web/20130604091722/http://www.adygei.vybory.izbirkom.ru/region/region/adygei?action=show&root=1&tvd=10010001189756&vrn=10010001189753&region=1&global=&sub_region=1&prver=0&pronetvd=null&vibid=10010001189756&type=226)

For hierarchy discovery, start with the root-tree URL above. For every node marked as
having unloaded children, use the same JSON child endpoint used by the adjacent 2004
presidential archive:

```text
http://old.izbirkom.ru/region/izbirkom?action=tvdTree&tvdchildren=true&vrn=10010001189753&tvd=<exact-parent-TVD>
```

This child endpoint was verified live in September 2026. It exposes 90 regional
branches, 2,632 TIKs, and 91,333 UIKs without synthesized descendant IDs.

For a discovered commission, preserve the official link's `root`, `region`,
`sub_region`, and path slug. The expected result URL is:

```text
http://old.izbirkom.ru/region/region/<slug>?action=show&root=<tree-root>&tvd=<commission-TVD>&vrn=10010001189753&region=<region>&global=null&sub_region=<region>&prver=0&pronetvd=null&vibid=<commission-TVD>&type=<226-or-227>
```

Live probes in September 2026 found that exact type-227 TIK links return election
navigation pages without protocol tables, while the same TIK and its child UIKs return
valid type-226 aggregate/direct protocols. Configure both TIK and UIK acquisition as
type 226, distinguish their scope from the hierarchy/request class, and recover every
UIK directly. Preserve the failed type-227 observations as evidence; do not treat the
linked report type as proof that a result table is still exposed.

## Important archive quirk: the GAS national total is not final

The recovered GAS `type=226` national page says there are 90 subject commissions but
only 89 had begun transmitting. Its totals are consequently smaller than the legally
final CEC result: for example, it reports `108,566,269` listed voters and `39,585,360`
votes for Putin.[Official CEC/GAS type 226 result](https://web.archive.org/web/20130604091722/http://www.adygei.vybory.izbirkom.ru/region/region/adygei?action=show&root=1&tvd=10010001189756&vrn=10010001189753&region=1&global=&sub_region=1&prver=0&pronetvd=null&vibid=10010001189756&type=226)
The CEC's static 5 April workbook records `109,372,046` listed voters and `39,740,434`
votes for Putin. The CEC's later Resolution 106/1149-3 corrects those and other values;
the source-controlled static build applies all 57 old-to-new evidence pairs and
publishes `39,740,467` votes for Putin while retaining the original workbook version.
[Official CEC 2000 presidential archive](http://www.cikrf.ru/banners/vib_arhiv/president/2000/)

Consequences for the crawl:

- Do not use the root `type=226` totals as the nationwide coverage oracle or published
  final result.
- Require the hierarchy to account for every branch returned by the root, and report
  branches with no descendants or no valid result table explicitly.
- Reconcile direct UIK sums to their immediate TIK `type=226` totals first. Only compare the
  national sum with the final CEC resolution after documenting any archive gap.
- Keep exact raw pages and retrieval metadata. The page calls the data “Итоги
  голосования” and shows a signing time in 2004, so timestamp alone does not make it a
  complete final aggregation.

## Candidate registry and ballot expectations

The official campaign page exposes the candidate registry at:

```text
http://old.izbirkom.ru/region/region/izbirkom?action=show&root=1&tvd=10010001189756&vrn=10010001189753&region=0&global=1&sub_region=0&prver=0&pronetvd=null&vibid=10010001189753&type=221
```

The selected Wayback timestamp does not preserve that target, so the live probe must
validate its rows and statuses before configuring count gates. The result protocol
itself proves the final ballot shape: eleven named candidates plus “Против всех”. The
registry may include withdrawn/non-ballot registrations and should not be gated to
eleven until parsed.

## Readiness decision

The election has enough official identifiers to begin a controlled crawl, but not to
skip the probe stage. Promotion to a nationwide run should require:

1. a valid root hierarchy for VRN `10010001189753` and root TVD `10010001189756`;
2. successful recursive child discovery without synthesized IDs;
3. decoded type-226 TIK aggregates and paired direct type-226 UIK protocols in at least
   two regions;
4. exact agreement between sampled UIK sums and TIK aggregates;
5. a parsed `type=221` registry with explicit registered/withdrawn status; and
6. a coverage report that keeps the incomplete GAS national aggregate distinct from
   the CEC's legally final result.
