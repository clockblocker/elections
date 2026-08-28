INSERT INTO analysis_methods (slug, version, name, description, parameters)
VALUES (
  'peer-clt-v2',
  2,
  'Peer-conditioned CLT residuals',
  'Leave-one-out TIK/region peer expectations with hierarchical shrinkage, robust overdispersion, empirical P_sus calibration, and Benjamini-Yekutieli review q-values.',
  '{"turnoutBinWidth":2.5,"turnoutWindow":10,"minTikPeers":8,"minRegionPeers":30,"tikPriorBallots":2000,"regionPriorBallots":10000,"dispersionPriorPoints":30,"fdrThreshold":0.05,"degIncluded":false}'::jsonb
)
ON CONFLICT (slug) DO UPDATE SET
  version = EXCLUDED.version,
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  parameters = EXCLUDED.parameters;

UPDATE dataset_coverage
SET deg_policy = 'excluded-outside-peer-clt-model', updated_at = now()
WHERE deg_policy = 'excluded-outside-shpilkin-model';
