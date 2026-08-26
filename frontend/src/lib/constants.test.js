import { describe, expect, it } from 'vitest'
import {
  DECISION_CLASS,
  DECISION_TONE_CLASS,
  EVIDENCE_LABELS,
  OUTCOME_LABELS,
  RISK_TIER_CLASS,
  RISK_TIER_LABELS,
  RISK_TIER_ORDER,
  TREND_LABELS,
} from './constants'

// These maps are the contract between the backend's machine strings and what
// a judge reads on the dashboard. A vocabulary the backend emits but the
// dashboard has no label for renders as a raw token or a blank cell, so the
// vocabularies are pinned here — copied from the Python that produces them.

describe('policy decision vocabulary', () => {
  // policy/engine.py returns exactly these three decisions.
  const DECISIONS = ['ALLOW', 'BLOCK', 'REQUIRE_HUMAN']

  it('has a badge class for every decision the engine can return', () => {
    for (const decision of DECISIONS) {
      expect(DECISION_CLASS[decision]).toBeTruthy()
    }
  })

  it('has a reasoning-chain tone for every decision', () => {
    for (const decision of DECISIONS) {
      expect(DECISION_TONE_CLASS[decision]).toBeTruthy()
    }
  })

  it('gives each decision a distinct class so they are visually separable', () => {
    expect(new Set(Object.values(DECISION_CLASS)).size).toBe(DECISIONS.length)
  })

  it('does not carry labels for decisions the engine cannot return', () => {
    expect(Object.keys(DECISION_CLASS).sort()).toEqual([...DECISIONS].sort())
  })
})

describe('evidence tier vocabulary', () => {
  // VALID_EVIDENCE_TIERS in policy/rules.py.
  it('labels all three tiers the diagnoser is allowed to emit', () => {
    expect(Object.keys(EVIDENCE_LABELS).sort()).toEqual([
      'known',
      'strong_inference',
      'unknown',
    ])
  })
})

describe('risk tier vocabulary', () => {
  // evaluation/health_scorer.py assigns one of these four.
  const TIERS = ['critical', 'at_risk', 'watch', 'healthy']

  it('labels and classes cover every tier the health scorer assigns', () => {
    for (const tier of TIERS) {
      expect(RISK_TIER_LABELS[tier]).toBeTruthy()
      expect(RISK_TIER_CLASS[tier]).toBeTruthy()
      expect(RISK_TIER_ORDER[tier]).toBeTypeOf('number')
    }
  })

  it('orders tiers worst-first, matching _RISK_TIER_ORDER in api/main.py', () => {
    expect(RISK_TIER_ORDER.critical).toBeLessThan(RISK_TIER_ORDER.at_risk)
    expect(RISK_TIER_ORDER.at_risk).toBeLessThan(RISK_TIER_ORDER.watch)
    expect(RISK_TIER_ORDER.watch).toBeLessThan(RISK_TIER_ORDER.healthy)
  })
})

describe('payment trend vocabulary', () => {
  it('labels every trend the health scorer can report', () => {
    expect(Object.keys(TREND_LABELS).sort()).toEqual([
      'deteriorating',
      'improving',
      'insufficient_data',
      'stable',
    ])
  })
})

describe('execution outcome vocabulary', () => {
  // The statuses execution/workers.py writes into execution_result.status.
  const WORKER_STATUSES = [
    'escalated',
    'blocked',
    'waiting',
    'draft_created',
    'simulated',
    'link_created',
    'duplicate_link_prevented',
    'failed_max_retries',
    'failed_server_error',
    'failed_client_error',
    'failed',
  ]

  it('has a human label for every status the worker layer emits', () => {
    for (const status of WORKER_STATUSES) {
      expect(OUTCOME_LABELS[status]).toBeTruthy()
    }
  })

  it('labels the two fault statuses that should never reach the dashboard', () => {
    // `unhandled_action` (execution/actions.py) and `error` (the async batch
    // runner) both mean something went wrong upstream. Rendering them as a
    // blank outcome cell would hide a fault rather than surface it.
    expect(OUTCOME_LABELS.unhandled_action).toBeTruthy()
    expect(OUTCOME_LABELS.error).toBeTruthy()
  })

  it('still labels the pre-refactor statuses left in existing audit logs', () => {
    // Dropping these would blank out the outcome column for any invoice
    // processed before the orchestrator-workers refactor, including rows the
    // demo scripts write.
    for (const status of ['escalated_to_human', 'reminder_simulated', 'no_action_within_pattern']) {
      expect(OUTCOME_LABELS[status]).toBeTruthy()
    }
  })

  it('never labels a failure as a success', () => {
    for (const [status, label] of Object.entries(OUTCOME_LABELS)) {
      if (status.startsWith('failed')) {
        expect(label.toLowerCase()).toContain('failed')
      }
    }
  })

  it('distinguishes a blocked outcome from an escalated one', () => {
    expect(OUTCOME_LABELS.blocked).not.toBe(OUTCOME_LABELS.escalated)
  })
})
