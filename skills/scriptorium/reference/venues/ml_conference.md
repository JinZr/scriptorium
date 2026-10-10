# Machine-learning conferences

NeurIPS, ICML, ICLR, COLM, and the ACL family. Use this page to draft the review brief with the author: it is a question list and a set of defaults, not frozen prompt text.

## What reviewers judge

- Whether the stated contributions match what the paper shows, and how they differ from recent work.
- Empirical support: current, comparably tuned baselines; ablations that isolate the claimed component; variance over seeds or confidence intervals; no test-set leakage.
- Whether headline claims (state of the art, generality, efficiency, scaling) hold at the scope stated.
- Reproducibility of the main result: data, hyperparameters, compute, and code availability.
- Clarity of the method and of the main table or figure.

## What typically blocks acceptance

- A headline gain that disappears against a stronger or properly tuned baseline, or that sits within noise.
- Generality claimed from one dataset, model size, language, or task.
- No ablation for the component the paper credits with the gain.
- A theoretical claim whose proof has a gap, or whose assumptions the experiments violate.
- Unfair comparisons: different compute budgets, data, or tuning effort.
- Overlap with prior work that the paper does not acknowledge.

## Compliance items

Report these as `submission_compliance` at `minor`: page limit, anonymity (names, acknowledgements, self-identifying links or repositories), checklist answers, template margins and fonts, required limitations or broader-impact sections, and supplement size.

## Usually ignored

Typographical polish, notation preferences, reference formatting, figure styling, and the length of related work, unless they hide or distort a result.

## Questions for the author

1. Which venue, and is this the submission, a rebuttal revision, or the camera-ready version?
2. Which two or three claims must survive review?
3. What did earlier reviewers, internal or external, object to?
4. Which weaknesses are already known, such as a single seed or a missing baseline?
5. What should this round ignore, such as the checklist or writing polish?

## Defaults

If the author says "defaults": `venue_family` `ml_conference`, the venue named by the template if any, stage `presubmission`, and every list empty.
