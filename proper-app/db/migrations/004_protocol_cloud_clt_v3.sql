INSERT INTO analysis_methods (slug, version, name, description, parameters)
VALUES (
  'protocol-cloud-clt-v3',
  3,
  'Election-wide protocol cloud',
  'Robust bivariate core fitted to turnout and selected-party result across all physical UIK protocols, with finite-count variance and per-protocol chi-square incompatibility grades.',
  '{"coreFraction":0.5,"coreIterations":8,"covarianceRidge":0.0001,"fdrThreshold":0.05,"reviewThreshold":0.999,"degIncluded":false}'::jsonb
)
ON CONFLICT (slug) DO UPDATE SET
  version = EXCLUDED.version,
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  parameters = EXCLUDED.parameters;

UPDATE dataset_coverage
SET deg_policy = 'excluded-outside-protocol-cloud-model', updated_at = now()
WHERE deg_policy IN ('excluded-outside-shpilkin-model', 'excluded-outside-peer-clt-model');
