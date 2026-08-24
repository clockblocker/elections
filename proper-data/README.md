# Proper data

This tree stores evidence-linked election protocols as generated TypeScript files.
It deliberately has no database layer.

- `crawler/` discovers, preserves, decodes, reconciles, and generates protocols.
- `2021-duma/protocol/tic/<type>/<id>.ts` stores TIK summary protocols. The directory
  name `tic` follows the requested on-disk convention; values retain the official TIK terminology.
- `2021-duma/protocol/uik/<type>/<id>.ts` stores individual UIK protocols.
- `2021-duma/uik-to-tik.ts` stores the exact UIK-to-TIK relation recovered from GAS.

The generated constants use `duma_2021_...`, because a TypeScript identifier cannot
legally begin with `2021_`.
