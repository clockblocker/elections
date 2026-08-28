ALTER TABLE regions ADD COLUMN tvd text;
UPDATE regions SET tvd = code WHERE tvd IS NULL;
ALTER TABLE regions ALTER COLUMN tvd SET NOT NULL;

ALTER TABLE regions DROP CONSTRAINT IF EXISTS regions_election_id_code_key;
ALTER TABLE regions
  ADD CONSTRAINT regions_election_id_tvd_key UNIQUE (election_id, tvd);
CREATE INDEX IF NOT EXISTS regions_election_code_idx ON regions(election_id, code);
