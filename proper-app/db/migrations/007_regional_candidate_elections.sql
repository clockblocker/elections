ALTER TABLE ballots DROP CONSTRAINT IF EXISTS ballots_kind_check;
ALTER TABLE ballots
  ADD CONSTRAINT ballots_kind_check
  CHECK (kind IN ('party-list', 'presidential', 'mayoral'));

ALTER TABLE protocols DROP CONSTRAINT IF EXISTS protocols_report_type_check;
ALTER TABLE protocols
  ADD CONSTRAINT protocols_report_type_check
  CHECK (report_type IN (226, 234, 242, 430));
