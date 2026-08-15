# 2021 data pipeline

Run commands from any directory after installing the backend with
`pip install -e 'backend[dev]'`. Set `DATABASE_URL` to the MySQL URL used by the
application. The CLI also accepts `--database-url` before the subcommand.

```sh
elections-data migrate
elections-data download
elections-data import-results
elections-data import-commissions
elections-data match
elections-data validate
```

The committed source manifest pins the preserved party-list CSV and 14 September
2021 GIS-Lab commission snapshot by URL, byte size, and SHA-256. Downloads go to
the Git-ignored `data/raw/` directory. An existing changed file fails verification;
`--force` is required to replace it.

Imports are restartable per source artifact. Every normalized result retains its
artifact, source row, raw row, and original result URL. Malformed rows are retained
in `import_rejects`. Commission raw JSON excludes phone, fax, and email fields;
those contacts are never part of the exploration contract.

Matching first uses a stable GAS Vybory ID. The fallback uses UIK number plus
normalized region/address and parent-TIK evidence. Every candidate and score is
stored in `match_evidence`; a tie is classified `ambiguous`, never silently chosen.
The default audit is `reports/generated/matches.json`.

Validation checks the two ballot-accounting identities, party-vote totals,
registered-voter bounds, duplicate or missing UIK identities, and unresolved
matches. It aggregates TIK, region, and national totals with DEG in a separate
bucket. By default it loads `data/published-totals-2021.json`, transcribed from the
CEC final national protocol, and compares all 12 accounting values and all 14 party
totals. The API reports `reconciled=false` when no published totals exist, any
validation errors remain, or any result has not been validated. Known exceptions
remain enumerated in `reports/generated/validation.json`; they are not suppressed.

The expected complete result import is 96,325 records and 1,348,550 vote rows.
Import and validation commands print machine-readable counts for automation.
