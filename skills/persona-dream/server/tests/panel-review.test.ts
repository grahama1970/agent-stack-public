/** PD-UX-04 tests: panel-review projection + edit-intent planner endpoint.
 *
 * Proves: the projection re-verifies candidate/accepted hashes against disk
 * and the frozen revision index; the planner returns the golden backend
 * counts (3 regenerate / 3 revalidate / 2 stale / 3 reuse); blocked intents
 * carry typed codes; planning never mutates the frozen run root. */
import assert from 'node:assert/strict'
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { digestTree, planEditIntent, readPanelReview } from '../src/panelReview'

const SKILL_ROOT = resolve(import.meta.dirname, '..', '..')
const RUN_ROOT = join(SKILL_ROOT, 'fixtures', 'pd-ux-04', 'review-run')
const GOLDEN_INTENT = JSON.parse(readFileSync(join(SKILL_ROOT, 'fixtures', 'pd-ux-03', 'golden.edit-intent.json'), 'utf-8'))

function plannerOptions(): { skillRoot: string; runRoot: string } {
  return { skillRoot: SKILL_ROOT, runRoot: RUN_ROOT }
}

test('projection: active revision, lineage, identity/continuity references, boundary', () => {
  const review = readPanelReview(RUN_ROOT)
  assert.equal(review.schema, 'persona_dream.ux_panel_review.v1')
  assert.equal(review.active_revision_id, 'rev_golden_pd_ux_03')
  assert.deepEqual(review.revision_lineage.map((r) => r.revision_id), ['rev_golden_pd_ux_03', 'rev_pd_ux_04_b'])
  assert.equal((review.identity_reference as Record<string, unknown>).canonical_id, 'embry_contact_sheet_v3')
  assert.equal((review.continuity_reference as Record<string, unknown>).kind, 'session_arc_bias')
  assert.equal(review.boundary.read_only, true)
  assert.equal(review.boundary.provider_calls, 0)
  assert.equal(review.boundary.memory_writes, 0)
})

test('projection: accepted candidate verified against the frozen revision index + disk', () => {
  const review = readPanelReview(RUN_ROOT)
  const panel = review.panels.find((p) => p.panel_id === 'sb_001')!
  assert.ok(panel)
  assert.equal(panel.accepted.candidate_id, 'sb_001_start_c1')
  assert.equal(panel.accepted.index_verified, true)
  const accepted = panel.candidates.find((c) => c.candidate_id === 'sb_001_start_c1')!
  assert.equal(accepted.review_status, 'accepted')
  assert.equal(accepted.hash_verified, true)
})

test('projection: review status is separate from render status; rejected stays distinct', () => {
  const review = readPanelReview(RUN_ROOT)
  const candidates = review.panels[0].candidates
  const byId = Object.fromEntries(candidates.map((c) => [c.candidate_id, c]))
  assert.equal(byId.sb_001_start_c2.review_status, 'rejected')
  assert.equal(byId.sb_001_start_c2.render_status, 'rendered')
  assert.equal(byId.sb_001_start_c3.review_status, 'draft')
  assert.equal(byId.sb_001_start_c3.render_status, 'not_rendered')
  assert.equal(byId.sb_001_start_c3.media_path, null) // draft: never rendered, no media to trust
  assert.equal(byId.sb_001_start_c3.hash_verified, false)
  assert.equal(byId.sb_001_start_c4.review_status, 'candidate')
  assert.ok(byId.sb_001_start_c2.findings.length > 0, 'rejected candidate carries reviewer findings')
})

test('projection: tampered candidate media is surfaced, not trusted', () => {
  const copy = mkdtempSync(join(tmpdir(), 'pd-ux-04-tamper-'))
  try {
    cpSync(RUN_ROOT, copy, { recursive: true })
    const media = join(copy, '.persona-dream/review/rev_golden_pd_ux_03/candidates/sb_001_start_c2.png')
    writeFileSync(media, Buffer.from('tampered bytes'))
    const review = readPanelReview(copy)
    const candidate = review.panels[0].candidates.find((c) => c.candidate_id === 'sb_001_start_c2')!
    assert.equal(candidate.hash_verified, false)
    assert.match(candidate.verify_note, /mismatch|missing/)
  } finally {
    rmSync(copy, { recursive: true, force: true })
  }
})

test('planner: golden intent returns backend-derived counts and planning-only boundary', () => {
  const before = digestTree(RUN_ROOT)
  const result = planEditIntent({ ...plannerOptions(), intent: GOLDEN_INTENT })
  const after = digestTree(RUN_ROOT)
  assert.equal(result.blocked, false)
  assert.equal(result.code, 'PASS_EDIT_INTENT_PLANNED')
  assert.equal(result.plan!.classification_counts.regenerate, 3)
  assert.equal(result.plan!.classification_counts.revalidate, 3)
  assert.equal(result.plan!.classification_counts.stale, 2)
  assert.equal(result.plan!.classification_counts.reuse, 3)
  assert.equal(result.plan!.boundary.execution, 'planning_only')
  assert.equal(result.plan!.boundary.provider_call_attempts, 0)
  assert.equal(result.plan!.boundary.memory_writes, 0)
  assert.equal(result.plan!.lineage.successor_revision_id, null)
  assert.equal(result.plan!.plan_id, 'editplan_427f35e6ec5a')
  assert.equal(before, after, 'frozen run root must be unchanged by planning')
})

test('planner: stale target revision is blocked with the typed code', () => {
  const intent = structuredClone(GOLDEN_INTENT)
  intent.target.revision_id = 'rev_pd_ux_04_b' // exists but is NOT the active revision
  const result = planEditIntent({ ...plannerOptions(), intent })
  assert.equal(result.blocked, true)
  assert.equal(result.code, 'BLOCKED_TARGET_REVISION_NOT_ACTIVE')
  assert.equal((result.details as Record<string, unknown>).active_revision_id, 'rev_golden_pd_ux_03')
})

test('planner: unknown preservation constraint is blocked', () => {
  const intent = structuredClone(GOLDEN_INTENT)
  intent.preservation_constraints = ['do_whatever_i_want']
  const result = planEditIntent({ ...plannerOptions(), intent })
  assert.equal(result.blocked, true)
  assert.equal(result.code, 'BLOCKED_UNKNOWN_PRESERVATION_CONSTRAINT')
})

test('planner: out-of-scope identity change is blocked', () => {
  const intent = structuredClone(GOLDEN_INTENT)
  intent.requested_change = { kind: 'identity_source_change', description: 'swap the identity source' }
  const result = planEditIntent({ ...plannerOptions(), intent })
  assert.equal(result.blocked, true)
  assert.equal(result.code, 'BLOCKED_EDIT_OUT_OF_SCOPE_IDENTITY')
})
