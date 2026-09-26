# Historical Fraud-Indicator Patterns (SYNTHETIC)

> Narrative knowledge used for grounded explanations ("why was this flagged?").
> Synthetic examples only; no real policyholders.

## FI-01 — Inception fraud ("buy after the loss")
Pattern: the policy starts days before a large theft or fire claim. In the synthetic
history set, 4 of 5 claims filed within a week of inception were withdrawn once a police
report or proof of ownership was requested. Investigators check the purchase channel, the
prior insurer and whether the property was insured elsewhere at the loss date.
Related rules: UW-001, UW-005.

## FI-02 — Limit-seeking amounts
Pattern: claimed amounts sit exactly at, or a few hundred below, the policy limit. Genuine
losses rarely land on the limit. Ask for the itemised list and compare with typical
replacement costs. Related rules: UW-002, UW-008.

## FI-03 — Repeat water damage
Pattern: repeated water-damage claims on the same property within a year. Common causes
are unrepaired earlier damage being claimed again, or chronic leaks (gradual damage is
typically excluded). Investigators ask for the repair invoice from the earlier claim and
a plumber's cause-of-loss report. Related rules: UW-004.

## FI-04 — Late reporting of small losses
Pattern: small losses reported weeks later are usually honest but poorly documented.
Requesting dated photos and receipts resolves most of them without investigation.
Related rules: UW-003.

## FI-05 — Wrong product for the peril
Pattern: a peril claimed on a product that does not cover it (for example vehicle damage on
a homeowners policy). Usually a filing error by the policyholder or agent; confirm whether
a separate policy exists before denying. Related: coverage check `peril_not_covered`.

## FI-06 — Duplicate submissions
Pattern: the same claim ID, or the same loss submitted twice with small changes. Often a
system resubmission, but occasionally an attempt to be paid twice. Always reconcile before
payment. Related: validation check `duplicate_claim_id`.
