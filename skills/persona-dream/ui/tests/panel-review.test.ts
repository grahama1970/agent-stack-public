/** PD-UX-04 UI pure-logic tests: stale selection, backend counts verbatim,
 * edit-intent shape. No counts or statuses are ever derived client-side. */
import assert from 'node:assert/strict'
import test from 'node:test'

import { buildEditIntent, isSelectionStale, planCountsFrom, type PanelReview } from '../src/lib/panelReview'

function review(activeRevisionId: string): PanelReview {
  return {
    schema: 'persona_dream.ux_panel_review.v1',
    run_root: '/tmp/run',
    run_id: 'review-run',
    active_revision_id: activeRevisionId,
    revision_lineage: [],
    identity_reference: null,
    continuity_reference: null,
    panels: [],
    boundary: {},
  }
}

test('selection made in the active revision is not stale', () => {
  assert.equal(isSelectionStale(review('rev_a'), 'rev_a'), false)
  assert.equal(isSelectionStale(review('rev_a'), null), false)
})

test('revision change makes the old selection stale', () => {
  assert.equal(isSelectionStale(review('rev_b'), 'rev_a'), true)
})

test('plan counts are read verbatim from the backend plan body', () => {
  assert.deepEqual(
    planCountsFrom({ classification_counts: { regenerate: 3, revalidate: 3, stale: 2, reuse: 3 } }),
    { regenerate: 3, revalidate: 3, stale: 2, reuse: 3 },
  )
})

test('missing backend counts are never guessed', () => {
  assert.equal(planCountsFrom(null), null)
  assert.equal(planCountsFrom({ classification_counts: { regenerate: 3 } }), null)
  assert.equal(planCountsFrom({}), null)
})

test('edit intent targets one explicit hash-bound frame at planning_only', () => {
  const intent = buildEditIntent({
    runId: 'review-run',
    revisionId: 'rev_a',
    panelId: 'sb_001',
    artifactRole: 'start_frame',
    expectedSha256: 'sha256:' + 'a'.repeat(64),
    description: 'tighten the jawline back to the identity source',
    constraints: ['identity_source_unchanged', 'no_provider_submission'],
    intentId: 'ux-1',
  })
  assert.equal(intent.execution_boundary, 'planning_only')
  assert.deepEqual(intent.target, {
    run_id: 'review-run',
    revision_id: 'rev_a',
    panel_id: 'sb_001',
    artifact_role: 'start_frame',
    expected_sha256: 'sha256:' + 'a'.repeat(64),
  })
  assert.equal((intent.requested_change as Record<string, unknown>).kind, 'replace_panel_frame')
})
