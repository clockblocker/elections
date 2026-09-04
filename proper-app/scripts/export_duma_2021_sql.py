#!/usr/bin/env python3
"""Export the 2021 State Duma proper-data shards as one self-contained PostgreSQL file.

Reads ../../proper-data/2021-duma/** (generated TypeScript shards), normalizes them
into the schema below, and writes a psql-loadable file that uses COPY ... FROM stdin.

By default the generated dump is written under reports/generated/, which is
excluded from Git. Load it with:
  psql -d <db> -v ON_ERROR_STOP=1 -f reports/generated/duma-2021.sql
Everything lives in schema `duma_2021`, so it does not collide with the app tables.

Normalization decisions (vs. the shards):
- Geography is stored once: regions -> tiks -> uiks (from uik-to-tik/), plus
  districts (OIK) and the uik -> district assignment. UIK protocols therefore do
  NOT repeat tik/region/district; join through uiks.
- UIKs without a protocol are kept (16 lack a party-list protocol, 14 lack a
  single-member one) so coverage gaps are queryable rather than silent.
- `protocols` holds both commission levels (uik / tik) and both contests
  (party_list / single_member) in one table with the 12 official accounting
  lines as columns. Votes live in party_votes / candidate_votes keyed by the
  official party position / candidate vibid.
- Every protocol and district points at a `sources` row (sha256 of the raw
  official response kept under data/raw/gas-duma-2021/). One TIK column report
  is the source of all its UIK rows; that is what `derivation` records.
- TIK `uikTvds` lists are not stored: they equal the set of UIKs of that TIK that
  have a protocol for the contest (verified on export); `uik_count` is kept so the
  equality can be re-checked in SQL.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "proper-data" / "2021-duma"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "generated" / "duma-2021.sql"

ACCOUNTING = [
    ("registered_voters", "Число избирателей, внесенных в список избирателей на момент окончания голосования"),
    ("ballots_received", "Число избирательных бюллетеней, полученных участковой избирательной комиссией"),
    ("ballots_issued_early", "Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно"),
    ("ballots_issued_at_station", "Число избирательных бюллетеней, выданных в помещении для голосования в день голосования"),
    ("ballots_issued_outside", "Число избирательных бюллетеней, выданных вне помещения для голосования в день голосования"),
    ("ballots_cancelled", "Число погашенных избирательных бюллетеней"),
    ("portable_box_ballots", "Число избирательных бюллетеней, содержащихся в переносных ящиках для голосования"),
    ("stationary_box_ballots", "Число избирательных бюллетеней, содержащихся в стационарных ящиках для голосования"),
    ("invalid_ballots", "Число недействительных избирательных бюллетеней"),
    ("valid_ballots", "Число действительных избирательных бюллетеней"),
    ("lost_ballots", "Число утраченных избирательных бюллетеней"),
    ("unaccounted_ballots", "Число избирательных бюллетеней, не учтенных при получении"),
]
ACCOUNTING_COLS = [c for c, _ in ACCOUNTING]


# ---------------------------------------------------------------- shard loading
def load_shard(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    start = text.index("= [") + 2
    end = text.rindex("] satisfies") + 1
    return json.loads(text[start:end])


def load_dir(sub: str) -> list:
    rows: list = []
    for name in sorted(os.listdir(DATA / sub)):
        if name.endswith(".ts"):
            rows.extend(load_shard(DATA / sub / name))
    return rows


# ---------------------------------------------------------------- COPY encoding
def enc(v) -> str:
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def copy(out, table: str, cols: list[str], rows) -> int:
    out.write(f"COPY {table} ({', '.join(cols)}) FROM stdin;\n")
    n = 0
    for row in rows:
        out.write("\t".join(enc(v) for v in row))
        out.write("\n")
        n += 1
    out.write("\\.\n\n")
    return n


def region_sort_key(code: str):
    return (int(code) if code.isdigit() else 10**6, code)


# ---------------------------------------------------------------- main
def main() -> None:
    rel = load_dir("uik-to-tik")
    districts = load_dir("districts")
    uik_party = load_dir("protocol/uik/242")
    uik_smd = load_dir("protocol/uik/463")
    tik_party = load_dir("protocol/tic/233")
    tik_smd = load_dir("protocol/tic/464")

    # geography ---------------------------------------------------------------
    regions = {}
    tiks = {}
    uiks = {}
    for r in rel:
        regions.setdefault(r["regionCode"], (r["regionCode"], r["regionTvd"], r["regionName"]))
        tiks.setdefault(r["tikTvd"], (r["tikTvd"], r["regionCode"], r["tikName"]))
        assert r["uikTvd"] not in uiks
        uiks[r["uikTvd"]] = (r["uikTvd"], r["uikNumber"], r["regionCode"], r["tikTvd"], r["district"]["districtNumber"])

    # sources -----------------------------------------------------------------
    sources: dict[str, dict] = {}

    def add_source(s: dict, extra: dict | None = None) -> str:
        sha = s["sha256"]
        row = {
            "sha256": sha,
            "report_type": s["sourceReportType"] if "sourceReportType" in s else None,
            "url": s["url"],
            "final_url": s.get("finalUrl"),
            "provenance": s.get("provenance"),
            "retrieved_at": s.get("retrievedAt"),
            "cec_resolution": None,
            "cec_resolution_date": None,
        }
        if extra:
            row.update(extra)
        prev = sources.setdefault(sha, row)
        assert prev == row, f"conflicting source metadata for {sha}"
        return sha

    # districts + candidates --------------------------------------------------
    district_rows = []
    candidate_rows = []
    for d in sorted(districts, key=lambda d: d["districtNumber"]):
        src = add_source(d["source"])
        ws = d["source"]["winnerSource"]
        wsrc = add_source(ws, {"cec_resolution": ws["resolution"], "cec_resolution_date": ws["resolutionDate"]})
        district_rows.append((d["districtNumber"], d["oikTvd"], d["oikName"], d["regionCode"], d["winnerCandidateVibid"], src, wsrc))
        for c in d["candidates"]:
            candidate_rows.append((c["candidateVibid"], d["districtNumber"], c["fullName"], c["nominatingEntity"], c["registrationStatus"] or None, c["isElected"]))

    # parties -----------------------------------------------------------------
    party_keys = list(uik_party[0]["votes"].keys())
    parties = []
    party_pos = {}
    for key in party_keys:
        pos, name = key.split(". ", 1)
        parties.append((int(pos), name))
        party_pos[key] = int(pos)
    for p in uik_party + tik_party:
        assert list(p["votes"].keys()) == party_keys, p.get("uikTvd") or p.get("tikTvd")

    # protocols ---------------------------------------------------------------
    protocol_rows = []
    party_vote_rows = []
    cand_vote_rows = []
    next_id = 0

    def accounting(p) -> list[int]:
        a = p["accounting"]
        assert list(a.keys()) == [label for _, label in ACCOUNTING]
        return [a[label] for _, label in ACCOUNTING]

    def add_protocol(p, level: str, contest: str):
        nonlocal next_id
        next_id += 1
        src = add_source(p["source"])
        uik_tvd = p["uikTvd"] if level == "uik" else None
        tik_tvd = p["tikTvd"] if level == "tik" else None
        if level == "uik":
            u = uiks[uik_tvd]
            assert u[3] == p["tikTvd"] and u[2] == p["regionCode"] and u[1] == p["uikNumber"]
            if contest == "single_member":
                assert u[4] == p["district"]["districtNumber"]
        protocol_rows.append(
            (next_id, level, contest, uik_tvd, tik_tvd, p["reportType"], src, p["source"].get("derivation"), p.get("uikCount"))
            + tuple(accounting(p))
        )
        if contest == "party_list":
            for key, v in p["votes"].items():
                party_vote_rows.append((next_id, party_pos[key], v))
        else:
            for vibid, v in p["votes"].items():
                cand_vote_rows.append((next_id, vibid, v))

    def uik_order(p):
        return (region_sort_key(p["regionCode"]), p["tikTvd"], p["uikNumber"], p["uikTvd"])

    def tik_order(p):
        return (region_sort_key(p["regionCode"]), p["tikTvd"])

    for p in sorted(uik_party, key=uik_order):
        add_protocol(p, "uik", "party_list")
    for p in sorted(uik_smd, key=uik_order):
        add_protocol(p, "uik", "single_member")
    for p in sorted(tik_party, key=tik_order):
        add_protocol(p, "tik", "party_list")
    for p in sorted(tik_smd, key=tik_order):
        add_protocol(p, "tik", "single_member")

    # verify that TIK uikTvds == UIKs of the TIK having that contest's protocol
    have = {"party_list": {p["uikTvd"] for p in uik_party}, "single_member": {p["uikTvd"] for p in uik_smd}}
    by_tik = defaultdict(set)
    for tvd, (_, _, _, tik_tvd, _) in uiks.items():
        by_tik[tik_tvd].add(tvd)
    for rows, contest in ((tik_party, "party_list"), (tik_smd, "single_member")):
        for t in rows:
            expected = by_tik[t["tikTvd"]] & have[contest]
            assert set(t["uikTvds"]) == expected, t["tikTvd"]
            assert t["uikCount"] == len(expected)

    # write -------------------------------------------------------------------
    OUT.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with OUT.open("w", encoding="utf-8") as out:
        out.write(SCHEMA_HEADER.format(generated_at=generated_at))
        n = {}
        n["regions"] = copy(out, "regions", ["code", "tvd", "name"], sorted(regions.values(), key=lambda r: region_sort_key(r[0])))
        n["sources"] = copy(
            out,
            "sources",
            ["sha256", "report_type", "url", "final_url", "provenance", "retrieved_at", "cec_resolution", "cec_resolution_date"],
            (tuple(s.values()) for _, s in sorted(sources.items())),
        )
        n["districts"] = copy(
            out, "districts", ["number", "oik_tvd", "name", "region_code", "winner_candidate_vibid", "source_sha256", "winner_source_sha256"], district_rows
        )
        n["candidates"] = copy(
            out, "candidates", ["vibid", "district_number", "full_name", "nominating_entity", "registration_status", "is_elected"], candidate_rows
        )
        n["tiks"] = copy(out, "tiks", ["tvd", "region_code", "name"], sorted(tiks.values(), key=lambda t: (region_sort_key(t[1]), t[0])))
        n["uiks"] = copy(
            out,
            "uiks",
            ["tvd", "number", "region_code", "tik_tvd", "district_number"],
            sorted(uiks.values(), key=lambda u: (region_sort_key(u[2]), u[3], u[1], u[0])),
        )
        n["parties"] = copy(out, "parties", ["position", "name"], parties)
        n["protocols"] = copy(
            out,
            "protocols",
            ["id", "level", "contest", "uik_tvd", "tik_tvd", "report_type", "source_sha256", "derivation", "uik_count"] + ACCOUNTING_COLS,
            protocol_rows,
        )
        n["party_votes"] = copy(out, "party_votes", ["protocol_id", "party_position", "votes"], party_vote_rows)
        n["candidate_votes"] = copy(out, "candidate_votes", ["protocol_id", "candidate_vibid", "votes"], cand_vote_rows)
        out.write(SCHEMA_FOOTER)
    for k, v in n.items():
        print(f"{k:16s} {v:>10,}")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


SCHEMA_HEADER = """\
-- 2021 State Duma election (19 September 2021), nationwide physical UIK and TIK protocols.
-- Generated by proper-app/scripts/export_duma_2021_sql.py at {generated_at}
-- from proper-data/2021-duma/. Load with: psql -v ON_ERROR_STOP=1 -f duma-2021.sql
--
-- Scope: party-list (federal) contest and single-member district contest, physical
-- precincts only. Remote electronic voting (DEG) is NOT included.

BEGIN;

DROP SCHEMA IF EXISTS duma_2021 CASCADE;
CREATE SCHEMA duma_2021;
SET search_path = duma_2021;

COMMENT ON SCHEMA duma_2021 IS 'State Duma election 2021-09-19: physical UIK/TIK protocols, party-list and single-member contests, no DEG.';

-- ---------------------------------------------------------------- geography
CREATE TABLE regions (
  code text PRIMARY KEY,           -- GAS region code ("1".."99")
  tvd  text NOT NULL UNIQUE,       -- GAS commission identifier of the regional commission
  name text NOT NULL
);

CREATE TABLE tiks (
  tvd         text PRIMARY KEY,    -- GAS identifier of the territorial commission
  region_code text NOT NULL REFERENCES regions(code),
  name        text NOT NULL
);

-- ---------------------------------------------------------------- provenance
CREATE TABLE sources (
  sha256              char(64) PRIMARY KEY,  -- checksum of the raw official response (data/raw/gas-duma-2021/)
  report_type         smallint,              -- GAS report type: 233/464 TIK column reports, 463 direct UIK, 220 candidate registry; NULL for CEC resolution
  url                 text NOT NULL,
  final_url           text,
  provenance          text NOT NULL CHECK (provenance IN ('live-official', 'wayback')),
  retrieved_at        timestamptz,
  cec_resolution      text,                  -- only for the CEC resolution naming the elected SMD deputies
  cec_resolution_date date
);

-- ---------------------------------------------------------------- single-member districts
CREATE TABLE districts (
  number                 smallint PRIMARY KEY,           -- OIK number 1..225
  oik_tvd                text NOT NULL UNIQUE,
  name                   text NOT NULL,
  region_code            text NOT NULL REFERENCES regions(code),
  winner_candidate_vibid text NOT NULL,                   -- FK added after candidates
  source_sha256          char(64) NOT NULL REFERENCES sources(sha256),  -- candidate registry page (type 220)
  winner_source_sha256   char(64) NOT NULL REFERENCES sources(sha256)   -- CEC resolution 61/467-8
);

CREATE TABLE candidates (
  vibid               text PRIMARY KEY,                   -- GAS candidate identifier
  district_number     smallint NOT NULL REFERENCES districts(number),
  full_name           text NOT NULL,
  nominating_entity   text NOT NULL,
  registration_status text,                               -- official wording; NULL where the registry left it blank
  is_elected          boolean NOT NULL
);

ALTER TABLE districts
  ADD CONSTRAINT districts_winner_fk FOREIGN KEY (winner_candidate_vibid) REFERENCES candidates(vibid) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE uiks (
  tvd             text PRIMARY KEY,                       -- GAS identifier of the precinct commission
  number          integer NOT NULL,                       -- UIK number, unique within a region
  region_code     text NOT NULL REFERENCES regions(code),
  tik_tvd         text NOT NULL REFERENCES tiks(tvd),
  district_number smallint NOT NULL REFERENCES districts(number),
  UNIQUE (region_code, number)
);

-- ---------------------------------------------------------------- party list
CREATE TABLE parties (
  position smallint PRIMARY KEY,   -- position on the federal ballot
  name     text NOT NULL UNIQUE    -- official name as printed in the protocol
);

-- ---------------------------------------------------------------- protocols
CREATE TABLE protocols (
  id            integer PRIMARY KEY,
  level         text NOT NULL CHECK (level IN ('uik', 'tik')),
  contest       text NOT NULL CHECK (contest IN ('party_list', 'single_member')),
  uik_tvd       text REFERENCES uiks(tvd),               -- set iff level = 'uik'
  tik_tvd       text REFERENCES tiks(tvd),               -- set iff level = 'tik'
  report_type   smallint NOT NULL,                       -- 242/463 UIK, 233/464 TIK
  source_sha256 char(64) NOT NULL REFERENCES sources(sha256),
  derivation    text NOT NULL CHECK (derivation IN ('direct', 'extracted-tic-column')),
  uik_count     smallint,                                -- TIK level only: number of UIK columns in the source report
  -- the 12 official protocol lines, in official order
  registered_voters         integer NOT NULL CHECK (registered_voters >= 0),
  ballots_received          integer NOT NULL CHECK (ballots_received >= 0),
  ballots_issued_early      integer NOT NULL CHECK (ballots_issued_early >= 0),
  ballots_issued_at_station integer NOT NULL CHECK (ballots_issued_at_station >= 0),
  ballots_issued_outside    integer NOT NULL CHECK (ballots_issued_outside >= 0),
  ballots_cancelled         integer NOT NULL CHECK (ballots_cancelled >= 0),
  portable_box_ballots      integer NOT NULL CHECK (portable_box_ballots >= 0),
  stationary_box_ballots    integer NOT NULL CHECK (stationary_box_ballots >= 0),
  invalid_ballots           integer NOT NULL CHECK (invalid_ballots >= 0),
  valid_ballots             integer NOT NULL CHECK (valid_ballots >= 0),
  lost_ballots              integer NOT NULL CHECK (lost_ballots >= 0),
  unaccounted_ballots       integer NOT NULL CHECK (unaccounted_ballots >= 0),
  CHECK ((level = 'uik') = (uik_tvd IS NOT NULL)),
  CHECK ((level = 'tik') = (tik_tvd IS NOT NULL)),
  CHECK ((level = 'tik') = (uik_count IS NOT NULL)),
  UNIQUE NULLS NOT DISTINCT (contest, level, uik_tvd, tik_tvd)
);

COMMENT ON COLUMN protocols.registered_voters         IS 'Число избирателей, внесенных в список избирателей на момент окончания голосования';
COMMENT ON COLUMN protocols.ballots_received          IS 'Число избирательных бюллетеней, полученных участковой избирательной комиссией';
COMMENT ON COLUMN protocols.ballots_issued_early      IS 'Число избирательных бюллетеней, выданных избирателям, проголосовавшим досрочно';
COMMENT ON COLUMN protocols.ballots_issued_at_station IS 'Число избирательных бюллетеней, выданных в помещении для голосования в день голосования';
COMMENT ON COLUMN protocols.ballots_issued_outside    IS 'Число избирательных бюллетеней, выданных вне помещения для голосования в день голосования';
COMMENT ON COLUMN protocols.ballots_cancelled         IS 'Число погашенных избирательных бюллетеней';
COMMENT ON COLUMN protocols.portable_box_ballots      IS 'Число избирательных бюллетеней, содержащихся в переносных ящиках для голосования';
COMMENT ON COLUMN protocols.stationary_box_ballots    IS 'Число избирательных бюллетеней, содержащихся в стационарных ящиках для голосования';
COMMENT ON COLUMN protocols.invalid_ballots           IS 'Число недействительных избирательных бюллетеней';
COMMENT ON COLUMN protocols.valid_ballots             IS 'Число действительных избирательных бюллетеней';
COMMENT ON COLUMN protocols.lost_ballots              IS 'Число утраченных избирательных бюллетеней';
COMMENT ON COLUMN protocols.unaccounted_ballots       IS 'Число избирательных бюллетеней, не учтенных при получении';
COMMENT ON COLUMN protocols.derivation IS 'direct = parsed from the commission''s own protocol page; extracted-tic-column = transposed from the TIK report whose columns are UIKs';

CREATE TABLE party_votes (
  protocol_id    integer  NOT NULL REFERENCES protocols(id) ON DELETE CASCADE,
  party_position smallint NOT NULL REFERENCES parties(position),
  votes          integer  NOT NULL CHECK (votes >= 0),
  PRIMARY KEY (protocol_id, party_position)
);

CREATE TABLE candidate_votes (
  protocol_id     integer NOT NULL REFERENCES protocols(id) ON DELETE CASCADE,
  candidate_vibid text    NOT NULL REFERENCES candidates(vibid),
  votes           integer NOT NULL CHECK (votes >= 0),
  PRIMARY KEY (protocol_id, candidate_vibid)
);

-- ---------------------------------------------------------------- data
"""

SCHEMA_FOOTER = """\
-- ---------------------------------------------------------------- indexes
CREATE INDEX ON tiks (region_code);
CREATE INDEX ON uiks (tik_tvd);
CREATE INDEX ON uiks (district_number);
CREATE INDEX ON candidates (district_number);
CREATE INDEX ON protocols (uik_tvd);
CREATE INDEX ON protocols (tik_tvd);
CREATE INDEX ON protocols (contest, level);
CREATE INDEX ON party_votes (party_position);
CREATE INDEX ON candidate_votes (candidate_vibid);

-- ---------------------------------------------------------------- convenience views
-- One row per UIK protocol with its geography resolved.
CREATE VIEW uik_protocols AS
SELECT p.*, u.number AS uik_number, u.tik_tvd AS uik_tik_tvd, t.name AS tik_name,
       u.region_code, r.name AS region_name, u.district_number
FROM protocols p
JOIN uiks u ON u.tvd = p.uik_tvd
JOIN tiks t ON t.tvd = u.tik_tvd
JOIN regions r ON r.code = u.region_code
WHERE p.level = 'uik';

-- Party-list result per UIK, one row per (uik, party), with turnout denominators.
CREATE VIEW uik_party_results AS
SELECT p.uik_tvd, p.uik_number, p.region_code, p.region_name, p.tik_name,
       pv.party_position, pa.name AS party_name, pv.votes,
       p.registered_voters, p.valid_ballots, p.invalid_ballots,
       p.portable_box_ballots + p.stationary_box_ballots AS ballots_cast
FROM uik_protocols p
JOIN party_votes pv ON pv.protocol_id = p.id
JOIN parties pa ON pa.position = pv.party_position
WHERE p.contest = 'party_list';

ANALYZE;
COMMIT;
"""

if __name__ == "__main__":
    main()
