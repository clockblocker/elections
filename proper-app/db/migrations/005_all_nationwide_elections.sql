ALTER TABLE ballots DROP CONSTRAINT IF EXISTS ballots_kind_check;
ALTER TABLE ballots
  ADD CONSTRAINT ballots_kind_check CHECK (kind IN ('party-list', 'presidential'));

ALTER TABLE protocols DROP CONSTRAINT IF EXISTS protocols_report_type_check;
ALTER TABLE protocols
  ADD CONSTRAINT protocols_report_type_check CHECK (report_type IN (226, 242, 430));

ALTER TABLE dataset_coverage
  RENAME COLUMN imported_party_protocols TO imported_protocols;
ALTER TABLE dataset_coverage
  RENAME COLUMN missing_party_protocols TO missing_protocols;

UPDATE analysis_methods
SET description = 'Robust bivariate core fitted to turnout and selected-option result across all physical UIK protocols, with finite-count variance and per-protocol chi-square incompatibility grades.'
WHERE slug = 'protocol-cloud-clt-v3';
