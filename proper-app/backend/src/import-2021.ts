import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { db, closeDatabase } from "./db";
import { ELECTION_SLUG } from "./constants";
import { REGION_NAMES, regionName } from "./regions";
import { ACCOUNTING_COLUMNS, accountingValues, flatten, parseOption, placeholders, protocolArray } from "./import-helpers";
import type { CoverageDocument, PartyProtocol } from "./types";

type Row = Record<string, unknown>;
type Executor = { unsafe(query: string, values?: readonly unknown[]): Promise<Row[]> };

const dataRoot = resolve(import.meta.dir, "../../../proper-data/2021-duma");
const shardRoot = resolve(dataRoot, "protocol/uik/242");

async function one(executor: Executor, query: string, values: readonly unknown[] = []): Promise<Row> {
  const rows = await executor.unsafe(query, values);
  if (rows.length !== 1) throw new Error(`Expected one row, received ${rows.length}`);
  return rows[0];
}

async function bulk(
  executor: Executor,
  table: string,
  columns: readonly string[],
  rows: readonly (readonly unknown[])[],
  conflict: string
): Promise<void> {
  if (!rows.length) return;
  const sql = `INSERT INTO ${table} (${columns.join(",")}) VALUES ${placeholders(rows.length, columns.length)} ${conflict}`;
  await executor.unsafe(sql, flatten(rows));
}

async function bootstrap(): Promise<{ electionId: number; ballotId: number; regionIds: Map<string, number> }> {
  const election = await one(db, `
    INSERT INTO elections (slug, name, election_date, scope_note)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, election_date = EXCLUDED.election_date,
      scope_note = EXCLUDED.scope_note
    RETURNING id
  `, [ELECTION_SLUG, "State Duma election, eighth convocation", "2021-09-19",
    "Federal party-list results at physical UIKs. DEG is deliberately outside this analytical dataset."]);
  const electionId = Number(election.id);

  await bulk(db, "regions", ["election_id", "code", "name"],
    Object.entries(REGION_NAMES).map(([code, name]) => [electionId, code, name]),
    "ON CONFLICT (election_id, code) DO UPDATE SET name = EXCLUDED.name");

  const regionRows = await db.unsafe("SELECT id, code FROM regions WHERE election_id = $1", [electionId]) as unknown as Row[];
  const regionIds = new Map<string, number>(regionRows.map((row) => [String(row.code), Number(row.id)]));

  const ballot = await one(db, `
    INSERT INTO ballots (election_id, kind, name)
    VALUES ($1, 'party-list', 'Federal party list')
    ON CONFLICT (election_id, kind) DO UPDATE SET name = EXCLUDED.name
    RETURNING id
  `, [electionId]);

  await db.unsafe(`
    INSERT INTO analysis_methods (slug, version, name, description, parameters)
    VALUES ('peer-clt-v2', 2, 'Peer-conditioned CLT residuals', $1, $2::jsonb)
    ON CONFLICT (slug) DO UPDATE SET version = EXCLUDED.version, name = EXCLUDED.name,
      description = EXCLUDED.description, parameters = EXCLUDED.parameters
  `, [
    "Leave-one-out TIK/region peer expectations with hierarchical shrinkage, robust overdispersion, empirical P_sus calibration, and Benjamini-Yekutieli review q-values.",
    JSON.stringify({ turnoutBinWidth: 2.5, turnoutWindow: 10, minTikPeers: 8,
      minRegionPeers: 30, tikPriorBallots: 2000, regionPriorBallots: 10000,
      dispersionPriorPoints: 30, fdrThreshold: 0.05, degIncluded: false })
  ]);
  return { electionId, ballotId: Number(ballot.id), regionIds };
}

async function ensureOptions(ballotId: number, protocol: PartyProtocol): Promise<Map<string, number>> {
  const options = Object.keys(protocol.votes).map((label) => ({ label, ...parseOption(label) }));
  await bulk(db, "ballot_options", ["ballot_id", "position", "name", "short_name", "color"],
    options.map((option) => [ballotId, option.position, option.name, option.shortName, option.color]),
    "ON CONFLICT (ballot_id, position) DO UPDATE SET name = EXCLUDED.name, short_name = EXCLUDED.short_name, color = EXCLUDED.color");
  const rows = await db.unsafe("SELECT id, position FROM ballot_options WHERE ballot_id = $1", [ballotId]) as unknown as Row[];
  const byPosition = new Map<number, number>(rows.map((row) => [Number(row.position), Number(row.id)]));
  return new Map<string, number>(options.map((option) => [option.label, byPosition.get(option.position)!]));
}

async function importShard(
  records: PartyProtocol[],
  regionCode: string,
  electionId: number,
  ballotId: number,
  regionId: number,
  optionIds: Map<string, number>
): Promise<void> {
  await db.begin(async (transaction) => {
    const executor = transaction as unknown as Executor;
    const sources = new Map(records.map((record) => [record.source.sha256, record.source]));
    await bulk(executor, "sources",
      ["sha256", "url", "final_url", "retrieved_at", "provenance", "source_report_type", "derivation"],
      [...sources.values()].map((source) => [source.sha256, source.url, source.finalUrl ?? null,
        source.retrievedAt ?? null, source.provenance ?? "live-official", source.sourceReportType,
        source.derivation ?? null]),
      "ON CONFLICT (sha256) DO UPDATE SET url = EXCLUDED.url, final_url = EXCLUDED.final_url, retrieved_at = EXCLUDED.retrieved_at, provenance = EXCLUDED.provenance, source_report_type = EXCLUDED.source_report_type, derivation = EXCLUDED.derivation");

    const tiks = new Map(records.map((record) => [record.tikTvd, record.tikName]));
    await bulk(executor, "tiks", ["election_id", "region_id", "tik_tvd", "name"],
      [...tiks].map(([tvd, name]) => [electionId, regionId, tvd, name]),
      "ON CONFLICT (election_id, tik_tvd) DO UPDATE SET region_id = EXCLUDED.region_id, name = EXCLUDED.name");

    const sourceRows = await executor.unsafe(`SELECT id, sha256 FROM sources WHERE sha256 IN (${placeholders(sources.size, 1).replace(/[()]/g, "")})`, [...sources.keys()]);
    const sourceIds = new Map(sourceRows.map((row) => [String(row.sha256).trim(), String(row.id)]));
    const tikRows = await executor.unsafe("SELECT id, tik_tvd FROM tiks WHERE election_id = $1 AND region_id = $2", [electionId, regionId]);
    const tikIds = new Map(tikRows.map((row) => [String(row.tik_tvd), String(row.id)]));

    await bulk(executor, "precincts", ["election_id", "region_id", "tik_id", "uik_number", "uik_tvd", "kind"],
      records.map((record) => [electionId, regionId, tikIds.get(record.tikTvd), record.uikNumber, record.uikTvd, "physical"]),
      "ON CONFLICT (election_id, uik_tvd) DO UPDATE SET region_id = EXCLUDED.region_id, tik_id = EXCLUDED.tik_id, uik_number = EXCLUDED.uik_number");
    const precinctRows = await executor.unsafe("SELECT id, uik_tvd FROM precincts WHERE election_id = $1 AND region_id = $2", [electionId, regionId]);
    const precinctIds = new Map(precinctRows.map((row) => [String(row.uik_tvd), String(row.id)]));

    await bulk(executor, "protocols", ["ballot_id", "precinct_id", "source_id", "report_type"],
      records.map((record) => [ballotId, precinctIds.get(record.uikTvd), sourceIds.get(record.source.sha256), record.reportType]),
      "ON CONFLICT (ballot_id, precinct_id) DO UPDATE SET source_id = EXCLUDED.source_id, report_type = EXCLUDED.report_type, imported_at = now()");
    const protocolRows = await executor.unsafe(`
      SELECT p.id, x.uik_tvd FROM protocols p
      JOIN precincts x ON x.id = p.precinct_id
      WHERE p.ballot_id = $1 AND x.region_id = $2
    `, [ballotId, regionId]);
    const protocolIds = new Map(protocolRows.map((row) => [String(row.uik_tvd), String(row.id)]));

    await bulk(executor, "ballot_accounting", ["protocol_id", ...ACCOUNTING_COLUMNS],
      records.map((record) => [protocolIds.get(record.uikTvd), ...accountingValues(record)]),
      `ON CONFLICT (protocol_id) DO UPDATE SET ${ACCOUNTING_COLUMNS.map((column) => `${column} = EXCLUDED.${column}`).join(", ")}`);

    const voteRows = records.flatMap((record) => Object.entries(record.votes).map(([label, votes]) => {
      const optionId = optionIds.get(label);
      if (!optionId) throw new Error(`Unknown option in region ${regionCode}: ${label}`);
      return [protocolIds.get(record.uikTvd), optionId, votes];
    }));
    await bulk(executor, "votes", ["protocol_id", "option_id", "votes"], voteRows,
      "ON CONFLICT (protocol_id, option_id) DO UPDATE SET votes = EXCLUDED.votes");
  });
}

const coverage = await Bun.file(resolve(dataRoot, "coverage.json")).json() as CoverageDocument;
if (coverage.election !== "2021-duma") throw new Error("Coverage file is not for the 2021 Duma election");

const { electionId, ballotId, regionIds } = await bootstrap();
const glob = new Bun.Glob("region-*-part-*.ts");
const files = Array.from(glob.scanSync({ cwd: shardRoot })).sort((left, right) => left.localeCompare(right, "en", { numeric: true }));
if (!files.length) throw new Error(`No 2021 party-list shards found below ${shardRoot}`);

let optionIds: Map<string, number> | null = null;
let imported = 0;
for (const [index, file] of files.entries()) {
  const match = /^region-(\d+)-part-\d+\.ts$/.exec(file);
  if (!match) throw new Error(`Cannot derive region from ${file}`);
  const code = match[1];
  regionName(code);
  const regionId = regionIds.get(code);
  if (!regionId) throw new Error(`Region ${code} was not bootstrapped`);
  const module = await import(pathToFileURL(resolve(shardRoot, file)).href);
  const records = protocolArray(module, file);
  optionIds ??= await ensureOptions(ballotId, records[0]);
  await importShard(records, code, electionId, ballotId, regionId, optionIds);
  imported += records.length;
  if ((index + 1) % 20 === 0 || index + 1 === files.length) {
    console.log(`Imported ${imported.toLocaleString()} protocols from ${index + 1}/${files.length} shards`);
  }
}

const totals = coverage.regions.reduce((result, region) => ({
  tiks: result.tiks + region.discovered_tik_count,
  uiks: result.uiks + region.discovered_uik_count,
  party: result.party + region.uiks_with_party_results,
  missing: result.missing + region.missing_uiks.length
}), { tiks: 0, uiks: 0, party: 0, missing: 0 });

await db.unsafe(`
  INSERT INTO dataset_coverage (election_id, discovered_regions, discovered_tiks, discovered_uiks,
    imported_party_protocols, missing_party_protocols, deg_policy, source_schema_version)
  VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
  ON CONFLICT (election_id) DO UPDATE SET discovered_regions = EXCLUDED.discovered_regions,
    discovered_tiks = EXCLUDED.discovered_tiks, discovered_uiks = EXCLUDED.discovered_uiks,
    imported_party_protocols = EXCLUDED.imported_party_protocols,
    missing_party_protocols = EXCLUDED.missing_party_protocols, deg_policy = EXCLUDED.deg_policy,
    source_schema_version = EXCLUDED.source_schema_version, updated_at = now()
`, [electionId, coverage.regions.length, totals.tiks, totals.uiks, imported, totals.missing,
  "excluded-outside-peer-clt-model", coverage.schema_version]);

if (imported !== totals.party) throw new Error(`Imported ${imported} protocols, coverage declares ${totals.party}`);
console.log(`2021 party-list import complete: ${imported.toLocaleString()} physical UIK protocols`);
await closeDatabase();
