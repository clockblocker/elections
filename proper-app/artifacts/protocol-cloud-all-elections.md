# Protocol-cloud v3 across nationwide elections

Generated from the imported physical-UIK protocols with the default
`protocol-cloud-clt-v3` parameters (`coreFraction = 0.5`, `reviewThreshold = 0.999`).
The leading party or presidential candidate by total imported votes is shown for each
election. Every election is fitted independently; no protocol contributes to another
election's core.

| Election | Target | Protocols | Scored | U | P3 | High-high P3 | Core turnout | Core result | >=99% / >=99% | Those graded P3 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 2003 Duma | United Russia | 95,205 | 95,178 | 27 | 13,845 | 10,822 | 52.945% | 33.375% | 278 | 276 |
| 2004 President | Putin | 95,779 | 95,777 | 2 | 17,019 | 14,184 | 60.026% | 68.870% | 1,614 | 1,599 |
| 2007 Duma | United Russia | 96,193 | 96,182 | 11 | 15,591 | 14,071 | 59.625% | 60.326% | 1,549 | 1,538 |
| 2008 President | Medvedev | 96,612 | 96,611 | 1 | 14,270 | 12,779 | 65.706% | 66.111% | 983 | 975 |
| 2011 Duma | United Russia | 95,225 | 95,223 | 2 | 15,750 | 14,589 | 54.257% | 38.607% | 1,071 | 1,069 |
| 2012 President | Putin | 95,415 | 95,413 | 2 | 15,574 | 14,266 | 61.165% | 59.403% | 829 | 822 |
| 2016 Duma | United Russia | 96,871 | 96,868 | 3 | 18,139 | 16,958 | 39.340% | 42.367% | 375 | 374 |
| 2018 President | Putin | 97,695 | 97,694 | 1 | 19,460 | 17,486 | 62.552% | 74.570% | 245 | 232 |
| 2021 Duma | United Russia | 96,307 | 96,284 | 23 | 16,731 | 15,496 | 41.611% | 34.333% | 121 | 119 |
| 2024 President | Putin | 91,946 | 91,939 | 7 | 5,859 | 4,709 | 75.280% | 86.596% | 589 | 423 |

## What the comparison says

- High-turnout/high-result protocols dominate the P3 population in every run.
- The >=99% turnout and >=99% target-result corner is almost entirely P3 through 2021.
  In 2024 the fitted core itself is much farther toward the upper-right, so the same
  percentage boundary is less discriminating; finite-count uncertainty also softens
  tiny unanimous protocols.
- P3 rates are vastly larger than the nominal 0.1% tail in every election. This is
  evidence that a single robust Gaussian core is an incomplete description of each
  nationwide protocol field. It is not a count or probability of fraudulent UIKs.
- Cross-year P3 counts are descriptive rather than directly causal: ballot choices,
  political geography, protocol populations, source gaps, and the fitted core all
  differ by election.
- DEG is excluded where a separate electronic dataset exists. Missing official-source
  physical protocols remain coverage gaps and are not imputed.

## Nationwide figures

- [2003 Duma](p-sus-all-uiks-2003-duma.png)
- [2004 President](p-sus-all-uiks-2004-president.png)
- [2007 Duma](p-sus-all-uiks-2007-duma.png)
- [2008 President](p-sus-all-uiks-2008-president.png)
- [2011 Duma](p-sus-all-uiks-2011-duma.png)
- [2012 President](p-sus-all-uiks-2012-president.png)
- [2016 Duma](p-sus-all-uiks-2016-duma.png)
- [2018 President](p-sus-all-uiks-2018-president.png)
- [2021 Duma](p-sus-all-uiks-2021-v3.png)
- [2024 President](p-sus-all-uiks-2024-president.png)

The workbench election and target selectors expose the same independent fit for every
imported party or candidate, not only the leading options summarized here.
