/** PD-UX-04 panel review + edit-intent planning back end (view/command surface).
 *
 * Read side: projects the per-revision `panel_review_state.json` sidecar onto
 * the canonical revision evidence. Candidate hashes are re-verified against
 * disk on every read, and the accepted candidate must match the frozen
 * revision artifact index — the sidecar is never trusted on its own.
 *
 * Command side: submits a typed edit intent to the PD-UX-03 planner
 * (scripts/compile_edit_intent_plan.py, planning_only boundary). The plan and
 * its receipt land in a temp output dir; the frozen run root is never mutated.
 * Counts shown by the UI come from this backend response, never the browser.
 *
 * No provider calls, no paid submissions, no persona-memory writes, no
 * successor activation. Selecting is not approving; planning is not executing.
 */
import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'

import { sha256File } from './assetCatalog'

export const PANEL_REVIEW_SCHEMA = 'persona_dream.ux_panel_review.v1'
export const EDIT_INTENT_PLAN_SCHEMA = 'persona_dream.ux_edit_intent_plan_result.v1'
export const REVIEW_STATUSES = ['draft', 'candidate', 'accepted', 'rejected'] as const
export const RENDER_STATUSES = ['rendered', 'not_rendered'] as const
export const PRESERVATION_CONSTRAINTS = [
  'identity_source_unchanged',
  'untouched_panels_reused_by_hash',
  'no_provider_submission',
  'historical_receipts_preserved',
  'target_only_no_downstream_propagation',
] as const

export type PanelReviewCandidate = {
  candidate_id: string
  artifact_role: string
  sha256: string
  media_path: string | null
  review_status: (typeof REVIEW_STATUSES)[number] | 'invalid'
  render_status: (typeof RENDER_STATUSES)[number]
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

export type PanelReviewProjection = {
  schema: typeof PANEL_REVIEW_SCHEMA
  live: boolean
  mocked: false
  run_root: string
  run_id: string
  active_revision_id: string
  revision_lineage: { revision_id: string; status: string }[]
  identity_reference: Record<string, unknown> | null
  continuity_reference: Record<string, unknown> | null
  panels: PanelReviewPanel[]
  boundary: {
    read_only: true
    provider_calls: 0
    paid_submissions: 0
    memory_writes: 0
    selected_is_not_approved: true
    rendered_is_not_accepted: true
  }
}

export type PanelReviewOptions = {
  /** Frozen run roots (each holding .persona-dream state) the inspector may read/plan against. */
  runRoots: readonly string[]
  /** Skill root: where scripts/compile_edit_intent_plan.py lives. */
  skillRoot: string
}

export class PanelReviewError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    readonly details: Record<string, unknown> = {},
  ) {
    super(code)
  }
}

function readJson(path: string): any {
  try {
    return JSON.parse(readFileSync(path, 'utf-8'))
  } catch (error) {
    throw new PanelReviewError('PANEL_REVIEW_READ_FAILED', 500, { path, error: String(error) })
  }
}

function sha256Of(path: string): string {
  return 'sha256:' + sha256File(path)
}

/** Read the panel-review projection for the active revision of one run root. */
export function readPanelReview(runRoot: string): PanelReviewProjection {
  const pointer = readJson(join(runRoot, '.persona-dream', 'state', 'active_revision.json'))
  const activeRevisionId = String(pointer.revisionId ?? '')
  if (!activeRevisionId) throw new PanelReviewError('ACTIVE_REVISION_MISSING', 404, { runRoot })

  const statePath = join(runRoot, '.persona-dream', 'review', activeRevisionId, 'panel_review_state.json')
  const state = readJson(statePath)
  if (state.schema !== 'persona_dream.panel_review_state.v1') {
    throw new PanelReviewError('PANEL_REVIEW_STATE_SCHEMA', 409, { observed: state.schema })
  }
  if (state.revision_id !== activeRevisionId) {
    throw new PanelReviewError('PANEL_REVIEW_STATE_STALE', 409, {
      state_revision: state.revision_id,
      active_revision: activeRevisionId,
    })
  }

  const revisionRoot = join(runRoot, '.persona-dream', 'revisions', activeRevisionId)
  const index = readJson(join(revisionRoot, 'revision_artifact_index.json'))
  const artifacts: Record<string, { sha256: string; relative_path: string }> = index.artifacts

  const panels: PanelReviewPanel[] = (state.panels ?? []).map((panel: any) => {
    const acceptedArtifact = artifacts[`${panel.panel_id}.${panel.accepted.artifact_role}`]
    const acceptedPath = join(revisionRoot, panel.accepted.artifact_path)
    const indexVerified = !!acceptedArtifact && acceptedArtifact.sha256 === panel.accepted.sha256
      && sha256Of(acceptedPath) === panel.accepted.sha256
    const candidates: PanelReviewCandidate[] = (panel.candidates ?? []).map((candidate: any) => {
      let hashVerified = false
      let verifyNote = 'hash not checked'
      if (candidate.media_path) {
        const mediaPath = isAbsolute(candidate.media_path) ? candidate.media_path : join(runRoot, candidate.media_path)
        try {
          hashVerified = sha256Of(mediaPath) === candidate.sha256
          verifyNote = hashVerified ? 'verified against disk' : 'sha256 mismatch against disk'
        } catch {
          verifyNote = 'media file missing'
        }
      } else {
        verifyNote = 'accepted-frame artifact (verified via revision index)'
        hashVerified = indexVerified && candidate.candidate_id === panel.accepted.candidate_id
      }
      const reviewStatus = REVIEW_STATUSES.includes(candidate.review_status) ? candidate.review_status : 'invalid'
      return {
        candidate_id: candidate.candidate_id,
        artifact_role: candidate.artifact_role,
        sha256: candidate.sha256,
        media_path: candidate.media_path,
        review_status: reviewStatus,
        render_status: RENDER_STATUSES.includes(candidate.render_status) ? candidate.render_status : 'not_rendered',
        findings: Array.isArray(candidate.findings) ? candidate.findings : [],
        receipt_path: candidate.receipt_path ?? null,
        hash_verified: hashVerified,
        verify_note: verifyNote,
      }
    })
    return {
      panel_id: panel.panel_id,
      accepted: {
        candidate_id: panel.accepted.candidate_id,
        artifact_role: panel.accepted.artifact_role,
        sha256: panel.accepted.sha256,
        artifact_path: panel.accepted.artifact_path,
        receipt_path: panel.accepted.receipt_path ?? null,
        index_verified: indexVerified,
      },
      candidates,
    }
  })

  return {
    schema: PANEL_REVIEW_SCHEMA,
    live: true,
    mocked: false,
    run_root: runRoot,
    run_id: state.run_id,
    active_revision_id: activeRevisionId,
    revision_lineage: state.revision_lineage ?? [],
    identity_reference: state.identity_reference ?? null,
    continuity_reference: state.continuity_reference ?? null,
    panels,
    boundary: {
      read_only: true,
      provider_calls: 0,
      paid_submissions: 0,
      memory_writes: 0,
      selected_is_not_approved: true,
      rendered_is_not_accepted: true,
    },
  }
}

export type EditIntentPlanResult = {
  schema: typeof EDIT_INTENT_PLAN_SCHEMA
  blocked: boolean
  code: 'PASS_EDIT_INTENT_PLANNED' | string
  plan: Record<string, unknown> | null
  receipt: Record<string, unknown> | null
  details: Record<string, unknown>
  boundary: Record<string, unknown>
  planner: { command: string; exit_code: number }
}

/** Submit a typed edit intent to the PD-UX-03 planner (planning_only).
 *
 * The frozen run root is read-only here: the intent and the plan output land
 * in a temp dir that is removed afterwards. Everything the UI displays
 * (classification counts, boundary, lineage) is parsed from the planner's own
 * output — the caller must not recompute any of it. */
export function planEditIntent(options: {
  skillRoot: string
  runRoot: string
  intent: Record<string, unknown>
}): EditIntentPlanResult {
  const work = mkdtempSync(join(tmpdir(), 'pd-ux-04-plan-'))
  try {
    const intentPath = join(work, 'intent.json')
    writeFileSync(intentPath, JSON.stringify(options.intent, null, 2) + '\n')
    const command = `python3 ${JSON.stringify(join(options.skillRoot, 'scripts', 'compile_edit_intent_plan.py'))}`
    const result = spawnSyncJson(
      'python3',
      [
        join(options.skillRoot, 'scripts', 'compile_edit_intent_plan.py'),
        '--intent', intentPath,
        '--run-root', options.runRoot,
        '--output-dir', join(work, 'plan'),
        '--json',
      ],
      { cwd: options.skillRoot },
    )
    return {
      schema: EDIT_INTENT_PLAN_SCHEMA,
      blocked: result.exitCode !== 0,
      code: result.payload?.code ?? (result.exitCode === 0 ? result.payload?.status ?? 'PASS_EDIT_INTENT_PLANNED' : 'PLANNER_FAILED'),
      plan: result.exitCode === 0 ? result.plan ?? null : null,
      receipt: result.exitCode === 0 ? result.payload ?? null : null,
      details: result.exitCode === 0 ? {} : (result.payload?.details ?? {}),
      boundary: result.payload?.boundary ?? { execution: 'planning_only', provider_call_attempts: 0, paid_submissions: 0, memory_writes: 0, successor_activation: false },
      planner: { command, exit_code: result.exitCode },
    }
  } finally {
    rmSync(work, { recursive: true, force: true })
  }
}

function spawnSyncJson(command: string, args: string[], options: { cwd: string }) {
  // spawnSync keeps this deterministic and bounded; no shell, no env-derived behavior.
  const run = spawnSync(command, args, { ...options, encoding: 'utf-8', timeout: 60_000 })
  let payload: any = null
  try {
    payload = JSON.parse(run.stdout || 'null')
  } catch {
    payload = null
  }
  let plan: any = null
  // On success the CLI prints the receipt; the plan body sits next to it.
  if (run.status === 0 && payload) {
    try {
      const planPath = resolve(options.cwd, args[args.indexOf('--output-dir') + 1], 'edit_intent_plan.json')
      plan = JSON.parse(readFileSync(planPath, 'utf-8'))
    } catch {
      plan = null
    }
  }
  const stderr = String(run.stderr ?? '')
  if (run.status !== 0 && !payload) {
    throw new PanelReviewError('PLANNER_INVOCATION_FAILED', 500, {
      exit_code: run.status,
      stderr: stderr.slice(0, 2000),
    })
  }
  return { exitCode: run.status ?? -1, payload, plan, stderr }
}

/** Deterministic digest of the fixture tree, used to prove planning never
 * mutated the frozen run root. */
export function digestTree(root: string): string {
  const hash = createHash('sha256')
  walk(root)
  return hash.digest('hex')
  function walk(dir: string) {
    // imported lazily to keep the module surface small
    for (const name of readdirSync(dir).sort()) {
      const path = join(dir, name)
      const stat = statSync(path)
      if (stat.isDirectory()) walk(path)
      else hash.update(path.slice(root.length)).update(sha256File(path))
    }
  }
}

// --- PD-UX-05: apply a validated edit plan via the shared CLI path -------------

export const EDIT_PLAN_EXECUTION_SCHEMA = 'persona_dream.ux_edit_plan_execution.v1'

export type EditPlanExecutionResult = {
  schema: typeof EDIT_PLAN_EXECUTION_SCHEMA
  blocked: boolean
  code: string
  result: Record<string, unknown> | null
  executor: { command: string; exit_code: number }
}

/** Hand an approved edit-intent plan to the sanctioned revision/Tau workflow.
 *
 * Single shared validation/execution path: this spawns the same
 * scripts/execute_edit_intent_plan.py the CLI runs; the server never
 * re-implements authorization, reconciliation, or handoff rules. The
 * authorization record must live inside the run root; a changed plan never
 * inherits approval (the CLI enforces the hash binding and blocks). */
export function applyEditPlan(options: {
  skillRoot: string
  runRoot: string
  planPath: string
  authorizationPath: string
}): EditPlanExecutionResult {
  const runRoot = resolve(options.runRoot)
  const authReal = resolve(options.authorizationPath)
  if (!(authReal === runRoot || authReal.startsWith(runRoot + '/'))) {
    throw new PanelReviewError('authorization_outside_run_root', 403, { authorization: options.authorizationPath })
  }
  const command = `python3 ${JSON.stringify(join(options.skillRoot, 'scripts', 'execute_edit_intent_plan.py'))}`
  const run = spawnSync(
    'python3',
    [
      join(options.skillRoot, 'scripts', 'execute_edit_intent_plan.py'),
      '--run-root', runRoot,
      '--plan', resolve(options.planPath),
      '--authorization', authReal,
    ],
    { cwd: options.skillRoot, encoding: 'utf-8', timeout: 60_000 },
  )
  let payload: any = null
  try {
    payload = JSON.parse(run.stdout || 'null')
  } catch {
    payload = null
  }
  if (!payload) {
    throw new PanelReviewError('EXECUTOR_INVOCATION_FAILED', 500, {
      exit_code: run.status,
      stderr: String(run.stderr ?? '').slice(0, 2000),
    })
  }
  return {
    schema: EDIT_PLAN_EXECUTION_SCHEMA,
    blocked: payload.status === 'BLOCKED',
    code: payload.status,
    result: payload,
    executor: { command, exit_code: run.status ?? -1 },
  }
}

/** Read-only job status for an edit-plan execution (same CLI, --status). */
export function editPlanJobStatus(options: { skillRoot: string; runRoot: string; planId: string }): Record<string, unknown> {
  const runRoot = resolve(options.runRoot)
  const run = spawnSync(
    'python3',
    [
      join(options.skillRoot, 'scripts', 'execute_edit_intent_plan.py'),
      '--run-root', runRoot,
      '--status',
      '--plan-id', options.planId,
    ],
    { cwd: options.skillRoot, encoding: 'utf-8', timeout: 30_000 },
  )
  let payload: any = null
  try {
    payload = JSON.parse(run.stdout || 'null')
  } catch {
    payload = null
  }
  if (!payload) {
    throw new PanelReviewError('STATUS_INVOCATION_FAILED', 500, {
      exit_code: run.status,
      stderr: String(run.stderr ?? '').slice(0, 2000),
    })
  }
  return payload
}
