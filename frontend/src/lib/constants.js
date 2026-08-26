// Machine strings from the backend mapped to display labels and CSS classes.
// Kept in one module so a new policy decision, risk tier, or execution status
// has exactly one place to be registered, and so tests can assert that the
// vocabulary the API emits is fully covered here.

export const EVIDENCE_LABELS = {
  known: 'Known',
  strong_inference: 'Strong Inference',
  unknown: 'Unknown',
}

export const DECISION_CLASS = {
  ALLOW: 'badge-allow',
  BLOCK: 'badge-block',
  REQUIRE_HUMAN: 'badge-human',
}

export const DECISION_TONE_CLASS = {
  ALLOW: 'reasoning-tone-allow',
  BLOCK: 'reasoning-tone-block',
  REQUIRE_HUMAN: 'reasoning-tone-human',
}

export const RISK_TIER_LABELS = {
  critical: 'Critical',
  at_risk: 'At Risk',
  watch: 'Watch',
  healthy: 'Healthy',
}

export const RISK_TIER_CLASS = {
  critical: 'badge-risk-critical',
  at_risk: 'badge-risk-at_risk',
  watch: 'badge-risk-watch',
  healthy: 'badge-risk-healthy',
}

// Worst-first. Mirrors _RISK_TIER_ORDER in api/main.py, which the
// /api/client-health endpoint already sorts by; the dashboard re-sorts
// client-side so the column headers stay interactive.
export const RISK_TIER_ORDER = { critical: 0, at_risk: 1, watch: 2, healthy: 3 }

export const TREND_LABELS = {
  improving: 'Improving',
  stable: 'Stable',
  deteriorating: 'Deteriorating',
  insufficient_data: 'Insufficient data',
}

// The outcome comes from the recorded execution event, not from re-deriving it
// from the decision — every terminal state now writes one, including BLOCK and
// REQUIRE_HUMAN, so the cell shows what was logged rather than what we assume.
export const OUTCOME_LABELS = {
  // Statuses returned by the orchestrator-workers execution layer.
  escalated: 'Escalated to human',
  blocked: 'Blocked',
  waiting: 'Waiting — within pattern',
  draft_created: 'Draft created — not sent',
  simulated: 'Simulated',
  link_created: 'Payment link created',
  duplicate_link_prevented: 'Duplicate link prevented',
  failed_max_retries: 'Failed — retries exhausted',
  failed_server_error: 'Failed — Razorpay 5xx',
  failed_client_error: 'Failed — Razorpay 4xx',
  failed: 'Failed — unexpected error',

  // Pre-refactor statuses, still produced by the demo scripts and present in
  // any audit_log written before the refactor.
  escalated_to_human: 'Escalated to human',
  reminder_simulated: 'Reminder simulated',
  information_gathering_queued: 'Info gathering queued',
  no_action_within_pattern: 'No action — within pattern',
  internal_error: 'Internal error',
  diagnosis_failed: 'Diagnosis failed',
  skipped: 'Skipped',

  // execution/actions.py returns this when the policy engine let through an
  // action no dispatcher branch handles, and the async batch runner returns
  // `error` for an invoice that failed outside its own error handling.
  // Neither should ever reach the dashboard; if one does, it must read as a
  // fault rather than as a blank cell.
  unhandled_action: 'Unhandled action — no worker matched',
  error: 'Error — invoice failed outside its handler',
}
