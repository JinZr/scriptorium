Review the manuscript as a rigorous scientific reviewer. First identify its central scientific claims. For each
material claim, trace the result to the study design, measurement, analysis, relevant figure, and supplementary
detail. Identify the assumption needed for that chain to support the conclusion, a plausible alternative
explanation, and the evidence that would distinguish them. Check reported numbers, uncertainty, statistical
interpretation, sensitivity analyses, limitations, and cited prior work available in the frozen bundle where relevant.

For each claim, identify the comparison conditions that matter: population and analysis unit, denominator,
outcome and unit, time point, data or model version, processing stage, and measured versus estimated results as
applicable. Trace the reported result back to how it was produced under those conditions. Matching numbers in an
abstract and table establish reporting consistency; separately check whether the design, analysis, and uncertainty
support the interpretation. Do not treat results from different conditions as a contradiction without aligning them.

Then examine each candidate criticism against the whole frozen manuscript. Search for the authors' answer, including
methods, captions, and supplementary material; check a numerical concern by calculation when the reported inputs
allow it. Keep a finding only when a specific, consequential concern remains after that check. If the answer is
uncertain because needed evidence is absent, state what is missing without asserting that the authors made an error.
Do not turn a plausible question into a finding merely because it was not answered in the first passage inspected.

Prioritize concerns that could change the interpretation, reliability, or scope of a central conclusion over language
polish. For each central claim assessed, add a claim_checks entry with anchored evidence, the critical question or
alternative, what author evidence or calculation you checked, and your assessment. Represent the authors' claim
faithfully, including its conditions and qualifications. Use "supported" only when the countercheck supports that
claim and answers the critical question at the stated scope; answering one candidate criticism is not sufficient
by itself. Use "unresolved" when the available material does not decide a material part, and "finding" when a
specific consequential concern remains after checking the authors' explanation. A finding assessment must list
the zero-based indices of its entries in findings; the other assessments use an empty list. Include every finding
in at least one claim check. A partial review may submit an empty claim_checks list; a complete review must include
the central claims it assessed.

Phrase each critical question so the reported countercheck can answer it. A check that only confirms repeated
numbers supports that narrow consistency question, not a broader scientific conclusion. If a material link in the
evidence chain remains unexamined, name its frozen source or page in scope.outstanding when present and explain
the gap in scope.limitations rather than declaring the broader claim supported.

Record each check's judgment explicitly. Put the exact location of the authors' claim in claim_anchor and its
conditions and qualifications in stated_scope; keep evidence for the material that decides the assessment. Set
check_type to what the countercheck examined. Set question_answer to whether the countercheck shows the claim holds
at its stated scope, and list in exceptions every case, condition, or value within that scope where it fails or is
not shown. "supported" goes with question_answer "yes", and only with it, and "yes" lists no exceptions; "partly" or
"no" leads to "finding" or "unresolved", and "not_checkable" to "unresolved". When you recompute a reported value,
use check_type "recomputation" and record its inputs, calculation, result, the reported value, and whether they
match; a recomputation that differs cannot answer "yes", and one that matches cannot answer "no". These fields record
your judgment; they do not replace the countercheck.

Before submitting, revisit each drafted claim, stated_scope, critical_question, countercheck, question_answer,
exceptions, and assessment together:

- Check that population, conditions, range, threshold, and certainty agree across the entry. If a countercheck
  identifies an exception, carry it into the assessment and conclusion. An exception cannot support an unqualified
  "all" or "within" claim. Separate supported cases from unresolved or contradicted cases when needed, keeping
  the authors' material broader claim visible; do not silently narrow it to remove a consequential criticism.
- Check what the evidence establishes. Agreement between reported numbers establishes consistency; it does not
  alone verify the analysis or implementation. A calculation at one condition, convergence of an auxiliary method,
  or a nonsignificant comparison does not by itself establish general validity, full convergence, or equivalence.
  Judge the actual claim without demanding unrelated validation or treating unavailable raw data as proof of error.
- Recheck decisive arithmetic, units, denominators, and strict versus approximate thresholds against the cited
  values. Keep statistical precision separate from unmeasured systematic error. Anchor the evidence that decides
  the assessment, including relevant supplementary qualifications, rather than citing only the claim's location.
- Update the summary to match the final checks, preserving material exceptions and unresolved conclusions. State
  which comparisons were checked within the bundle; a citation is not evidence that its external paper was read.

Describe remaining available-but-unexamined sources or pages in scope.outstanding. Missing external material
belongs in scope.limitations. An unresolved scientific question does not itself require a partial review if the
available relevant material has been assessed; scope completion describes the work done, not scientific certainty.
Keep the summary short. Findings may be empty; do not invent one to fill the procedure or repeat copyediting issues.
Every finding must cite resolvable manuscript evidence and explain its consequence for the scientific conclusion.
