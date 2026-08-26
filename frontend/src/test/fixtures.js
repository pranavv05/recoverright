// Fixtures shaped exactly like the payloads api/main.py returns.
//
// Field names are copied from the endpoint handlers rather than invented:
// `_load_invoice_rows` for invoices, `api_metrics` for metrics,
// `api_client_health` for health rows, `compute_batch_analytics` for
// analytics, and `audit.trail.get_audit_trail` for the trail. A test that
// passes against a fixture whose keys have drifted from the API proves
// nothing, so these are the one place the shapes are written down.

// An invoice the engine cleared for autonomous action.
export const allowedInvoice = {
  invoice_id: 'INV-1001',
  client_name: 'Sahyadri Precision Auto',
  amount_paise: 45000000,
  outstanding_balance_paise: 45000000,
  days_overdue: 31,
  account_value_tier: 'standard',
  scenario_type: 'payment_link_expired',
  client_notes: 'The payment link you sent expired before I could use it.',
  processed_at: '2026-08-20T10:15:00Z',
  diagnosis: {
    diagnosed_cause: 'payment_link_expired',
    evidence_tier: 'known',
    evidence_cited: 'Client states the link expired',
    recommended_action: 'create_payment_link',
    suspicious_content_detected: false,
  },
  diagnosis_failed: false,
  policy_decision: 'ALLOW',
  policy_reasons: ['evidence_tier_known'],
  execution_status: 'link_created',
}

// A dispute: a hard stop the engine blocks regardless of what the model wanted.
export const blockedInvoice = {
  invoice_id: 'INV-1042',
  client_name: 'Nilgiri Textiles',
  amount_paise: 128000000,
  outstanding_balance_paise: 128000000,
  days_overdue: 64,
  account_value_tier: 'standard',
  scenario_type: 'disputed_invoice',
  client_notes:
    'We are disputing this invoice in full — the quantities billed do not match what was delivered to our warehouse in June.',
  processed_at: '2026-08-20T10:16:00Z',
  diagnosis: {
    diagnosed_cause: 'dispute',
    evidence_tier: 'known',
    evidence_cited: 'Client disputes the billed quantities',
    recommended_action: 'send_reminder',
    suspicious_content_detected: false,
  },
  diagnosis_failed: false,
  policy_decision: 'BLOCK',
  policy_reasons: ['dispute_detected', 'automated_chasing_forbidden'],
  execution_status: 'blocked',
}

// A high-value account: allowed in principle, but a human signs it off.
export const humanInvoice = {
  invoice_id: 'INV-1077',
  client_name: 'Konkan Marine Logistics',
  amount_paise: 5200000000,
  outstanding_balance_paise: 5200000000,
  days_overdue: 22,
  account_value_tier: 'strategic',
  scenario_type: 'cash_flow_delay',
  client_notes: '',
  processed_at: '2026-08-20T10:17:00Z',
  diagnosis: {
    diagnosed_cause: 'cash_flow_delay',
    evidence_tier: 'strong_inference',
    evidence_cited: '',
    recommended_action: 'send_reminder',
    suspicious_content_detected: false,
  },
  diagnosis_failed: false,
  policy_decision: 'REQUIRE_HUMAN',
  policy_reasons: ['strategic_account_requires_approval'],
  execution_status: 'escalated',
}

// The fail-closed path: the LLM call did not produce a usable diagnosis.
export const failedDiagnosisInvoice = {
  invoice_id: 'INV-1099',
  client_name: 'Deccan Agro Exports',
  amount_paise: 900000,
  outstanding_balance_paise: 900000,
  days_overdue: 12,
  account_value_tier: 'standard',
  scenario_type: 'unknown',
  client_notes: '',
  processed_at: '2026-08-20T10:18:00Z',
  diagnosis: null,
  diagnosis_failed: true,
  policy_decision: 'REQUIRE_HUMAN',
  policy_reasons: ['diagnosis_unavailable'],
  execution_status: 'escalated',
}

export const invoices = [allowedInvoice, blockedInvoice, humanInvoice, failedDiagnosisInvoice]

export const metrics = {
  total_processed: 4,
  allow_count: 1,
  block_count: 1,
  require_human_count: 2,
  verified_recovered_paise: 45000000,
  recoverable_paise: 45000000,
  requiring_more_data_count: 1,
  escalated_to_human_count: 2,
  suspicious_content_detected_count: 0,
  api_failures_recovered_count: 1,
}

export const healthScores = [
  {
    client_name: 'Nilgiri Textiles',
    risk_tier: 'critical',
    pattern_description: 'Two open disputes and no payment in 90 days.',
    invoice_count: 3,
    total_outstanding_paise: 128000000,
    dominant_cause: 'dispute',
    promise_broken_count: 0,
    dispute_count: 2,
    days_overdue_avg: 64,
    payment_trend: 'deteriorating',
    capital_flag: false,
    capital_flag_reason: null,
    scored_at: '2026-08-20T10:20:00Z',
  },
  {
    client_name: 'Konkan Marine Logistics',
    risk_tier: 'watch',
    pattern_description: 'Pays late but pays; strategic account.',
    invoice_count: 1,
    total_outstanding_paise: 5200000000,
    dominant_cause: 'cash_flow_delay',
    promise_broken_count: 1,
    dispute_count: 0,
    days_overdue_avg: 22,
    payment_trend: 'stable',
    capital_flag: true,
    capital_flag_reason: 'Large stable receivable — working-capital candidate',
    scored_at: '2026-08-20T10:20:00Z',
  },
  {
    client_name: 'Sahyadri Precision Auto',
    risk_tier: 'healthy',
    pattern_description: 'One expired link, otherwise on time.',
    invoice_count: 1,
    total_outstanding_paise: 45000000,
    dominant_cause: 'payment_link_expired',
    promise_broken_count: 0,
    dispute_count: 0,
    days_overdue_avg: 31,
    payment_trend: 'improving',
    capital_flag: false,
    capital_flag_reason: null,
    scored_at: '2026-08-20T10:20:00Z',
  },
]

export const analytics = {
  batch_summary: {
    total_invoices_processed: 4,
    total_outstanding_paise: 5473900000,
    total_recovered_paise: 45000000,
    overall_recovery_rate: 8.2,
  },
  recovery_by_scenario: [
    { scenario_type: 'payment_link_expired', recovery_rate: 100, total_invoices: 1 },
    { scenario_type: 'cash_flow_delay', recovery_rate: 45, total_invoices: 1 },
    { scenario_type: 'disputed_invoice', recovery_rate: 0, total_invoices: 1 },
  ],
  top_policy_rules: [
    { reason: 'dispute_detected', count: 1 },
    { reason: 'strategic_account_requires_approval', count: 1 },
  ],
  multi_invoice_clients: [
    {
      client_name: 'Nilgiri Textiles',
      invoice_count: 2,
      total_outstanding_paise: 128000000,
      invoice_ids: ['INV-1042', 'INV-1043'],
    },
  ],
  avg_overdue_by_cause: [
    { diagnosed_cause: 'dispute', avg_days_overdue: 64, invoice_count: 1 },
    { diagnosed_cause: 'payment_link_expired', avg_days_overdue: 31, invoice_count: 1 },
  ],
}

export const emptyAnalytics = {
  batch_summary: {
    total_invoices_processed: 0,
    total_outstanding_paise: 0,
    total_recovered_paise: 0,
    overall_recovery_rate: 0,
  },
  recovery_by_scenario: [],
  top_policy_rules: [],
  multi_invoice_clients: [],
  avg_overdue_by_cause: [],
}

// A full trail for a BLOCK, in the chronological order get_audit_trail returns.
export const blockedTrail = [
  {
    event_type: 'reasoning_trace',
    created_at: '2026-08-20T10:16:01Z',
    diagnosis: {
      signals: {
        engagement_signal: 'responsive',
        payment_history_pattern: 'consistently_on_time',
        content_signal: 'dispute',
        content_detail: 'quantities billed do not match delivery',
        evidence_available: true,
      },
      thought_1_engagement: 'The client replied within a day.',
      thought_2_content: 'The note raises a billing dispute in explicit terms.',
      thought_3_payment_history: 'No prior late payments on this account.',
      thought_4_evidence_quality: 'The dispute is stated directly — this is known, not inferred.',
      thought_5_cause_and_action: 'Cause is a dispute; chasing would escalate it.',
    },
  },
  {
    event_type: 'diagnosis',
    created_at: '2026-08-20T10:16:02Z',
    diagnosis: {
      diagnosed_cause: 'dispute',
      evidence_tier: 'known',
      evidence_cited: 'Client disputes the billed quantities',
      recommended_action: 'send_reminder',
    },
  },
  {
    event_type: 'policy_decision',
    created_at: '2026-08-20T10:16:03Z',
    decision: 'BLOCK',
    reasons: ['dispute_detected', 'automated_chasing_forbidden'],
    blocked_actions: ['send_reminder', 'create_payment_link'],
  },
  {
    event_type: 'execution_complete',
    created_at: '2026-08-20T10:16:04Z',
    execution_result: { status: 'blocked', action_taken: null },
  },
]

// The same trail shape for an ALLOW, which has no refusal to explain.
export const allowedTrail = [
  {
    event_type: 'diagnosis',
    created_at: '2026-08-20T10:15:02Z',
    diagnosis: {
      diagnosed_cause: 'payment_link_expired',
      evidence_tier: 'known',
      evidence_cited: 'Client states the link expired',
      recommended_action: 'create_payment_link',
    },
  },
  {
    event_type: 'policy_decision',
    created_at: '2026-08-20T10:15:03Z',
    decision: 'ALLOW',
    reasons: ['evidence_tier_known'],
    blocked_actions: [],
  },
  {
    event_type: 'execution_complete',
    created_at: '2026-08-20T10:15:04Z',
    execution_result: { status: 'link_created', payment_link_id: 'plink_test_123' },
  },
]

// Convenience builder for the axios mock: maps a URL onto its payload.
export function apiResponses(overrides = {}) {
  return {
    '/api/invoices': invoices,
    '/api/metrics': metrics,
    '/api/client-health': healthScores,
    '/api/analytics': analytics,
    ...overrides,
  }
}
