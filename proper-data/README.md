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

Nationwide output is deterministically sharded by region. Generated constants use
`duma_<year>_...`, because a TypeScript identifier cannot legally begin with a year.
