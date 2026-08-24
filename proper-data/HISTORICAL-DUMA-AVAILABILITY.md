# Historical State Duma official-data reconnaissance

Controlled probes covered 1993, 1995, 1999, 2003, 2007, 2011, and 2016. No nationwide
historical crawl was started. Raw responses are checksum-addressed below the ignored
`data/raw/gas-duma-history/` tree; the checked-in request inventory makes the probes
repeatable.

The earliest recovered official result is the CEC's static 1993 HTML archive. It has
national party results and single-member district summaries, but the probed entry does
not expose commission-level machine-readable protocols. The earliest recovered official
machine-readable protocols are the 1995 CEC workbooks. Separate `.xls` files contain
party and candidate results with territorial commissions as columns. The 1999 archive
publishes the same two-contest/TIK pattern. Neither workbook generation contains UIK
columns, UIK identifiers, or a UIK-to-TIK mapping.

GAS Vybory pages are empirically available from 2003. The recovered 2003, 2007, 2011,
and 2016 national pages are Windows-1251 plain HTML. `type=242` is not a stable semantic
level: it is a national party summary at the root, a region or district summary at some
intermediate selections, and a direct TIK protocol for the verified 2011 TIK samples.
This differs from 2021 and confirms that type numbers must be registered against page
contents and selected hierarchy level.

The strongest complete preservation/decoding flow is 2011. National and two-region
navigation, two Kamchatka TIK protocols, and two Moscow district branches were recovered.
Both generated Kamchatka TIK samples preserve all 18 accounting labels and seven party
rows, and each reconciles the party vote sum to the official valid-ballot row. The
central page explicitly delegates UIK navigation to each regional election site;
neither the generic handoff nor bounded regional-mirror guesses produced an archived
UIK page, so UIK recovery is not claimed.

Readiness by generation:

- 1993: aggregate official summaries only; not ready for a UIK crawl.
- 1995 and 1999: ready for a separate TIK-workbook importer, not a UIK crawl.
- 2003: national GAS party summary recovered; regional mirrors and candidate reports
  remain unresolved.
- 2007: party-list-only election; national and region-level pages are partly recovered,
  but deeper generic-host captures are missing.
- 2011: party-list-only election; direct TIK protocols are verified and generated,
  while regional UIK pages remain unresolved.
- 2016: mixed party/candidate election; the party root and OIK navigation are recovered,
  but TIK/UIK descendants and the candidate report registry remain unresolved.

No election is yet evidence-complete for a nationwide UIK crawl. The next work should
search exact regional mirror host/path spellings through bounded CDX queries, recover
their hierarchy pages, then verify each candidate/party report by contents before adding
it to a per-election registry. The live legacy host was unreachable from this execution
environment; that is recorded as a route-specific failure, not evidence that the live
archive is absent.

The machine-readable detail, evidence hashes, missing elements, and readiness states are
in `proper-data/historical-duma-availability.json`.
