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
- interactive turnout and vote-share analysis for the party-list ballot.

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

The 2021 dataset fits comfortably in SQLite. Keep ingestion and query boundaries database-agnostic so PostgreSQL remains an uncomplicated deployment option.

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

## Decisions to make before implementation

- Ingestion language and parser ownership.
- SQLite-only local application versus PostgreSQL-backed deployment.
- API framework and deployment target.
- Exact mathematical definition of the reference Shpilkin analysis.
- Storage policy for large raw artifacts that should not live in Git.

## Definition of done for 2021

- A fresh checkout can build the database from documented inputs.
- National and regional totals reconcile to published CEC totals.
- The UI displays linked turnout, vote-share, and excess-vote views.
- DEG treatment is explicit.
- Every estimate is reproducible from saved parameters.
- Known limitations and data discrepancies are documented.
