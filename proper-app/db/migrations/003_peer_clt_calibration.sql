UPDATE analysis_methods
SET parameters = parameters || '{"reviewThreshold":0.999}'::jsonb,
    description = 'Leave-one-out TIK/region peer expectations with hierarchical shrinkage, robust and empirically calibrated overdispersion, P_sus residual percentiles, and diagnostic Benjamini-Yekutieli q-values.'
WHERE slug = 'peer-clt-v2';
