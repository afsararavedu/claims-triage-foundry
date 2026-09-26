# Underwriting & Claims-Triage Rules (SYNTHETIC)

> Synthetic rulebook for the Claims Triage Assistant demo. Not real underwriting guidance.
>
> Each rule is a self-contained section so the vector store can index it as one chunk.
> The `Condition` line is machine-readable (`feature operator value`, joined with `AND`).
> Features are computed by the `assess_fraud_indicators` tool:
> `days_since_inception`, `days_until_expiry`, `amount_to_limit_ratio`, `report_lag_days`,
> `prior_similar_claims_12m`, `prior_claims_12m`, `claim_amount`, `claim_type`, `is_round_amount`.
> Change a threshold here and the system's behaviour changes with no code change.

## UW-001 — Early claim after policy inception
- **Condition:** `days_since_inception <= 7`
- **Severity:** high
- **Action:** route_to_investigator
- **Rationale:** Claims filed within 7 days of policy inception are flagged for review regardless of amount. Early-inception losses are a classic indicator of a policy bought after the loss occurred.

## UW-002 — Claim amount at or near the policy limit
- **Condition:** `amount_to_limit_ratio >= 0.98`
- **Severity:** high
- **Action:** route_to_investigator
- **Rationale:** Claim amounts within 2% of the policy limit (or above it) warrant investigator review. Amounts engineered to the limit suggest inflation of the loss.

## UW-003 — Late reporting
- **Condition:** `report_lag_days > 30`
- **Severity:** medium
- **Action:** request_more_documentation
- **Rationale:** Losses reported more than 30 days after they occurred need contemporaneous evidence (dated photos, repair quotes, third-party reports) because the delay makes the loss harder to verify.

## UW-004 — Repeat peril on the same policy
- **Condition:** `prior_similar_claims_12m >= 1`
- **Severity:** high
- **Action:** route_to_investigator
- **Rationale:** A second claim of the same peril type (for example a second water-damage claim) on the same policy within 12 months should be reviewed for unrepaired prior damage, maintenance neglect, or double-claiming the same damage.

## UW-005 — High-value theft
- **Condition:** `claim_type == theft AND claim_amount >= 10000`
- **Severity:** medium
- **Action:** request_more_documentation
- **Rationale:** Theft claims of 10,000 or more require a police report number, proof of ownership (receipts, appraisals, photos) and, for jewelry, a scheduled-items rider check.

## UW-006 — High-value claim needs itemised proof of loss
- **Condition:** `claim_amount > 15000`
- **Severity:** medium
- **Action:** request_more_documentation
- **Rationale:** Any claim above 15,000 requires an itemised proof-of-loss statement and at least one independent repair or replacement estimate before settlement.

## UW-007 — Loss close to policy expiry
- **Condition:** `days_until_expiry <= 14`
- **Severity:** medium
- **Action:** request_more_documentation
- **Rationale:** Losses in the final 14 days of the policy term need evidence of the loss date, because the incentive to claim before cover lapses is higher.

## UW-008 — Round-number high amount
- **Condition:** `is_round_amount == 1 AND claim_amount >= 10000`
- **Severity:** low
- **Action:** none
- **Rationale:** Large, perfectly round amounts (exact multiples of 1,000) are a weak fraud signal on their own. Mention it in the briefing but do not change the recommendation because of it.

## UW-009 — High claim frequency
- **Condition:** `prior_claims_12m >= 3`
- **Severity:** high
- **Action:** route_to_investigator
- **Rationale:** Three or more claims of any type on the same policy within 12 months indicates a frequency pattern that should be reviewed by special investigations.
