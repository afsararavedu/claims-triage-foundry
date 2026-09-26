# Triage Decision Policy (SYNTHETIC)

> How findings map to the recommended next action. The `recommend_next_action` tool reads
> this file at runtime; the Adjuster Briefing Agent explains the result but cannot override it.
> A human adjuster confirms or overrides every recommendation at the review gate.

## Allowed actions
- `auto_approve` — no findings of medium or high severity and the amount is under the ceiling.
- `request_more_documentation` — the claim is incomplete or needs evidence before a decision.
- `route_to_investigator` — a coverage problem or a high-severity fraud indicator exists.

## Action precedence
- **Action precedence:** `route_to_investigator > request_more_documentation > auto_approve`

## Auto-approve ceiling
- **Auto-approve ceiling:** `10000`

## Finding-to-action mapping (validation and coverage findings)

| finding_code              | action                     | note                                              |
|---------------------------|----------------------------|---------------------------------------------------|
| missing_required_field    | request_more_documentation | Ask the policyholder for the missing fields.      |
| invalid_amount            | request_more_documentation | Amount must be a positive number.                 |
| invalid_date              | request_more_documentation | Dates must be ISO format YYYY-MM-DD.              |
| date_inconsistency        | request_more_documentation | Loss date cannot be after the report date.        |
| future_date               | request_more_documentation | Dates cannot be in the future.                    |
| invalid_policy_number     | request_more_documentation | Confirm the policy number with the policyholder.  |
| unknown_policy            | request_more_documentation | Policy not found in the coverage records.         |
| duplicate_claim_id        | route_to_investigator      | Reconcile duplicates before any payment.          |
| peril_not_covered         | route_to_investigator      | Possible denial; a human must decide.             |
| loss_outside_policy_period| route_to_investigator      | Possible denial; a human must decide.             |
| exceeds_policy_limit      | route_to_investigator      | Amount above the limit.                           |
| coverage_unverifiable     | request_more_documentation | Missing data prevents a coverage decision.        |
| tool_failure              | request_more_documentation | Automated check failed; manual review needed.     |
