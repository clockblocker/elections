CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS elections (
  id smallserial PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  election_date date NOT NULL,
  scope_note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS regions (
  id smallserial PRIMARY KEY,
  election_id smallint NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
  code text NOT NULL,
  name text NOT NULL,
  UNIQUE (election_id, code)
);

CREATE TABLE IF NOT EXISTS tiks (
  id bigserial PRIMARY KEY,
  election_id smallint NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
  region_id smallint NOT NULL REFERENCES regions(id),
  tik_tvd text NOT NULL,
  name text NOT NULL,
  UNIQUE (election_id, tik_tvd)
);

CREATE TABLE IF NOT EXISTS precincts (
  id bigserial PRIMARY KEY,
  election_id smallint NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
  region_id smallint NOT NULL REFERENCES regions(id),
  tik_id bigint NOT NULL REFERENCES tiks(id),
  uik_number integer NOT NULL,
  uik_tvd text NOT NULL,
  kind text NOT NULL DEFAULT 'physical' CHECK (kind = 'physical'),
  UNIQUE (election_id, uik_tvd)
);

CREATE TABLE IF NOT EXISTS ballots (
  id smallserial PRIMARY KEY,
  election_id smallint NOT NULL REFERENCES elections(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind = 'party-list'),
  name text NOT NULL,
  UNIQUE (election_id, kind)
);

CREATE TABLE IF NOT EXISTS ballot_options (
  id smallserial PRIMARY KEY,
  ballot_id smallint NOT NULL REFERENCES ballots(id) ON DELETE CASCADE,
  position smallint NOT NULL,
  name text NOT NULL,
  short_name text NOT NULL,
  color text NOT NULL,
  UNIQUE (ballot_id, position)
);

CREATE TABLE IF NOT EXISTS sources (
  id bigserial PRIMARY KEY,
  sha256 char(64) NOT NULL UNIQUE,
  url text NOT NULL,
  final_url text,
  retrieved_at timestamptz,
  provenance text NOT NULL,
  source_report_type smallint NOT NULL,
  derivation text
);

CREATE TABLE IF NOT EXISTS protocols (
  id bigserial PRIMARY KEY,
  ballot_id smallint NOT NULL REFERENCES ballots(id) ON DELETE CASCADE,
  precinct_id bigint NOT NULL REFERENCES precincts(id) ON DELETE CASCADE,
  source_id bigint NOT NULL REFERENCES sources(id),
  report_type smallint NOT NULL CHECK (report_type = 242),
  imported_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ballot_id, precinct_id)
);

CREATE TABLE IF NOT EXISTS ballot_accounting (
  protocol_id bigint PRIMARY KEY REFERENCES protocols(id) ON DELETE CASCADE,
  registered_voters integer NOT NULL CHECK (registered_voters >= 0),
  ballots_received integer NOT NULL CHECK (ballots_received >= 0),
  ballots_issued_early integer NOT NULL CHECK (ballots_issued_early >= 0),
  ballots_issued_at_station integer NOT NULL CHECK (ballots_issued_at_station >= 0),
  ballots_issued_outside integer NOT NULL CHECK (ballots_issued_outside >= 0),
  ballots_cancelled integer NOT NULL CHECK (ballots_cancelled >= 0),
  portable_box_ballots integer NOT NULL CHECK (portable_box_ballots >= 0),
  stationary_box_ballots integer NOT NULL CHECK (stationary_box_ballots >= 0),
  invalid_ballots integer NOT NULL CHECK (invalid_ballots >= 0),
  valid_ballots integer NOT NULL CHECK (valid_ballots >= 0),
  lost_ballots integer NOT NULL CHECK (lost_ballots >= 0),
  unaccounted_ballots integer NOT NULL CHECK (unaccounted_ballots >= 0)
);

CREATE TABLE IF NOT EXISTS votes (
  protocol_id bigint NOT NULL REFERENCES protocols(id) ON DELETE CASCADE,
  option_id smallint NOT NULL REFERENCES ballot_options(id),
  votes integer NOT NULL CHECK (votes >= 0),
  PRIMARY KEY (protocol_id, option_id)
);

CREATE TABLE IF NOT EXISTS dataset_coverage (
  election_id smallint PRIMARY KEY REFERENCES elections(id) ON DELETE CASCADE,
  discovered_regions integer NOT NULL,
  discovered_tiks integer NOT NULL,
  discovered_uiks integer NOT NULL,
  imported_party_protocols integer NOT NULL,
  missing_party_protocols integer NOT NULL,
  deg_policy text NOT NULL,
  source_schema_version integer NOT NULL,
  source_generated_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS analysis_methods (
  id smallserial PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  version integer NOT NULL,
  name text NOT NULL,
  description text NOT NULL,
  parameters jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS precincts_region_idx ON precincts(region_id);
CREATE INDEX IF NOT EXISTS precincts_tik_idx ON precincts(tik_id);
CREATE INDEX IF NOT EXISTS protocols_precinct_idx ON protocols(precinct_id);
CREATE INDEX IF NOT EXISTS votes_option_protocol_idx ON votes(option_id, protocol_id);
