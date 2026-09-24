/** PD-UX-06 integration tests: the retained operator scenario through the real
 * backend surface (asset catalog -> draft binding -> panel review -> edit-intent
 * plan -> execution handoff -> read-only status), plus the negative set.
 *
 * Every request goes through createPersonaDreamRouter on a live loopback
 * listener — the same code path the browser hits. No provider calls, no paid
 * submissions, no persona-memory writes; the run root is a tmp copy of the
 * PD-UX-04 fixture, the committed fixtures are never mutated. */
import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cpSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, relative, resolve } from 'node:path'
import test from 'node:test'

import express from 'express'

import { createPersonaDreamRouter } from '../src/index'

const SKILL_ROOT = resolve(import.meta.dirname, '..', '..')
const FIXTURE_RUN = join(SKILL_ROOT, 'fixtures', 'pd-ux-04', 'review-run')
const FIXTURE_MANIFEST = join(SKILL_ROOT, 'fixtures', 'pd-ux-01', 'canonical_asset_manifest.json')
const GRAPH = join(SKILL_ROOT, 'contracts', 'persona_dream_revision_dependency_graph.v1.json')

function sha256Hex(path: string): string {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(sort(value))
  function sort(v: any): any {
    if (Array.isArray(v)) return v.map(sort)
    if (v && typeof v === 'object') return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sort(v[k])]))
    return v
  }
}

function digestTree(root: string): string {
  const hash = createHash('sha256')
  walk(root)
  return hash.digest('hex')
  function walk(dir: string) {
    for (const name of readdirSync(dir).sort()) {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) walk(path)
      else hash.update(path.slice(root.length)).update(sha256Hex(path))
    }
  }
}

/** A tmp run root + compiled plan + matching authorization inside the run root. */
async function makeRig(scene: string, extra: { manifests?: string[]; assetRoots?: string[] } = {}) {
  const work = mkdtempSync(join(tmpdir(), `pd-ux-06-${scene}-`))
  const runRoot = join(work, 'run')
  cpSync(FIXTURE_RUN, runRoot, { recursive: true })
  const draftRoot = join(work, 'drafts')
  const app = express()
  app.use(express.json({ limit: '4mb' }))
  app.use('/api/projects/dream', createPersonaDreamRouter({
    reportRoots: [runRoot],
    outputRoots: [],
    assetRoots: ['/mnt/storage12tb/media/personas/embry', join(SKILL_ROOT, '..'), ...(extra.assetRoots ?? [])],
    assetLibrary: { manifests: [FIXTURE_MANIFEST, ...(extra.manifests ?? [])], draftRoot, pathBase: resolve(SKILL_ROOT, '..', '..') },
    panelReview: { runRoots: [runRoot], skillRoot: SKILL_ROOT },
  }))
  const server = app.listen(0, '127.0.0.1')
  await new Promise<void>((done) => server.once('listening', done))
  const base = `http://127.0.0.1:${(server.address() as { port: number }).port}/api/projects/dream`

  const planDir = join(runRoot, '.persona-dream', 'e2e', 'plan')
  writeFileSync(join(runRoot, '.persona-dream', 'e2e-marker.json'), '{}\n')
  const compiled = spawnSync('python3', [
    join(SKILL_ROOT, 'scripts', 'compile_edit_intent_plan.py'),
    '--intent', join(SKILL_ROOT, 'fixtures', 'pd-ux-03', 'golden.edit-intent.json'),
    '--run-root', runRoot, '--output-dir', planDir, '--json',
  ], { encoding: 'utf-8' })
  assert.equal(compiled.status, 0, compiled.stderr)
  const plan = JSON.parse(readFileSync(join(planDir, 'edit_intent_plan.json'), 'utf-8'))
  const authPath = join(runRoot, 'edit_plan_authorization.json')
  const writeAuthorization = (overrides: Record<string, string> = {}) => {
    const revisionRoot = join(runRoot, '.persona-dream', 'revisions', plan.target.revision_id)
    writeFileSync(authPath, JSON.stringify({
      schema: 'persona_dream.edit_plan_authorization.v1',
      operation: 'execute_edit_intent_plan',
      approved_by: 'human-test-approver',
      plan_id: plan.plan_id,
      plan_sha256: 'sha256:' + sha256Hex(join(planDir, 'edit_intent_plan.json')),
      payload_sha256: 'sha256:' + createHash('sha256').update(canonicalJson({ ...plan, plan_id: undefined })).digest('hex'),
      revision_index_sha256: 'sha256:' + sha256Hex(join(revisionRoot, 'revision_artifact_index.json')),
      dependency_graph_sha256: 'sha256:' + sha256Hex(GRAPH),
      target_artifact_id: plan.target.artifact_id,
      target_expected_sha256: plan.target.expected_sha256,
      ...overrides,
    }, null, 2) + '\n')
  }
  writeAuthorization()
  const close = () => { (server as any).closeAllConnections?.(); server.close() }
  return { work, runRoot, planDir, plan, authPath, writeAuthorization, base, close }
}

const QUALIFIED = {
  manifest: FIXTURE_MANIFEST,
  persona: 'embry',
  project: 'persona-dream',
  duty: 'identity',
  canonical_id: 'embry_contact_sheet_v3',
  version_content_hash: '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c',
}

test.after(() => { /* tmpdirs are left for the OS; nothing under the repo is written */ })

test('scenario: bind qualified reference to two scenes (reuse), plan, hand off, verify state independently', async () => {
  const rig = await makeRig('scenario')
  const revisionBefore = digestTree(join(rig.runRoot, '.persona-dream', 'revisions'))

  // 1. locate qualified reference: catalog projection is read-only and verifies it.
  const catalog = await fetch(`${rig.base}/asset-catalog?manifest=${encodeURIComponent(FIXTURE_MANIFEST)}`).then((r) => r.json())
  const entry = catalog.entries.find((e: any) => e.canonical_id === 'embry_contact_sheet_v3')
  assert.equal(catalog.read_only, true)
  assert.equal(entry.state, 'qualified')
  assert.equal(catalog.provider_calls, 0)

  // 2+3. bind the same qualified asset to two scenes — DRAFT only, reused not duplicated.
  for (const scene of ['scene-a', 'scene-b']) {
    const bound = await fetch(`${rig.base}/asset-catalog/draft-binding`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...QUALIFIED, scene_id: scene }),
    }).then((r) => r.json())
    assert.equal(bound.binding.status, 'DRAFT_BOUND_NOT_APPROVED_NOT_ACCEPTED')
    assert.equal(bound.binding.canonical_id, 'embry_contact_sheet_v3')
    assert.equal(bound.binding.duty, 'identity')
  }
  const bindings = await fetch(`${rig.base}/asset-catalog/draft-bindings`).then((r) => r.json())
  assert.equal(bindings.bindings.length, 2)
  assert.equal(new Set(bindings.bindings.map((b: any) => b.compiler_input)).size, 1, 'same canonical media reused, not duplicated')

  // 4. panel review projection: accepted vs candidates, rejected stays rejected.
  const review = await fetch(`${rig.base}/panel-review?run_root=${encodeURIComponent(rig.runRoot)}`).then((r) => r.json())
  const sb001 = review.panels.find((p: any) => p.panel_id === 'sb_001')
  assert.equal(sb001.accepted.candidate_id, 'sb_001_start_c1')
  assert.equal(sb001.accepted.index_verified, true)
  assert.equal(sb001.candidates.find((c: any) => c.candidate_id === 'sb_001_start_c2').review_status, 'rejected')

  // 5. plan via the UI surface: planning_only boundary, backend counts.
  const planned = await fetch(`${rig.base}/edit-intent-plan`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_root: rig.runRoot,
      intent: {
        schema: 'persona_dream.edit_intent.v1',
        intent_id: 'ux06-scenario',
        target: {
          run_id: review.run_id, revision_id: review.active_revision_id, panel_id: 'sb_001',
          artifact_role: sb001.accepted.artifact_role, expected_sha256: sb001.accepted.sha256,
        },
        requested_change: { kind: 'replace_panel_frame', description: 'integration scenario bounded edit' },
        preservation_constraints: ['identity_source_unchanged', 'no_provider_submission'],
        execution_boundary: 'planning_only',
      },
    }),
  }).then((r) => r.json())
  assert.equal(planned.blocked, false)
  assert.equal(planned.code, 'PASS_EDIT_INTENT_PLANNED')
  assert.equal(planned.plan.boundary.execution, 'planning_only')

  // 6. execution handoff via the UI surface: shared CLI, zero provider calls.
  const executed = await fetch(`${rig.base}/edit-plan-execution`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: rig.runRoot, plan: rig.planDir, authorization: rig.authPath }),
  })
  assert.equal(executed.status, 200)
  const payload = await executed.json()
  assert.equal(payload.code, 'PASS_EDIT_PLAN_HANDED_OFF')
  assert.equal(payload.result.state, 'HANDED_OFF')
  assert.equal(payload.result.actual_provider_call_attempts, 0)

  // Duplicate execution through the same surface is idempotent, never a second handoff.
  const duplicate = await fetch(`${rig.base}/edit-plan-execution`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: rig.runRoot, plan: rig.planDir, authorization: rig.authPath }),
  }).then((r) => r.json())
  assert.equal(duplicate.result.reconciled, true)
  assert.equal((readdirSync(join(rig.runRoot, '.persona-dream', 'repair', 'work-orders'))).length, 1)

  // 7. independent readback: read-only status endpoint + frozen revision digest.
  const status = await fetch(`${rig.base}/edit-plan-execution?run_root=${encodeURIComponent(rig.runRoot)}&plan_id=${rig.plan.plan_id}`).then((r) => r.json())
  assert.equal(status.read_only, true)
  assert.equal(status.state, 'HANDED_OFF')
  assert.equal(digestTree(join(rig.runRoot, '.persona-dream', 'revisions')), revisionBefore, 'frozen revision unchanged')

  rig.close()
})

test('negative: stale selection / wrong scope / conflicting duty / missing media / invalid qualification are typed refusals, never accepted states', async () => {
  const negDir = mkdtempSync(join(tmpdir(), 'pd-ux-06-neg-'))
  const manifestData = JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf-8'))
  manifestData.assets[0].qualification_evidence[0].sha256 = '0'.repeat(64)
  const badManifest = join(negDir, 'canonical_asset_manifest_badq.json')
  writeFileSync(badManifest, JSON.stringify(manifestData, null, 2) + '\n')
  const goneData = JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf-8'))
  goneData.assets[0].provenance.image_path = '/nonexistent/embry_contact_sheet_v3.png'
  goneData.assets[0].image_path = '/nonexistent/embry_contact_sheet_v3.png'
  const goneManifest = join(negDir, 'canonical_asset_manifest_gone.json')
  writeFileSync(goneManifest, JSON.stringify(goneData, null, 2) + '\n')
  const rig = await makeRig('negatives', { manifests: [badManifest, goneManifest], assetRoots: [negDir] })
  const refusal = async (body: Record<string, unknown>) => {
    const response = await fetch(`${rig.base}/asset-catalog/draft-binding`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    const payload = await response.json()
    assert.equal(response.status, 409, `expected typed refusal for ${JSON.stringify(body)}`)
    assert.match(payload.error, /^[A-Z_]+$/)
    return payload.error as string
  }

  // stale selection: catalog version changed underneath
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', version_content_hash: '0'.repeat(64) }), 'STALE_ASSET_HASH')
  // wrong scope: persona + project
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', persona: 'horus' }), 'WRONG_ASSET_SCOPE')
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', project: 'other-project' }), 'WRONG_ASSET_SCOPE')
  // conflicting duty: the asset is not qualified for this duty
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', duty: 'continuity' }), 'DUTY_NOT_QUALIFIED')
  // invalid qualification: manifest entry whose qualification receipt hash does not match
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', manifest: badManifest }), 'QUALIFICATION_RECEIPT_INVALID')
  // missing media: manifest entry whose retained media file is absent
  assert.equal(await refusal({ ...QUALIFIED, scene_id: 's', manifest: goneManifest }), 'MEDIA_HASH_MISMATCH')

  // no draft bindings escaped the refusals
  const bindings = await fetch(`${rig.base}/asset-catalog/draft-bindings`).then((r) => r.json())
  assert.equal(bindings.bindings.length, 0)
  rig.close()
})

test('negative: unavailable generation boundary is refused by the planner, not deferred to execution', async () => {
  const rig = await makeRig('boundary')
  const review = await fetch(`${rig.base}/panel-review?run_root=${encodeURIComponent(rig.runRoot)}`).then((r) => r.json())
  const blocked = await fetch(`${rig.base}/edit-intent-plan`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_root: rig.runRoot,
      intent: {
        schema: 'persona_dream.edit_intent.v1',
        intent_id: 'ux06-generate-now',
        target: {
          run_id: review.run_id, revision_id: review.active_revision_id, panel_id: 'sb_001',
          artifact_role: 'start_frame', expected_sha256: review.panels[0].accepted.sha256,
        },
        requested_change: { kind: 'replace_panel_frame', description: 'attempt to generate immediately' },
        preservation_constraints: ['identity_source_unchanged'],
        execution_boundary: 'generate_and_submit',
      },
    }),
  }).then((r) => r.json())
  assert.equal(blocked.blocked, true)
  assert.equal(blocked.code, 'BLOCKED_EXECUTION_BOUNDARY')
  assert.equal(blocked.boundary.provider_call_attempts, 0)
  rig.close()
})

test('negative: changed plan never inherits approval; duplicate interrupted execution reconciles, never blind-retries', async () => {
  const rig = await makeRig('drift')
  // Execute once, then record a simulated TIMEOUT (interrupted session) via the CLI.
  const first = await fetch(`${rig.base}/edit-plan-execution`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: rig.runRoot, plan: rig.planDir, authorization: rig.authPath }),
  }).then((r) => r.json())
  assert.equal(first.result.state, 'HANDED_OFF')
  const recorded = execFileSync('python3', [
    join(SKILL_ROOT, 'scripts', 'execute_edit_intent_plan.py'),
    '--run-root', rig.runRoot, '--record-outcome', 'TIMEOUT', '--plan-id', rig.plan.plan_id,
  ], { encoding: 'utf-8' })
  assert.equal(JSON.parse(recorded).state, 'AMBIGUOUS')
  // Retry through the UI surface: timeout is not proof no job exists — reconcile.
  const retried = await fetch(`${rig.base}/edit-plan-execution`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: rig.runRoot, plan: rig.planDir, authorization: rig.authPath }),
  }).then((r) => r.json())
  assert.equal(retried.code, 'PASS_EDIT_PLAN_HANDED_OFF')
  assert.equal(retried.result.reconciled, true)
  assert.equal((readdirSync(join(rig.runRoot, '.persona-dream', 'repair', 'work-orders'))).length, 1)

  // Changed plan (drifted target hash) with the OLD authorization: blocked, no handoff, no false accepted state.
  rig.writeAuthorization({ target_expected_sha256: 'sha256:' + '0'.repeat(64) })
  const drifted = await fetch(`${rig.base}/edit-plan-execution`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_root: rig.runRoot, plan: rig.planDir, authorization: rig.authPath }),
  })
  assert.equal(drifted.status, 409)
  const driftedPayload = await drifted.json()
  assert.equal(driftedPayload.blocked, true)
  assert.equal(driftedPayload.result.code, 'BLOCKED_AUTHORIZATION_PLAN_CHANGED')
  assert.equal(driftedPayload.result.actual_provider_call_attempts, 0)
  rig.close()
})
