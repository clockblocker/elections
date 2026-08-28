import { db } from "./db";
import type { ScatterPoint } from "./types";

type Row = Record<string, unknown>;

export async function elections(): Promise<Record<string, unknown>[]> {
  const rows = await db.unsafe(`
    SELECT e.slug, e.name, e.election_date, b.kind AS ballot_kind, b.name AS ballot_name,
      c.imported_protocols, c.missing_protocols
    FROM elections e
    JOIN ballots b ON b.election_id = e.id
    LEFT JOIN dataset_coverage c ON c.election_id = e.id
    ORDER BY e.election_date DESC
  `) as Row[];
  return rows.map((row) => ({
    slug: row.slug,
    name: row.name,
    electionDate: row.election_date,
    ballot: { kind: row.ballot_kind, name: row.ballot_name },
    importedProtocols: row.imported_protocols == null ? null : Number(row.imported_protocols),
    missingProtocols: row.missing_protocols == null ? null : Number(row.missing_protocols)
  }));
}

export async function metadata(slug: string): Promise<Record<string, unknown> | null> {
  const elections = await db.unsafe(`
    SELECT e.id, e.slug, e.name, e.election_date, e.scope_note,
      c.discovered_regions, c.discovered_tiks, c.discovered_uiks,
      c.imported_protocols, c.missing_protocols, c.deg_policy, c.updated_at,
      b.kind AS ballot_kind, b.name AS ballot_name
    FROM elections e LEFT JOIN dataset_coverage c ON c.election_id = e.id
    JOIN ballots b ON b.election_id = e.id
    WHERE e.slug = $1
  `, [slug]) as Row[];
  if (!elections.length) return null;
  const election = elections[0];
  const options = await db.unsafe(`
    SELECT o.id, o.position, o.name, o.short_name, o.color,
      COALESCE(sum(v.votes), 0)::bigint AS votes
    FROM ballot_options o JOIN ballots b ON b.id = o.ballot_id
    LEFT JOIN votes v ON v.option_id = o.id
    JOIN elections e ON e.id = b.election_id
    WHERE e.slug = $1
    GROUP BY o.id ORDER BY o.position
  `, [slug]) as Row[];
  const regions = await db.unsafe(`
    SELECT r.tvd AS key, r.code, r.name, count(p.id)::integer AS precincts
    FROM regions r LEFT JOIN precincts p ON p.region_id = r.id
    JOIN elections e ON e.id = r.election_id
    WHERE e.slug = $1
    GROUP BY r.id ORDER BY r.code::integer, r.name
  `, [slug]) as Row[];
  const methods = await db.unsafe("SELECT slug, version, name, description, parameters FROM analysis_methods WHERE slug = 'protocol-cloud-clt-v3'") as Row[];
  return {
    election: {
      slug: election.slug,
      name: election.name,
      electionDate: election.election_date,
      scopeNote: election.scope_note
    },
    ballot: { kind: election.ballot_kind, name: election.ballot_name },
    coverage: {
      regions: election.discovered_regions,
      tiks: election.discovered_tiks,
      discoveredUiks: election.discovered_uiks,
      importedProtocols: election.imported_protocols,
      missingProtocols: election.missing_protocols,
      degPolicy: election.deg_policy,
      updatedAt: election.updated_at
    },
    options: options.map((option) => ({
      id: Number(option.id), position: Number(option.position), name: option.name,
      shortName: option.short_name, color: option.color, votes: String(option.votes)
    })),
    regions: regions.map((region) => ({ key: region.key, code: region.code, name: region.name, precincts: Number(region.precincts) })),
    method: methods[0] ? {
      slug: methods[0].slug, version: Number(methods[0].version), name: methods[0].name,
      description: methods[0].description,
      parameters: typeof methods[0].parameters === "string" ? JSON.parse(methods[0].parameters) : methods[0].parameters
    } : null
  };
}

export async function points(slug: string, optionId: number, regions: string[]): Promise<ScatterPoint[]> {
  const values: unknown[] = [slug, optionId];
  let regionCondition = "";
  if (regions.length) {
    const regionParams = regions.map((region) => { values.push(region); return `$${values.length}`; });
    regionCondition = `AND (r.tvd IN (${regionParams.join(",")}) OR r.code IN (${regionParams.join(",")}))`;
  }
  const rows = await db.unsafe(`
    SELECT pr.id, x.uik_number, x.uik_tvd, t.tik_tvd, t.name AS tik_name,
      r.tvd AS region_key, r.code AS region_code, r.name AS region_name,
      a.registered_voters, a.valid_ballots,
      (a.portable_box_ballots + a.stationary_box_ballots) AS ballots_counted,
      v.votes AS option_votes,
      100.0 * (a.portable_box_ballots + a.stationary_box_ballots) / NULLIF(a.registered_voters, 0) AS turnout,
      100.0 * v.votes / NULLIF(a.valid_ballots, 0) AS result
    FROM protocols pr
    JOIN ballots b ON b.id = pr.ballot_id
    JOIN elections e ON e.id = b.election_id
    JOIN precincts x ON x.id = pr.precinct_id
    JOIN regions r ON r.id = x.region_id
    JOIN tiks t ON t.id = x.tik_id
    JOIN ballot_accounting a ON a.protocol_id = pr.id
    JOIN votes v ON v.protocol_id = pr.id AND v.option_id = $2
    WHERE e.slug = $1 AND x.kind = 'physical'
      ${regionCondition}
    ORDER BY pr.id
  `, values) as Row[];
  return rows.map((row) => ({
    id: String(row.id),
    uikNumber: Number(row.uik_number),
    uikTvd: String(row.uik_tvd),
    tikTvd: String(row.tik_tvd),
    tikName: String(row.tik_name),
    regionKey: String(row.region_key),
    regionCode: String(row.region_code),
    regionName: String(row.region_name),
    registeredVoters: Number(row.registered_voters),
    validBallots: Number(row.valid_ballots),
    ballotsCounted: Number(row.ballots_counted),
    optionVotes: Number(row.option_votes),
    turnout: row.turnout == null ? null : Number(row.turnout),
    result: row.result == null ? null : Number(row.result)
  }));
}

export async function protocolDetail(slug: string, protocolId: number): Promise<Record<string, unknown> | null> {
  const rows = await db.unsafe(`
    SELECT pr.id, x.uik_number, x.uik_tvd, t.tik_tvd, t.name AS tik_name,
      r.tvd AS region_key, r.code AS region_code, r.name AS region_name, s.url, s.final_url, s.sha256,
      s.provenance, s.source_report_type, s.derivation, s.retrieved_at,
      a.registered_voters, a.ballots_received, a.ballots_issued_early,
      a.ballots_issued_at_station, a.ballots_issued_outside, a.ballots_cancelled,
      a.portable_box_ballots, a.stationary_box_ballots, a.invalid_ballots,
      a.valid_ballots, a.lost_ballots, a.unaccounted_ballots
    FROM protocols pr JOIN ballots b ON b.id = pr.ballot_id
    JOIN elections e ON e.id = b.election_id
    JOIN precincts x ON x.id = pr.precinct_id
    JOIN tiks t ON t.id = x.tik_id JOIN regions r ON r.id = x.region_id
    JOIN sources s ON s.id = pr.source_id
    JOIN ballot_accounting a ON a.protocol_id = pr.id
    WHERE e.slug = $1 AND pr.id = $2
  `, [slug, protocolId]) as Row[];
  if (!rows.length) return null;
  const row = rows[0];
  const votes = await db.unsafe(`
    SELECT o.position, o.name, o.short_name, o.color, v.votes,
      CASE WHEN a.valid_ballots > 0 THEN 100.0 * v.votes / a.valid_ballots END AS result
    FROM votes v JOIN ballot_options o ON o.id = v.option_id
    JOIN ballot_accounting a ON a.protocol_id = v.protocol_id
    WHERE v.protocol_id = $1 ORDER BY o.position
  `, [protocolId]) as Row[];
  return {
    id: String(row.id),
    precinct: {
      uikNumber: Number(row.uik_number), uikTvd: row.uik_tvd,
      tikTvd: row.tik_tvd, tikName: row.tik_name,
      regionKey: row.region_key, regionCode: row.region_code, regionName: row.region_name,
      kind: "physical"
    },
    accounting: {
      registeredVoters: Number(row.registered_voters), ballotsReceived: Number(row.ballots_received),
      ballotsIssuedEarly: Number(row.ballots_issued_early), ballotsIssuedAtStation: Number(row.ballots_issued_at_station),
      ballotsIssuedOutside: Number(row.ballots_issued_outside), ballotsCancelled: Number(row.ballots_cancelled),
      portableBoxBallots: Number(row.portable_box_ballots), stationaryBoxBallots: Number(row.stationary_box_ballots),
      invalidBallots: Number(row.invalid_ballots), validBallots: Number(row.valid_ballots),
      lostBallots: Number(row.lost_ballots), unaccountedBallots: Number(row.unaccounted_ballots)
    },
    votes: votes.map((vote) => ({
      position: Number(vote.position), name: vote.name, shortName: vote.short_name,
      color: vote.color, votes: Number(vote.votes), result: vote.result == null ? null : Number(vote.result)
    })),
    source: {
      url: row.url, finalUrl: row.final_url, sha256: String(row.sha256).trim(),
      provenance: row.provenance, sourceReportType: Number(row.source_report_type),
      derivation: row.derivation, retrievedAt: row.retrieved_at
    }
  };
}
