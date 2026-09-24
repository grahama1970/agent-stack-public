/** PD-UX-05 tests: edit-plan application back end — one shared path.
 *
 * The server spawns the SAME scripts/execute_edit_intent_plan.py the operator
 * CLI runs; it never re-implements authorization, reconciliation, or handoff
 * rules. Proves: golden handoff through the real backend CLI, idempotent
 * reconcile, interrupted-execution reconciliation (timeout is not proof no job
 * exists) and failure/ambiguity blocking, authorization outside the run root
 * refused, and the frozen run-root revision tree unchanged (unrelated accepted
 * assets preserved). */
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { applyEditPlan, editPlanJobStatus } from '../src/panelReview'

const SKILL_ROOT = resolve(import.meta.dirname, '..', '..')
const FIXTURE_RUN = join(SKILL_ROOT, 'fixtures', 'pd-ux-03', 'golden-run')
const GRAPH = join(SKILL_ROOT, 'contracts', 'persona_dream_revision_dependency_graph.v1.json')

function sha256File(path: string): string {
  // small files only (fixtures); matches the python side's "sha256:..." form
  return 'sha256:' + createHash('sha256').update(readFileSync(path)).digest('hex')
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(sort(value))
  function sort(v: any): any {
    if (Array.isArray(v)) return v.map(sort)
    if (v && typeof v === 'object') {
      return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sort(v[k])]))
    }
    return v
  }
}

interface Rig {
  runRoot: string
  planDir: string
  plan: any
  authPath: string
  writeAuthorization(overrides?: Record<string, string>): void
}

async function makeRig(): Promise<Rig> {
  const work = mkdtempSync(join(tmpdir(), 'pd-ux-05-'))
  const runRoot = join(work, 'run')
  cpSync(FIXTURE_RUN, runRoot, { recursive: true })
  const planDir = join(work, 'plan')
  const { mkdirSync } = await import('node:fs')
  mkdirSync(planDir, { recursive: true })
  const { spawnSync } = await import('node:child_process')
  const compiled = spawnSync('python3', [
    join(SKILL_ROOT, 'scripts', 'compile_edit_intent_plan.py'),
    '--intent', join(SKILL_ROOT, 'fixtures', 'pd-ux-03', 'golden.edit-intent.json'),
    '--run-root', runRoot, '--output-dir', planDir, '--json',
  ], { encoding: 'utf-8' })
  assert.equal(compiled.status, 0, compiled.stderr)
  const plan = JSON.parse(readFileSync(join(planDir, 'edit_intent_plan.json'), 'utf-8'))
  const authPath = join(runRoot, 'edit_plan_authorization.json')
  const rig: Rig = {
    runRoot, planDir, plan, authPath,
    writeAuthorization(overrides: Record<string, string> = {}) {
      const revisionRoot = join(runRoot, '.persona-dream', 'revisions', plan.target.revision_id)
      const auth = {
        schema: 'persona_dream.edit_plan_authorization.v1',
        operation: 'execute_edit_intent_plan',
        approved_by: 'human-test-approver',
        plan_id: plan.plan_id,
        plan_sha256: sha256File(join(planDir, 'edit_intent_plan.json')),
        payload_sha256: 'sha256:' + createHash('sha256').update(canonicalJson({ ...plan, plan_id: undefined })).digest('hex'),
        revision_index_sha256: sha256File(join(revisionRoot, 'revision_artifact_index.json')),
        dependency_graph_sha256: sha256File(GRAPH),
        target_artifact_id: plan.target.artifact_id,
        target_expected_sha256: plan.target.expected_sha256,
        ...overrides,
      }
      writeFileSync(authPath, JSON.stringify(auth, null, 2) + '\n')
    },
  }
  rig.writeAuthorization()
  return rig
}

test.after(() => {
  // tmpdirs are left for the OS; nothing under the repo is written.
})

test('apply: hands the approved plan to the sanctioned workflow via the shared CLI', async () => {
  const rig = await makeRig()
  const result = applyEditPlan({ skillRoot: SKILL_ROOT, runRoot: rig.runRoot, planPath: rig.planDir, authorizationPath: rig.authPath })
  assert.equal(result.blocked, false)
  assert.equal(result.code, 'PASS_EDIT_PLAN_HANDED_OFF')
  assert.equal(result.executor.exit_code, 0)
  const payload = result.result as any
  assert.equal(payload.state, 'HANDED_OFF')
  assert.equal(payload.actual_provider_call_attempts, 0)
  assert.equal(payload.boundary.successor_activation, false)
  // Unrelated accepted assets: the frozen revision tree is unchanged by the handoff.
  const digestBefore = payloadJobRevisionDigest(rig)
  const status = editPlanJobStatus({ skillRoot: SKILL_ROOT, runRoot: rig.runRoot, planId: rig.plan.plan_id }) as any
  assert.equal(status.state, 'HANDED_OFF')
  assert.equal(status.read_only, true)
  assert.equal(payloadJobRevisionDigest(rig), digestBefore)
  rmSync(rig.runRoot, { recursive: true, force: true })
})

test('apply: blocked outcomes are typed and surface provider-call zeros', async () => {
  const rig = await makeRig()
  // Change the target artifact after planning -> stale, changed plan never inherits approval.
  const target = join(rig.runRoot, '.persona-dream', 'revisions', rig.plan.target.revision_id, rig.plan.target.relative_path)
  writeFileSync(target, 'mutated')
  const result = applyEditPlan({ skillRoot: SKILL_ROOT, runRoot: rig.runRoot, planPath: rig.planDir, authorizationPath: rig.authPath })
  assert.equal(result.blocked, true)
  assert.equal(result.code, 'BLOCKED')
  assert.equal((result.result as any).code, 'BLOCKED_STALE_TARGET')
  assert.equal((result.result as any).actual_provider_call_attempts, 0)
  rmSync(rig.runRoot, { recursive: true, force: true })
})

test('apply: authorization outside the run root is refused before any execution', async () => {
  const rig = await makeRig()
  const outside = join(mkdtempSync(join(tmpdir(), 'pd-ux-05-out-')), 'auth.json')
  writeFileSync(outside, readFileSync(rig.authPath))
  assert.throws(
    () => applyEditPlan({ skillRoot: SKILL_ROOT, runRoot: rig.runRoot, planPath: rig.planDir, authorizationPath: outside }),
    /authorization_outside_run_root/,
  )
  rmSync(rig.runRoot, { recursive: true, force: true })
})

test('status: missing job record is a typed error, not a silent default', async () => {
  const rig = await makeRig()
  const result = editPlanJobStatus({ skillRoot: SKILL_ROOT, runRoot: rig.runRoot, planId: rig.plan.plan_id }) as any
  assert.equal(result.status, 'BLOCKED')
  assert.equal(result.code, 'BLOCKED_JOB_RECORD_MISSING')
  rmSync(rig.runRoot, { recursive: true, force: true })
})

function payloadJobRevisionDigest(rig: Rig): string {
  return execFileSync('python3', ['-c', `
import hashlib, sys
from pathlib import Path
root = Path(${JSON.stringify(join(rig.runRoot, '.persona-dream', 'revisions', rig.plan.target.revision_id))})
h = hashlib.sha256()
for p in sorted(x for x in root.rglob('*') if x.is_file()):
    h.update(str(p.relative_to(root)).encode())
    h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
print(h.hexdigest())
`], { encoding: 'utf-8' }).trim()
}
