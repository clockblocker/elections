# Wayfinder

## Goal

Build an interactive, reproducible Shpilkin-style analysis of Russian federal elections. Users must be able to inspect the underlying precinct data, change analytical assumptions, and see how those choices affect estimated excess votes.

## MVP boundary

The first complete vertical slice is the **2021 State Duma election**:

- federal party-list results at UIK level;
- single-member results at UIK level;
- region, district, TIK, and UIK hierarchy;
- DEG represented explicitly, never silently merged with physical precincts;
- UIK/TIK membership snapshot from 14 September 2021;
- interactive turnout and vote-share analysis for party-list and district candidates.

Presidential elections, the 2020 constitutional vote, and earlier Duma elections follow only after the 2021 pipeline is reproducible and validated.

## Product principles

1. **Raw evidence is immutable.** Preserve source URL, retrieval time, checksum, and original payload.
2. **Every derived number is reproducible.** Analysis parameters and code version accompany outputs.
3. **Assumptions are visible.** Baseline regions, smoothing, exclusions, and thresholds are user-controlled or clearly stated.
4. **Official totals are validation targets, not substitutes for precinct data.**
5. **Missing and anomalous records remain visible.** Do not coerce them into plausible values.

## System shape

```text
CEC and archived sources
        │
        ▼
raw snapshots + manifest
        │
        ▼
parser / normalizer / validation
        │
        ▼
analytical database
        │
        ├── API / query layer
        │
        ▼
React client
```

MySQL 8.4 is the reproducible development database. The SQLAlchemy boundary and SQLite test coverage keep ingestion and queries portable without weakening the MySQL migration target.

## Core data model

```text
elections
ballots
geographies
commissions
commission_memberships
precincts
ballot_accounting
options                 # party or candidate
votes
source_artifacts
ingestion_runs
analysis_runs
```

Important identities:

- commissions retain both internal IDs and original GAS Vybory IDs;
- UIKs belong to TIKs; TIKs map to regions and, where applicable, districts;
- votes are long-form: one row per precinct, ballot, and option;
- commission membership is time-bounded and linked to its source snapshot.

## Interactive analysis

Minimum views:

1. Turnout histogram, weighted by registered voters or precinct count.
2. Turnout versus party vote-share scatterplot.
3. Vote distributions by turnout bin.
4. Configurable baseline and excess-vote estimate.
5. Geography filters: country, region, district, TIK, UIK.
6. Physical voting and DEG shown separately and together.
7. Drill-through from every aggregate to contributing precinct records.

Each chart must expose its denominator, filters, bin width, and excluded records.

## Delivery path

### 1. Acquire

- Download 2021 party-list and single-member results.
- Download the 14 September 2021 commission snapshot.
- Record provenance and checksums.

**Exit:** raw inputs can be re-fetched or independently verified.

### 2. Normalize

- Parse the CEC hierarchy and both ballot types.
- Load commissions and memberships.
- Produce deterministic database migrations and imports.

**Exit:** each source record has a traceable normalized representation.

### 3. Validate

- Reconcile UIK totals through TIK, district, region, and national levels.
- Check ballot-accounting identities.
- Report missing commissions, duplicate IDs, and mismatches without suppressing them.

**Exit:** validation report explains every unresolved discrepancy.

### 4. Analyze

- Specify the Shpilkin calculation mathematically.
- Implement parameterized, testable calculations.
- Store analysis parameters and outputs.

**Exit:** a scripted 2021 reference run is reproducible.

### 5. Visualize

- Build the React client and query layer.
- Add linked filters, charts, parameter controls, and data drill-through.

**Exit:** changing a parameter updates the estimate and its supporting charts while retaining a shareable analysis state.

### 6. Verify and publish

- Test the full path from raw artifact to browser output.
- Document methodology, limitations, and known data gaps.
- Add deployment only after local reproducibility is established.

## Ticket breakdown

The executable MVP queue lives in [GitHub Issues](https://github.com/clockblocker/elections/issues). Tickets are ordered by dependency, not necessarily by final execution order.

| # | Ticket | Depends on |
|---|---|---|
| [1](https://github.com/clockblocker/elections/issues/1) | Bootstrap the local development environment | — |
| [2](https://github.com/clockblocker/elections/issues/2) | Define the MySQL schema and migrations | 1 |
| [3](https://github.com/clockblocker/elections/issues/3) | Create the source manifest and deterministic downloader | — |
| [4](https://github.com/clockblocker/elections/issues/4) | Import 2021 State Duma party-list UIK results | 2, 3 |
| [5](https://github.com/clockblocker/elections/issues/5) | Import the 14 September 2021 UIK and TIK snapshot | 2, 3 |
| [6](https://github.com/clockblocker/elections/issues/6) | Match election results to UIK and TIK metadata | 4, 5 |
| [7](https://github.com/clockblocker/elections/issues/7) | Validate and reconcile the 2021 dataset | 4, 6 |
| [8](https://github.com/clockblocker/elections/issues/8) | Implement the read-only FastAPI exploration API | 2, 6, 7 |
| [9](https://github.com/clockblocker/elections/issues/9) | Build the custom-styled React research interface | 1, 8 |
| [10](https://github.com/clockblocker/elections/issues/10) | Implement the WebGL Shpilkin-style scatterplot | 8, 9 |
| [11](https://github.com/clockblocker/elections/issues/11) | Add UIK hover, selection, and full metadata inspection | 8, 10 |
| [12](https://github.com/clockblocker/elections/issues/12) | Add exports and verify the complete local MVP | 7, 10, 11 |
| [14](https://github.com/clockblocker/elections/issues/14) | Acquire and preserve 2021 single-member UIK result sources | 3 |
| [15](https://github.com/clockblocker/elections/issues/15) | Model district-scoped ballots and candidates | 2 |
| [16](https://github.com/clockblocker/elections/issues/16) | Import 2021 single-member UIK candidate results | 14, 15 |
| [17](https://github.com/clockblocker/elections/issues/17) | Validate and reconcile all 225 single-member districts | 16 |
| [18](https://github.com/clockblocker/elections/issues/18) | Add candidate and district exploration to the API and UI | 16, 8–11 |
| [19](https://github.com/clockblocker/elections/issues/19) | Verify and export the complete two-ballot dataset | 12, 17, 18 |

Each issue contains its own outcome, scope, and acceptance criteria. Update the issue first when ticket scope changes, then keep this dependency map aligned.

## Implementation decisions

- Python owns ingestion, matching, validation, and the FastAPI read-only query layer.
- MySQL 8.4 is the local application database; SQLite is used for fast isolated tests.
- React/Vite and regl provide the browser workbench and WebGL point rendering.
- Raw archives live under ignored `data/raw/`; only the URL/checksum manifest is committed.
- The first delivered graph is the descriptive turnout/result field. A parameterized excess-vote estimator remains a later analytical slice and must define its mathematics before implementation.

## Definition of done for 2021

- A fresh checkout can build the database from documented inputs.
- Party-list national totals and all 225 single-member OIK totals reconcile to published CEC totals.
- The UI displays linked turnout, vote-share, and excess-vote views.
- DEG treatment is explicit.
- Every estimate is reproducible from saved parameters.
- Known limitations and data discrepancies are documented.
