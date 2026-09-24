/** PD-UX-04 panel-review client types + pure state derivation.
 *
 * All authority comes from the backend projection
 * (persona_dream.ux_panel_review.v1) and the planner response. Nothing here
 * recomputes counts, hashes, or statuses; these helpers only classify what
 * the backend already said (stale selection, plan display readiness). */

export type PanelReviewCandidate = {
  candidate_id: string
  artifact_role: string
  sha256: string
  media_path: string | null
  review_status: 'draft' | 'candidate' | 'accepted' | 'rejected' | 'invalid'
  render_status: 'rendered' | 'not_rendered'
  findings: string[]
  receipt_path: string | null
  hash_verified: boolean
  verify_note: string
}

export type PanelReviewPanel = {
  panel_id: string
  accepted: {
    candidate_id: string
    artifact_role: string
    sha256: string
    artifact_path: string
    receipt_path: string | null
    index_verified: boolean
  }
  candidates: PanelReviewCandidate[]
}

export type PanelReview = {
  schema: 'persona_dream.ux_panel_review.v1'
  run_root: string
  run_id: string
  active_revision_id: string
  revision_lineage: { revision_id: string; status: string }[]
  identity_reference: Record<string, unknown> | null
  continuity_reference: Record<string, unknown> | null
  panels: PanelReviewPanel[]
  boundary: Record<string, unknown>
}

export type PlanResult = {
  schema: 'persona_dream.ux_edit_intent_plan_result.v1'
  blocked: boolean
  code: string
  plan: Record<string, unknown> | null
  receipt: Record<string, unknown> | null
  details: Record<string, unknown>
  boundary: Record<string, unknown>
}

/** A selection is stale when the revision it was made in is no longer the
 * active revision — the backend projection decides, not a local timestamp. */
export function isSelectionStale(
  review: PanelReview,
  selectedRevisionId: string | null,
): boolean {
  return !!selectedRevisionId && selectedRevisionId !== review.active_revision_id
}

export type PlanCounts = { regenerate: number; revalidate: number; stale: number; reuse: number }

/** Counts are read verbatim from the backend plan body; returns null when the
 * backend did not supply them (the UI then shows "—" rather than guessing). */
export function planCountsFrom(plan: Record<string, unknown> | null): PlanCounts | null {
  const counts = (plan as { classification_counts?: unknown } | null)?.classification_counts
  if (!counts || typeof counts !== 'object') return null
  const c = counts as Record<string, unknown>
  const keys = ['regenerate', 'revalidate', 'stale', 'reuse'] as const
  if (!keys.every((key) => typeof c[key] === 'number')) return null
  return {
    regenerate: Number(c.regenerate),
    revalidate: Number(c.revalidate),
    stale: Number(c.stale),
    reuse: Number(c.reuse),
  }
}

/** Build the typed edit intent for the planner. The target is explicit and
 * hash-bound; the boundary is always planning_only — requesting an edit never
 * authorizes generation. */
export function buildEditIntent(input: {
  runId: string
  revisionId: string
  panelId: string
  artifactRole: string
  expectedSha256: string
  description: string
  constraints: string[]
  intentId: string
}): Record<string, unknown> {
  return {
    schema: 'persona_dream.edit_intent.v1',
    intent_id: input.intentId,
    target: {
      run_id: input.runId,
      revision_id: input.revisionId,
      panel_id: input.panelId,
      artifact_role: input.artifactRole,
      expected_sha256: input.expectedSha256,
    },
    requested_change: { kind: 'replace_panel_frame', description: input.description },
    preservation_constraints: input.constraints,
    execution_boundary: 'planning_only',
  }
}

export async function fetchPanelReview(runRoot: string): Promise<PanelReview> {
  const response = await fetch(`/api/projects/dream/panel-review?run_root=${encodeURIComponent(runRoot)}`)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error ?? `HTTP ${response.status}`)
  return payload as PanelReview
}

/** PD-UX-06 execution-handoff client call. The UI forwards paths only;
 * the server spawns the same CLI the operator runs — the browser never
 * re-implements authorization, reconciliation, or handoff rules, and a
 * blocked outcome is surfaced verbatim (HTTP 409 payload). */
export async function postEditPlanExecution(runRoot: string, planPath: string, authorizationPath: string): Promise<Record<string, unknown>> {
  const response = await fetch('/api/projects/dream/edit-plan-execution', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: runRoot, plan: planPath, authorization: authorizationPath }),
  })
  const payload = await response.json()
  // 200 and 409 both carry the executor's own verdict — return it either way.
  if (!response.ok && response.status !== 409) throw new Error(payload.error ?? `HTTP ${response.status}`)
  return payload as Record<string, unknown>
}

/** Read-only job status: the durable job record is the authority; the UI
 * renders it and never infers execution state on its own. */
export async function fetchEditPlanJobStatus(runRoot: string, planId: string): Promise<Record<string, unknown>> {
  const response = await fetch(`/api/projects/dream/edit-plan-execution?run_root=${encodeURIComponent(runRoot)}&plan_id=${encodeURIComponent(planId)}`)
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error ?? `HTTP ${response.status}`)
  return payload as Record<string, unknown>
}

export async function postEditIntentPlan(runRoot: string, intent: Record<string, unknown>): Promise<PlanResult> {
  const response = await fetch('/api/projects/dream/edit-intent-plan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: runRoot, intent }),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error ?? `HTTP ${response.status}`)
  return payload as PlanResult
}
