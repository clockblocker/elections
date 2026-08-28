import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { existsSync } from "node:fs";
import { db, closeDatabase } from "./db";
import { electionConfig } from "./constants";
import {
  ACCOUNTING_COLUMNS,
  accountingValues,
  ballotOptions,
  flatten,
  placeholders,
  protocolArray
} from "./import-helpers";
import type { BallotCatalog, CoverageDocument, ElectionProtocol } from "./types";

type Row = Record<string, unknown>;
type Executor = { unsafe(query: string, values?: readonly unknown[]): Promise<Row[]> };

const config = electionConfig(Bun.argv[2] ?? "");
const dataRoot = resolve(import.meta.dir, "../../../proper-data", config.slug);
const shardRoot = resolve(dataRoot, "protocol/uik", String(config.reportType));

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

async function loadCatalog(): Promise<BallotCatalog | null> {
  if (!config.catalogFile) return null;
  const module = await import(pathToFileURL(resolve(dataRoot, config.catalogFile)).href) as Record<string, unknown>;
  const catalog = Object.values(module).find((value) => {
    if (!value || typeof value !== "object") return false;
    const candidate = value as BallotCatalog;
    return Array.isArray(candidate.choices) || Array.isArray(candidate.candidates);
  });
  if (!catalog) throw new Error(`No ballot catalog exported by ${config.catalogFile}`);
  return catalog as BallotCatalog;
}

async function bootstrap(): Promise<{ electionId: number; ballotId: number }> {
  const election = await one(db, `
    INSERT INTO elections (slug, name, election_date, scope_note)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, election_date = EXCLUDED.election_date,
      scope_note = EXCLUDED.scope_note
    RETURNING id
  `, [config.slug, config.name, config.electionDate, config.scopeNote]);
  const electionId = Number(election.id);
  const ballot = await one(db, `
    INSERT INTO ballots (election_id, kind, name)
    VALUES ($1, $2, $3)
    ON CONFLICT (election_id, kind) DO UPDATE SET name = EXCLUDED.name
    RETURNING id
  `, [electionId, config.ballotKind, config.ballotName]);
  return { electionId, ballotId: Number(ballot.id) };
}

async function ensureRegionIdentity(
  electionId: number,
  code: string,
  tvd: string,
  name: string,
  regionIds: Map<string, number>
): Promise<number> {
  const cached = regionIds.get(tvd);
  if (cached) return cached;
  const legacy = await db.unsafe(`
    UPDATE regions SET tvd = $3, name = $4
    WHERE election_id = $1 AND code = $2 AND tvd = code
    RETURNING id
  `, [electionId, code, tvd, name]) as unknown as Row[];
  if (legacy.length === 1) {
    const id = Number(legacy[0].id);
    regionIds.set(tvd, id);
    return id;
  }
  const row = await one(db, `
    INSERT INTO regions (election_id, code, tvd, name)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (election_id, tvd) DO UPDATE SET code = EXCLUDED.code, name = EXCLUDED.name
    RETURNING id
  `, [electionId, code, tvd, name]);
  const id = Number(row.id);
  regionIds.set(tvd, id);
  return id;
}

async function ensureRegion(
  electionId: number,
  record: ElectionProtocol,
  regionIds: Map<string, number>
): Promise<number> {
  return ensureRegionIdentity(
    electionId,
    record.regionCode,
    record.regionTvd ?? record.regionCode,
    record.regionName,
    regionIds
  );
}

async function ensureOptions(
  ballotId: number,
  protocol: ElectionProtocol,
  catalog: BallotCatalog | null
): Promise<Map<string, number>> {
  const options = ballotOptions(protocol, config, catalog);
  await bulk(db, "ballot_options", ["ballot_id", "position", "name", "short_name", "color"],
    options.map((option) => [ballotId, option.position, option.name, option.shortName, option.color]),
    "ON CONFLICT (ballot_id, position) DO UPDATE SET name = EXCLUDED.name, short_name = EXCLUDED.short_name, color = EXCLUDED.color");
  const rows = await db.unsafe("SELECT id, position FROM ballot_options WHERE ballot_id = $1", [ballotId]) as unknown as Row[];
  const byPosition = new Map<number, number>(rows.map((row) => [Number(row.position), Number(row.id)]));
  return new Map(options.map((option) => [option.key, byPosition.get(option.position)!]));
}

async function importShard(
  records: ElectionProtocol[],
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

    const sourceRows = await executor.unsafe(
      `SELECT id, sha256 FROM sources WHERE sha256 IN (${placeholders(sources.size, 1).replace(/[()]/g, "")})`,
      [...sources.keys()]
    );
    const sourceIds = new Map(sourceRows.map((row) => [String(row.sha256).trim(), String(row.id)]));
    const tikRows = await executor.unsafe(
      "SELECT id, tik_tvd FROM tiks WHERE election_id = $1 AND region_id = $2",
      [electionId, regionId]
    );
    const tikIds = new Map(tikRows.map((row) => [String(row.tik_tvd), String(row.id)]));

    await bulk(executor, "precincts", ["election_id", "region_id", "tik_id", "uik_number", "uik_tvd", "kind"],
      records.map((record) => [electionId, regionId, tikIds.get(record.tikTvd), record.uikNumber, record.uikTvd, "physical"]),
      "ON CONFLICT (election_id, uik_tvd) DO UPDATE SET region_id = EXCLUDED.region_id, tik_id = EXCLUDED.tik_id, uik_number = EXCLUDED.uik_number");
    const precinctRows = await executor.unsafe(
      "SELECT id, uik_tvd FROM precincts WHERE election_id = $1 AND region_id = $2",
      [electionId, regionId]
    );
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

    const voteRows = records.flatMap((record) => Object.entries(record.votes).map(([key, votes]) => {
      const optionId = optionIds.get(key);
      if (!optionId) throw new Error(`Unknown option in ${config.slug}, region ${record.regionCode}: ${key}`);
      return [protocolIds.get(record.uikTvd), optionId, votes];
    }));
    await bulk(executor, "votes", ["protocol_id", "option_id", "votes"], voteRows,
      "ON CONFLICT (protocol_id, option_id) DO UPDATE SET votes = EXCLUDED.votes");
  });
}

const coverage = await Bun.file(resolve(dataRoot, "coverage.json")).json() as CoverageDocument;
if (coverage.election !== config.slug) throw new Error(`Coverage file is not for ${config.slug}`);

const { electionId, ballotId } = await bootstrap();
const catalog = await loadCatalog();
const glob = new Bun.Glob("region-*-part-*.ts");
const files = Array.from(glob.scanSync({ cwd: shardRoot }))
  .sort((left, right) => left.localeCompare(right, "en", { numeric: true }));
if (!files.length) throw new Error(`No ${config.slug} ${config.ballotName} shards found below ${shardRoot}`);

const regionRows = await db.unsafe("SELECT id, tvd FROM regions WHERE election_id = $1", [electionId]) as unknown as Row[];
const regionIds = new Map<string, number>(regionRows.map((row) => [String(row.tvd), Number(row.id)]));
for (const region of coverage.regions) {
  if (region.regionCode && region.regionTvd && region.regionName) {
    await ensureRegionIdentity(electionId, region.regionCode, region.regionTvd, region.regionName, regionIds);
  }
}
let optionIds: Map<string, number> | null = null;
let imported = 0;
for (const [index, file] of files.entries()) {
  const module = await import(pathToFileURL(resolve(shardRoot, file)).href) as Record<string, unknown>;
  const records = protocolArray(module, file, config);
  optionIds ??= await ensureOptions(ballotId, records[0], catalog);
  const regionGroups = new Map<string, ElectionProtocol[]>();
  for (const record of records) {
    const identity = record.regionTvd ?? record.regionCode;
    const group = regionGroups.get(identity) ?? [];
    group.push(record);
    regionGroups.set(identity, group);
  }
  for (const group of regionGroups.values()) {
    const regionId = await ensureRegion(electionId, group[0], regionIds);
    await importShard(group, electionId, ballotId, regionId, optionIds);
  }
  imported += records.length;
  if ((index + 1) % 20 === 0 || index + 1 === files.length) {
    console.log(`${config.slug}: imported ${imported.toLocaleString()} protocols from ${index + 1}/${files.length} shards`);
  }
}

const totals = coverage.regions.reduce((result, region) => ({
  tiks: result.tiks + region.discovered_tik_count,
  uiks: result.uiks + region.discovered_uik_count,
  expected: result.expected + (region[config.coverageResultKey] ?? 0)
}), { tiks: 0, uiks: 0, expected: 0 });
const missing = Math.max(0, totals.uiks - totals.expected);
const hasDeg = existsSync(resolve(dataRoot, "DEG"));

await db.unsafe(`
  INSERT INTO dataset_coverage (election_id, discovered_regions, discovered_tiks, discovered_uiks,
    imported_protocols, missing_protocols, deg_policy, source_schema_version, source_generated_at)
  VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
  ON CONFLICT (election_id) DO UPDATE SET discovered_regions = EXCLUDED.discovered_regions,
    discovered_tiks = EXCLUDED.discovered_tiks, discovered_uiks = EXCLUDED.discovered_uiks,
    imported_protocols = EXCLUDED.imported_protocols, missing_protocols = EXCLUDED.missing_protocols,
    deg_policy = EXCLUDED.deg_policy, source_schema_version = EXCLUDED.source_schema_version,
    source_generated_at = EXCLUDED.source_generated_at, updated_at = now()
`, [electionId, coverage.regions.length, totals.tiks, totals.uiks, imported, missing,
  hasDeg ? "excluded-outside-protocol-cloud-model" : "not-applicable-no-deg-dataset",
  coverage.schema_version, coverage.generated_at ?? null]);

if (imported !== totals.expected) {
  throw new Error(`Imported ${imported} protocols, coverage declares ${totals.expected} for ${config.slug}`);
}
console.log(`${config.slug} import complete: ${imported.toLocaleString()} physical UIK protocols`);
await closeDatabase();
