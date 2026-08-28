# Election-wide protocol cloud — method version 3

`protocol-cloud-clt-v3` is a **protocol-level model-incompatibility screen**. It is not
a finding of fraud and `P_sus` is not the probability that misconduct occurred. The
current application covers the 2021 State Duma federal party-list ballot at physical
UIKs; remote electronic voting (DEG) is outside the dataset, fit, and denominators.

The unit of observation is one complete physical UIK protocol. For a selected election
and ballot option, every protocol that passes the accounting checks enters one nationwide
dataset. Region and TIK filters are display slices only: they never refit the core or
change a protocol's score.

The central limit theorem does **not** imply that the raw nationwide UIK turnout/result
cloud must be Gaussian. UIKs have different electorates, geography, institutions, and
political composition. Version 3 instead fits a robust central reference field on a
variance-stabilizing scale, adds protocol-specific finite-count uncertainty, and asks
how incompatible each actual protocol is with that reference.

## 1. Complete, valid protocol

For protocol `i` and the selected option:

```text
R_i = registered voters
B_i = portable-box ballots + stationary-box ballots
n_i = valid ballots
y_i = selected-option votes
```

The method requires `R_i > 0`, `n_i > 0`, `0 <= n_i <= B_i <= R_i`, and
`0 <= y_i <= n_i`. A protocol that fails is retained in coverage and export with grade
`U` and an explicit reason; a missing source protocol cannot be graded or imputed.

The observed shares are:

```text
turnout_i = B_i / R_i
result_i  = y_i / n_i
```

Each is transformed with the same half-count continuity correction used by the code:

```text
T_i = log((B_i + 0.5) / (R_i - B_i + 0.5))
Y_i = log((y_i + 0.5) / (n_i - y_i + 0.5))
x_i = (T_i, Y_i)'
```

The correction keeps 0% and 100% protocols finite without pretending that a unanimous
one-ballot protocol is as informative as a unanimous thousand-ballot protocol.

## 2. Robust central 50% core

The default `coreFraction` is `0.5`. The fit starts at the component-wise medians of all
valid nationwide `x_i`, with a diagonal median-absolute-deviation covariance. For each
of the default eight iterations it:

1. calculates every protocol's squared Mahalanobis distance from the current center;
2. retains the central `floor(coreFraction * m)` protocols, with a minimum of 20;
3. replaces the center with the retained protocols' bivariate mean;
4. replaces the covariance with their bivariate covariance, multiplied by the
   consistency correction for a truncated bivariate Gaussian and regularized by
   `covarianceRidge`.

For core fraction `f`, the implementation uses:

```text
c_f              = -2 log(1 - f)
truncated_mean_f = 2 - c_f (1 - f) / f
multiplier_f     = 2 / truncated_mean_f
```

The iterative covariance at this point is the covariance of the **observed** central
protocol logits, so its diagonal still includes the ordinary finite-count variation of
those core protocols. Section 3 describes the correction that turns it into the
between-protocol covariance `Sigma_core`. The expected turnout and selected-option
result displayed by the app are `logit^-1(mu_T)` and `logit^-1(mu_Y)`.

This core is deliberately not conditioned on observed turnout, region, or TIK. A dense
high-turnout/high-result tail therefore remains a deviation from the central field
instead of being learned as its own local normal. Conversely, legitimate geographic or
institutional subpopulations can also differ from a national core; that is an important
interpretation limit, not proof of wrongdoing.

## 3. Finite-count predictive covariance

For protocol `i`, let:

```text
p_T = logit^-1(mu_T)
p_Y = logit^-1(mu_Y)
```

The delta-method/binomial sampling contributions on the logit scale are:

```text
s_Ti = 1 / (R_i p_T (1 - p_T))
s_Yi = 1 / (n_i p_Y (1 - p_Y))
```

First, the implementation calculates the average `s_T` and `s_Y` over the retained core
members and subtracts them from the corresponding diagonal of the observed trimmed
covariance. Each resulting variance is floored at `covarianceRidge`; the off-diagonal is
retained but clipped to `+/- 0.999 sqrt(var_T var_Y)` so the matrix remains invertible:

```text
var_T_between = max(ridge, var_T_observed_core - mean_core(s_T))
var_Y_between = max(ridge, var_Y_observed_core - mean_core(s_Y))
Sigma_core    = [[var_T_between, clipped_cov_TY],
                 [clipped_cov_TY, var_Y_between]]
```

This prevents the typical core protocol's sampling noise from being counted once in the
observed core covariance and then again for every target. Version 3 then adds the target
protocol's own sampling covariance:

```text
Sigma_i = Sigma_core + diag(s_Ti, s_Yi)
```

Thus small denominators produce a wider expected distribution and less extreme scores.
For the displayed result interval, the app takes `mu_Y +/- 1.96 sqrt(Sigma_i[Y,Y])`,
back-transforms to a share, and multiplies by `n_i`.
Protocols for which any expected turnout/result success or failure count is below 10
remain graded, as required by the complete-protocol contract, but receive the explicit
`finite-count-clt-weak` quality flag.

## 4. D2, tail probability, and `P_sus`

For the actual continuity-corrected protocol vector `x_i`, the nonconformity statistic is:

```text
D2_i = (x_i - mu)' inverse(Sigma_i) (x_i - mu)
```

Under the working bivariate-normal approximation, `D2` has a chi-square distribution
with two degrees of freedom. Its survival tail has the simple form:

```text
p_i     = Pr(chi-square_2 >= D2_i) = exp(-D2_i / 2)
P_sus_i = 1 - p_i
```

The implementation floors `p_i` at machine epsilon. `P_sus` is therefore an analytic
model-incompatibility score, not an empirical rank and never `1 - q`. Higher values mean
that the observed turnout/result pair lies farther into the working model's tail.

The 95% central contour uses `D2 = 5.99146`. Outside it, the signs of the turnout and
result logit residuals label a protocol `high-high`, `high-low`, `low-high`, or
`low-low`; points inside it are `central`. `P_sus` itself is two-sided and can be high in
any quadrant.

The versioned display grades are:

```text
P0  P_sus < 0.95        routine under the working model
P1  P_sus >= 0.95       elevated
P2  P_sus >= 0.99       high
P3  P_sus >= 0.999      extreme
U   no valid score; explicit reason retained
```

The default review queue threshold is P3 (`P_sus >= 0.999`). Grades are descriptive
screening bands, not posterior probabilities or legal findings.

## 5. Multiple-testing diagnostic

The raw chi-square tails for all scored physical UIKs in the selected nationwide
election/option family are adjusted with the Benjamini-Yekutieli procedure. If
`p_(1) <= ... <= p_(m)` and `H_m = sum(1/k, k=1..m)`, the ordered adjusted values are:

```text
q_(i) = min over j >= i of min(1, m H_m p_(j) / j)
```

BY is conservative and accommodates arbitrary dependence **if** the marginal p-values
are valid. It cannot repair a misspecified national Gaussian core, so `q` is exported as
a separate diagnostic and does not define the grade or the default review queue.

## 6. Observed 2021 validation check

On the currently imported physical-UIK dataset for United Russia with default v3
parameters, 121 protocols have both turnout and selected-option result at least 99%.
Of those, 119 receive P3. The two exceptions are the tiny unanimous protocols with
counts 4/4 and 1/1: the 4/4 protocol receives P1 (`P_sus` about 0.9890) and the 1/1
protocol receives P0 (`P_sus` about 0.4107). This is the intended consequence of
finite-count uncertainty: a very high percentage based on one or four observations is
not treated like the same percentage based on hundreds or thousands.

This check demonstrates score behavior on an observed edge case. It does not validate a
fraud claim, establish that the other 119 protocols are manipulated, or establish that
the two tiny protocols are ordinary.

## 7. Interpretation limits

- A complete protocol is the observation; individual ballots are not independent
  protocol observations.
- The CLT supports the conditional finite-count approximation. It does not make the raw
  nationwide turnout/result cloud Gaussian.
- `P_sus` measures incompatibility with this fitted robust core. It is not
  `Pr(fraud | data)`, and a low value is not proof that a protocol is correct.
- The central half is a robust descriptive reference, not an independently verified
  clean counterfactual. A shift affecting the center can be absorbed into the fit.
- The nationwide model intentionally does not explain geography, TIK structure,
  settlement type, special precinct types, demographics, mobilization, or observer/audit
  status. Legitimate heterogeneity can therefore create high scores, while coordinated
  patterns aligned with unmodeled structure can be mischaracterized.
- The analytic chi-square tail assumes the corrected bivariate logits are adequately
  approximated by the fitted Gaussian covariance. Heavy tails, multimodality, and
  spatial dependence can invalidate nominal p/q interpretations.
- The half-count and finite-count covariance sensibly soften tiny-denominator scores,
  but an exact small-count hierarchical model would be preferable for inference at the
  boundary.
- Round-percentage spikes, duplication, and TIK/region clustering are aggregate
  fingerprints. They require separate, multiplicity-controlled modules and are not
  silently folded into the per-protocol grade.
- Results are option-specific. Selecting the most anomalous option after inspection adds
  another multiple-testing layer not covered by the displayed BY value.
- DEG and missing source protocols remain outside the model and are never imputed.

## 8. Required validation

Every implementation must test accounting invariants, preservation of unscored rows,
continuity-corrected boundaries, robust-core convergence, covariance regularization,
finite-count monotonicity, known chi-square tails, grade boundaries, known BY
adjustments, and geography-filter invariance. Synthetic mixtures should additionally
measure calibration under skew, multimodality, geographic heterogeneity, small counts,
and injected isolated or coordinated shifts.

The Python implementation in `protocol_cloud_v3.py` is the independently runnable
reference. The TypeScript implementation in `frontend/src/analysis.ts` powers the app.
Exports retain the method slug/version, parameters, fitted core, raw `p`, diagnostic
BY `q`, `P_sus`, grade, direction, `D2`, and quality flags.

## Sources

- [NIST, central limit theorem](https://itl.nist.gov/div898/handbook/eda/section3/eda3661.htm)
- [Penn State, binomial overdispersion](https://online.stat.psu.edu/stat504/Lesson07)
- [Benjamini and Yekutieli, FDR under dependence](https://doi.org/10.1214/aos/1013699998)
- [Kobak, Shpilkin, and Pshenichnikov, turnout/result fingerprints](https://arxiv.org/abs/1205.0741)
- [Statistical anomaly claims require a defensible null](https://pmc.ncbi.nlm.nih.gov/articles/PMC8609310/)
