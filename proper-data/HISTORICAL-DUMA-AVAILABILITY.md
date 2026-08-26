# Historical State Duma official-data reconnaissance

Controlled official-source probes covered 1993, 1995, 1999, 2003, 2007, 2011, and
2016. No nationwide historical crawl was started. Live official responses and errors
are checksum-addressed under the ignored `data/raw/gas-duma-history-live/` tree.

The earliest recovered official election result is the CEC's 1993 static HTML archive.
It contains national party results and single-member district summaries, but no
commission-level machine-readable protocols were found. The earliest recovered
machine-readable protocols are the 1995 CEC BIFF workbooks. The 1995 and 1999 party
and candidate workbooks use TIKs as columns; they do not expose UIKs or UIK-to-TIK
relations.

The earliest recovered official UIK result is 2003. Live `old.izbirkom.ru` supplies a
root hierarchy and load-on-demand child trees, direct TIK/UIK protocols, and reports
whose columns are individual UIKs. The same official endpoint family works for 2007,
2011, and 2016. The controlled matrix selected Kamchatka and Moscow, four TIKs per GAS
election, and up to three direct UIKs per TIK. All 108 protocol requests ultimately
returned HTTP 200 and passed offline validation.

Verified leaf report semantics differ by election:

| Year | Contests | Direct protocol types | UIK-column types | UIK recovery |
| --- | --- | --- | --- | --- |
| 2003 | party and single-member | party `430`, candidate `428` | party `431`, candidate `429` | both contests, direct and transposed |
| 2007 | party only | `242` | `233` | party, direct and transposed |
| 2011 | party only | `242` | `233` | party, direct and transposed |
| 2016 | party and single-member | party `242`, candidate `463` | party `233`, candidate `464` | both contests, direct and transposed |

The numbers are verified only in these election/level contexts. For example, 2003
`type=242` is a national party result, while the verified party leaf type is `430`.
This confirms that report numbers are not stable semantic identifiers by themselves.

The interface generations observed are:

- 1993: current CEC HTML summaries.
- 1995 and 1999: CEC HTML navigation plus downloadable OLE/BIFF workbooks.
- 2003–2016: Windows-1251 GAS result pages, UTF-8 JSON hierarchy endpoints, and
  randomized inline JavaScript that repairs decoy table cells in the browser.
- 2003 and 2016 insert OIK levels in the sampled branches; 2007/2011 Kamchatka exposes
  TIKs directly below the region while Moscow retains an OIK level.

The sample validator reconciled every numeric aggregate row to its UIK columns and
matched every sampled direct UIK and TIK row to the paired aggregate report. It covers
131 party and 131 candidate UIK columns in 2003, 139 party columns in 2007, 140 party
columns in 2011, and 157 party plus 157 candidate columns in 2016. Generated TypeScript
contains 84 direct protocols (24 TIK and 60 UIK) with hierarchy evidence and source
hashes.

Readiness:

- 1993 is aggregate-only and unavailable for a UIK crawl.
- 1995 and 1999 are ready for a TIK-workbook importer, not a UIK crawl.
- 2003, 2007, 2011, and 2016 have verified protocol formats, official UIK hierarchy,
  direct pages, and UIK-column reports. Before a nationwide collection, the controlled
  hierarchy traversal must be promoted into a recursive resumable planner and its
  complete coverage gate must be enforced.

The authoritative field-level matrix, evidence hashes, probe counts, missing elements,
and readiness states are in `proper-data/historical-duma-availability.json`.
