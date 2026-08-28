# Separate DEG crawls

DEG is a separate evidence family, not another GAS UIK page. The repository therefore
keeps Moscow and the rest of the country in independent trees:

```text
proper-data/<election>/DEG/msk/
proper-data/<election>/DEG/fed/
```

Within the elections currently represented by `proper-data`, DEG affected only
`2021-duma` and `2024-president`. Earlier directories predate its use in those federal
elections. The machine-readable acquisition matrix is
[`deg-elections.json`](deg-elections.json).

## Federal platform (`DEG/fed`)

The federal observer portal publishes one voting contract per district and hourly ZIP
exports per contract. Discovery and downloading must be separate phases: first save a
catalog of regions, elections, districts, contract IDs, and filenames; then stream the
cataloged files without rediscovering IDs on resume.

The API changed between the two elections:

| Election | Regions outside Moscow | Catalog API | File API | Authentication |
| --- | ---: | --- | --- | --- |
| 2021 Duma | 6 | `GET /api/elections?regionCode=...`, then districts and voting statistics | `GET /api/transactions/filenames/{contract}` and `GET /download/{contract}/{file}` | none |
| 2024 president | 28 | `GET /api/voting/regions`, region elections, districts, then voting statistics | `GET /api/files/{contract}` and `POST /api/files/{file}/contract/{contract}/download` | observer ESIA bearer token |

For 2024, read the token only from `PROPER_DATA_DEG_FED_TOKEN` (or an explicitly
ignored token file). Never print it, put it in a URL, save request headers, or commit it.
The existing `PROPER_DATA_PROXY_URL` handling applies to both discovery and downloads.
The 2021 acquisition code and its original six-region seed are preserved in the
historical `VotingFilesDownloader` implementation linked under Sources.

Use this raw layout:

```text
data/raw/deg-<election>/fed/catalog.json
data/raw/deg-<election>/fed/<region>/<election-id>/<district-id>/<contract-id>/<hour>.zip
data/raw/deg-<election>/fed/manifest.json
```

Downloads must be streamed to a `.part` file, hashed while streaming, fsynced, and
atomically renamed. A restart skips a file only after checking its recorded byte length
and SHA-256. Reject path separators in API-provided filenames. Keep every hourly file;
do not retain only the last hour, because the exports are intervals rather than
cumulative snapshots.

## Moscow platform (`DEG/msk`)

Moscow did not use the federal observer platform and has two incompatible export
families of its own.

### 2021 Duma

The archived official observer configuration maps server 1 to the Duma single-member
contest and server 2 to the federal party-list contest. Each server exposes its own
observer API and `GET /files` dump link:

```text
https://observer.mos.ru/1/observer/files  single-member contests
https://observer.mos.ru/2/observer/files  federal party list
```

Store the two SQL gzip dumps independently:

```text
data/raw/deg-2021-duma/msk/single-member/observer.sql.gz
data/raw/deg-2021-duma/msk/federal-list/observer.sql.gz
```

These are multi-gigabyte files and must use the same streaming and atomic-publication
rules as the federal ZIP files. Historical reference hashes are for the *uncompressed*
SQL: `af3ca1f9002a7bc92065fd696e642fca84691dff7a3d8ee5165c009513082c66`
for single-member contests and
`63f0cea15928ed31b1dceaaa74d2651fd901be17624bd2435ea925037fa3abec`
for the party list. Record the compressed-file SHA-256 as well.

### 2024 president

The observer frontend changed to a single archive. Do not hard-code the archive URL:

1. Fetch `/config.js` and read the public `APP_TOKEN` value at runtime.
2. `POST {}` to `/api/elect-observer-service/all_votings` with that value in
   `x-application-token`.
3. Verify that the returned voting metadata matches the expected election and save the
   response as the catalog.
4. Stream the returned `dumpUrl` to a timestamped 7z file.

The 7z contains NDJSON transaction chunks named by their starting block and companion
MD5 files. During a live election the dump is cumulative and mutable, so timestamp and
hash every observation. The final post-close snapshot is the promotion candidate;
earlier snapshots remain useful evidence that old blocks did not change.

```text
data/raw/deg-2024-president/msk/catalog.json
data/raw/deg-2024-president/msk/snapshots/<retrieved-at>-<sha256>.7z
data/raw/deg-2024-president/msk/manifest.json
```

The live official sites now describe the current election rather than the historical
one. A historical crawl must therefore start from a previously saved official catalog
or a clearly marked preservation mirror. Mirror bytes never receive `live-official`
provenance, and their checksums must be retained.

## Promotion and validation

Raw blockchain exports stay under ignored `data/raw/`. Promote only deterministic,
compact results and their source manifest into the matching `DEG/msk` or `DEG/fed`
directory.

- For federal ZIPs, run the matching CEC `observer-tools` release per contract, reject
  duplicate filename/hash conflicts, and reconcile reconstructed totals with the
  published DEG protocol.
- For Moscow 2021, verify gzip integrity and both uncompressed hashes before importing
  the SQL dumps read-only.
- For Moscow 2024, test the 7z, verify every companion MD5, parse every NDJSON line, and
  check block continuity and duplicate transaction hashes before deriving results.
- Preserve platform, region, district, contract, ballot/contest, retrieval time, source
  URL, provenance, byte length, and SHA-256 in every promoted manifest.
- Never assign DEG records to physical UIKs. A combined physical-plus-DEG result must be
  an explicit derived view with both source families named.

## Sources

- Federal observer portal: <https://stat.vybory.gov.ru/>
- Moscow observer portal: <https://observer.mos.ru/>
- CEC verifier source for 2021: <https://github.com/cikrf/deg2021>
- CEC verifier source for 2024: <https://github.com/cikrf/deg2024>
- Historical federal 2021 downloader: <https://github.com/AlexeiScherbakov/Voting2021/tree/55805b7af26420baaf4b8a8c91bc6dbfb04d5603/src/VotingFilesDownloader>
- Archived Moscow 2021 observer configuration: <https://web.archive.org/web/20210921191542id_/https://observer.mos.ru/all/assets/config/config.json?1631923685558>
- Moscow 2021 dump layout and reference hashes: <https://github.com/50000-Quaoar/election2021_msk>
- Historical 2024 preservation mirror (fallback only): <https://data.deg.observer/2024/>
