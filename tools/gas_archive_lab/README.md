# GAS archive lab

This directory is an intentionally isolated research workbench for recovering official
GAS Vybory result pages. Nothing here credits election coverage or writes to the
application database. A URL becomes production input only after its response has been
preserved, checksummed, parsed, and reconciled independently.

The tools use only the Python standard library. `HTTP_PROXY` and `HTTPS_PROXY` are
honored by `urllib`, so the local Outline HTTP CONNECT proxy can be selected without
putting an access key in this repository:

```sh
export HTTP_PROXY=http://127.0.0.1:18080
export HTTPS_PROXY=http://127.0.0.1:18080
```

Outputs should go below ignored `data/raw/gas-archive-lab/` and
`reports/generated/gas-archive-lab/` paths.

## 1. Extract the client-side hierarchy

CEC pages render much of their navigation from `tvdTreeJson`, not ordinary links.

```sh
python3 tools/gas_archive_lab/extract_tree.py \
  data/raw/duma-2021-single-member-cec/index/a52134a1b6d1e88209d6.html \
  --output reports/generated/gas-archive-lab/tree.json
```

The extractor records node text, `tvd`, `root`, region parameters, and URLs. It does
not infer nationwide OIK numbers from `root`; those values are internal hierarchy keys.

## 2. Probe a controlled URL matrix

The same report may exist under a regional subdomain, either `/region/izbirkom` path,
different `pronetvd`/`report_mode` values, HTTP or HTTPS, and several Wayback captures.

```sh
python3 tools/gas_archive_lab/probe_matrix.py \
  --url 'http://www.vybory.izbirkom.ru/region/izbirkom?...' \
  --wayback 20210928190257 --wayback 20211007073221 \
  --skip-live --rate 10 --limit 40 \
  --raw-dir data/raw/gas-archive-lab/matrix \
  --report reports/generated/gas-archive-lab/matrix.json
```

Every non-empty response is preserved byte-for-byte. The report records the requested
and final URL, status, media type, size, SHA-256, timing, and lightweight content signals.
HTTP errors remain observations rather than being discarded.

## 3. Ask Wayback's CDX index

CDX can locate captures whose timestamp or query spelling differs from a guessed URL.

```sh
python3 tools/gas_archive_lab/cdx_lookup.py \
  --url 'www.vybory.izbirkom.ru/region/izbirkom*' \
  --from-year 2021 --to-year 2022 --limit 500 \
  --output reports/generated/gas-archive-lab/cdx.json
```

Start narrow. Broad wildcard queries can be slow or rejected by the archive.

Fetch the exact indexed captures without regenerating or normalizing their URLs:

```sh
python3 tools/gas_archive_lab/fetch_cdx.py \
  reports/generated/gas-archive-lab/cdx.json \
  --limit 20 --rate 10 \
  --raw-dir data/raw/gas-archive-lab/cdx \
  --report reports/generated/gas-archive-lab/cdx-fetch.json
```

## 4. Generate exact children from saved pages

`extract_tree.py` output is designed to feed later probes. Prefer exact `href` values
from official saved pages over arithmetic guesses. For load-on-demand nodes, also probe
the official endpoint visible in the page JavaScript:

```text
/region/izbirkom?action=tvdTree&tvdchildren=true&vrn=<election-vrn>&tvd=<node-id>
```

## 5. Decode an obfuscated result table

Query CDX for the exact `.ttf` referenced by a saved page; the font often has a
different capture timestamp. Then decode the visible rows:

```sh
backend/.venv/bin/python tools/gas_archive_lab/decode_result.py page.html \
  --font captured-font.ttf \
  --output reports/generated/gas-archive-lab/decoded-table.json
```

## Promotion gate

A recovered result source is usable only when all of these hold:

1. The raw response is checksum-preserved.
2. It contains a result table, not merely navigation or a Wayback error page.
3. Election VRN and ballot/report type match 2021 Duma results.
4. Region, OIK, TIK, and UIK identities come from page evidence or a reconciled mapping.
5. Candidate/party totals reconcile upward to an official protocol.
