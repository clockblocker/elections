# Regional baseline odds — method version 1

This is a custom **Shpilkin-style descriptive estimate**, not a finding that a specific
precinct committed fraud. It applies only to physical UIK party-list protocols from the
2021 State Duma election. DEG is excluded because aggregate electronic returns do not
have the precinct distribution required by this comparison.

For precinct `i`:

```text
turnout_i = 100 × (portable-box ballots + stationary-box ballots) / registered voters
other_i   = valid ballots - target-party votes
```

The default reference set contains physical precincts with turnout in `[20%, 50%)`.
For every region with usable reference precincts:

```text
baseline odds_region = sum(target-party votes) / sum(other votes)
```

For a precinct at or above the default 50% analysis threshold:

```text
expected target votes_i = other_i × baseline odds_region
estimated excess_i      = max(0, observed target votes_i - expected target votes_i)
```

When a selected region has no usable reference observations, the pooled reference odds
are used and the fallback is exposed in the output. The national estimate is the sum of
positive precinct estimates. The reference band and analysis threshold are parameters;
every exported result must include them with method slug `shpilkin-odds-v1` and version 1.

## Interpretation limits

- The method assumes target-to-other voting odds would remain comparable above the
  reference turnout band after regional stratification.
- Demography, precinct type, mobilization, and reporting artifacts can violate that
  assumption.
- Positive-only aggregation does not net negative deviations against positive ones.
- Missing source protocols remain missing; the model does not impute them.
- DEG is outside the model and outside every denominator shown by the app.
