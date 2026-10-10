# Nature-family journals

Nature, the Nature research journals, Nature Communications, and other Springer Nature journals. Use this page to draft the review brief with the author: it is a question list and a set of defaults, not frozen prompt text.

## What editors and referees judge

- Significance beyond the immediate subfield; the editor decides whether to send the paper to review at all.
- Whether the conclusions follow from the data: adequate controls, independent replication, and appropriate statistics with sample sizes, tests, and error bars defined.
- Robustness and generality: independent datasets or cohorts, sensitivity analyses, and alternative explanations ruled out.
- Methods detailed enough for another group to reproduce the central result.
- Data and code availability for the central result.
- An abstract and main text that a non-specialist can follow.

## What typically blocks acceptance

- Conclusions that overreach the evidence, especially causal or general claims from correlational or limited data.
- Missing controls, or no validation on independent data.
- Statistics that are absent, undefined, or inappropriate for the design.
- An advance too small relative to prior literature for the journal's scope.
- Central data or code that cannot be shared or inspected.

## Compliance items

Report these as `submission_compliance` at `minor`: word, display-item, and reference limits; reporting-summary and editorial-policy checklists; data and code availability statements; source-data files and figure resolution; author contribution and competing-interest statements; and Methods placement.

## Usually ignored

House style, reference formatting, and light language editing, which production and copy-editing handle after acceptance, unless they obscure a result.

## Questions for the author

1. Which journal, and is this the first submission, a revision answering referees, or the accepted version?
2. Which two or three conclusions must survive review?
3. What did earlier referees or the editor ask for?
4. Which weaknesses are already known, such as a small cohort or one site?
5. What should this round ignore, such as reporting checklists or word counts?

## Defaults

If the author says "defaults": `venue_family` `nature_family`, no venue, stage `presubmission`, and every list empty.
