# Proper data

This tree stores evidence-linked election protocols as generated TypeScript files.
It deliberately has no database layer.

- `crawler/` discovers, preserves, decodes, reconciles, and generates protocols.
- `<year>-duma/protocol/tic/<type>/` stores TIK summary protocol shards. The directory
  name `tic` follows the requested on-disk convention; values retain the official TIK terminology.
- `<year>-duma/protocol/uik/<type>/` stores UIK protocol shards, including UIKs
  transposed from an official TIK column report with that derivation recorded.
- `<year>-duma/uik-to-tik.ts` and `uik-to-tik/region-*.ts` store the exact UIK-to-TIK
  relation recovered from GAS.
- `<election>/DEG/msk/` stores Moscow DEG output and provenance. Moscow's observer
  platform is crawled independently from the federal DEG platform.
- `<election>/DEG/fed/` stores non-Moscow federal DEG output and provenance. DEG is
  never folded into physical UIK protocols without an explicit combined view.
- `2026/` stores the crawler and current official campaign/UIK/candidate-set snapshot
  for every federal, regional, and local campaign scheduled on that voting date. It
  keeps inactive candidates and uncertain association registration marks explicitly
  labeled, together with central and regional candidate declarations and separately
  classified findings of inaccurate disclosures.

Nationwide output is deterministically sharded by region. Generated constants use
`duma_<year>_...`, because a TypeScript identifier cannot legally begin with a year.

Only elections that actually used DEG have a `DEG/` directory. See
[`crawler/DEG.md`](crawler/DEG.md) for the affected-election inventory, acquisition
endpoints, raw layout, and validation gates.
