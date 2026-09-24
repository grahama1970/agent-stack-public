import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

import {
  AssetCatalogError,
  listDraftBindings,
  projectCatalogFromManifest,
  readAssetCatalog,
  sha256File,
  writeDraftBinding,
  type AssetLibraryOptions,
} from '../src/assetCatalog'
import { DreamPathPolicy } from '../src/paths'

const SKILL_ROOT = resolve(new URL('.', import.meta.url).pathname, '..', '..')
const REPO_ROOT = resolve(SKILL_ROOT, '..', '..')
const FIXTURE_MANIFEST = join(SKILL_ROOT, 'fixtures', 'pd-ux-01', 'canonical_asset_manifest.json')
const RETAINED_MEDIA = '/mnt/storage12tb/media/personas/embry/assets/contact_sheets/embry-gpt-image-2-v3/images/embry_contact_sheet_v3.png'

function options(draftRoot: string): AssetLibraryOptions {
  return { manifests: [FIXTURE_MANIFEST], draftRoot, pathBase: REPO_ROOT }
}

function policy() {
  return new DreamPathPolicy([SKILL_ROOT, '/mnt/storage12tb/media/personas/embry'])
}

test('catalog projection matches the Python projection field-for-field', () => {
  const manifest = JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf-8'))
  const entries = projectCatalogFromManifest(manifest)
  assert.equal(entries.length, 1)
  const entry = entries[0]
  assert.equal(entry.canonical_id, 'embry_contact_sheet_v3')
  assert.equal(entry.version_content_hash, '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c')
  assert.equal(entry.media_kind, 'image')
  assert.deepEqual(entry.persona_scope, ['embry'])
  assert.deepEqual(entry.project_scope, ['persona-dream'])
  assert.equal(entry.lifecycle_status, 'qualified')
  assert.deepEqual(entry.reference_roles, ['identity'])
  assert.deepEqual(entry.reuse_restrictions, ['synthetic-reference-only', 'selected-is-not-approved', 'rendered-is-not-accepted'])
})

test('readAssetCatalog verifies retained media + qualification receipts, no writes', () => {
  const draftRoot = mkdtempSync(join(tmpdir(), 'pd-ux-02-catalog-'))
  try {
    const mediaHashBefore = sha256File(RETAINED_MEDIA)
    const catalog = readAssetCatalog(policy(), options(draftRoot), FIXTURE_MANIFEST)
    assert.equal(catalog.schema, 'persona_dream.ux_asset_catalog.v1')
    assert.equal(catalog.provider_calls, 0)
    assert.equal(catalog.memory_writes, 0)
    assert.equal(catalog.read_only, true)
    assert.equal(catalog.canonical_manifest_sha256, sha256File(FIXTURE_MANIFEST))
    const entry = catalog.entries[0]
    assert.equal(entry.state, 'qualified')
    assert.deepEqual(entry.reasons, [])
    assert.equal(entry.media_path, RETAINED_MEDIA)
    assert.equal(draftRootContents(draftRoot).length, 0, 'read side must not write drafts')
    assert.equal(sha256File(RETAINED_MEDIA), mediaHashBefore, 'media bytes unchanged by verification')
  } finally {
    rmSync(draftRoot, { recursive: true, force: true })
  }
})

test('draft binding writes DRAFT-only record and readback proves exact id+version+duty', () => {
  const draftRoot = mkdtempSync(join(tmpdir(), 'pd-ux-02-draft-'))
  try {
    const binding = writeDraftBinding(policy(), options(draftRoot), {
      manifest: FIXTURE_MANIFEST,
      scene_id: 'surf-scene-a',
      persona: 'embry',
      project: 'persona-dream',
      duty: 'identity',
      canonical_id: 'embry_contact_sheet_v3',
      version_content_hash: '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c',
    })
    assert.equal(binding.schema, 'persona_dream.draft_scene_binding.v1')
    assert.equal(binding.status, 'DRAFT_BOUND_NOT_APPROVED_NOT_ACCEPTED')
    assert.equal(binding.approved, false)
    assert.equal(binding.accepted, false)
    assert.equal(binding.provider_calls, 0)
    assert.equal(binding.memory_writes, 0)
    assert.equal(binding.media_duplicated, false)
    assert.equal(binding.compiler_input, RETAINED_MEDIA)

    const rows = listDraftBindings(draftRoot, 'surf-scene-a')
    assert.equal(rows.length, 1)
    assert.equal(rows[0].canonical_id, 'embry_contact_sheet_v3')
    assert.equal(rows[0].version_content_hash, '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c')
    assert.equal(rows[0].duty, 'identity')
    assert.equal(listDraftBindings(draftRoot, 'other-scene').length, 0)
  } finally {
    rmSync(draftRoot, { recursive: true, force: true })
  }
})

test('stale version hash refuses with typed code (no silent substitution server-side)', () => {
  const draftRoot = mkdtempSync(join(tmpdir(), 'pd-ux-02-stale-'))
  try {
    assert.throws(
      () => writeDraftBinding(policy(), options(draftRoot), {
        manifest: FIXTURE_MANIFEST,
        scene_id: 'surf-scene-a',
        persona: 'embry',
        project: 'persona-dream',
        duty: 'identity',
        canonical_id: 'embry_contact_sheet_v3',
        version_content_hash: 'deadbeef'.repeat(8),
      }),
      (error: unknown) => error instanceof AssetCatalogError && error.code === 'STALE_ASSET_HASH',
    )
    assert.equal(draftRootContents(draftRoot).length, 0, 'refused binding must not leave a draft')
  } finally {
    rmSync(draftRoot, { recursive: true, force: true })
  }
})

test('wrong persona scope and unqualified duty refuse with typed codes', () => {
  const draftRoot = mkdtempSync(join(tmpdir(), 'pd-ux-02-scope-'))
  const version = '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c'
  const base = {
    manifest: FIXTURE_MANIFEST,
    scene_id: 'surf-scene-a',
    project: 'persona-dream',
    canonical_id: 'embry_contact_sheet_v3',
    version_content_hash: version,
  }
  try {
    assert.throws(
      () => writeDraftBinding(policy(), options(draftRoot), { ...base, persona: 'attacker', duty: 'identity' }),
      (error: unknown) => error instanceof AssetCatalogError && error.code === 'WRONG_ASSET_SCOPE',
    )
    assert.throws(
      () => writeDraftBinding(policy(), options(draftRoot), { ...base, persona: 'embry', duty: 'style' }),
      (error: unknown) => error instanceof AssetCatalogError && error.code === 'DUTY_NOT_QUALIFIED',
    )
    assert.throws(
      () => writeDraftBinding(policy(), options(draftRoot), { ...base, persona: 'embry', duty: 'identity', canonical_id: 'nope' }),
      (error: unknown) => error instanceof AssetCatalogError && error.code === 'ASSET_NOT_FOUND',
    )
    assert.equal(draftRootContents(draftRoot).length, 0)
  } finally {
    rmSync(draftRoot, { recursive: true, force: true })
  }
})

test('tampered qualification receipt downgrades entry to unavailable with reason, read-only', () => {
  // Point the policy at a sandbox copy so the real retained fixture stays frozen.
  const sandbox = mkdtempSync(join(tmpdir(), 'pd-ux-02-tamper-'))
  const media = join(sandbox, 'embry.png')
  const receipt = join(sandbox, 'qualification.json')
  const manifestPath = join(sandbox, 'manifest.json')
  try {
    writeFileSync(media, readFileSync(RETAINED_MEDIA))
    const qualification = JSON.parse(readFileSync(join(SKILL_ROOT, 'fixtures', 'pd-ux-01', 'qualification_receipt.json'), 'utf-8'))
    writeFileSync(receipt, JSON.stringify(qualification))
    const manifest = JSON.parse(readFileSync(FIXTURE_MANIFEST, 'utf-8'))
    manifest.assets[0].sha256 = sha256File(media)
    manifest.assets[0].version_content_hash = sha256File(media)
    manifest.assets[0].image_path = media
    manifest.assets[0].provenance.image_path = media
    manifest.assets[0].qualification_evidence = [{ receipt, sha256: sha256File(receipt) }]
    writeFileSync(manifestPath, JSON.stringify(manifest))
    const sandboxPolicy = new DreamPathPolicy([sandbox])
    const good = readAssetCatalog(sandboxPolicy, { manifests: [manifestPath], draftRoot: join(sandbox, 'drafts'), pathBase: sandbox }, manifestPath)
    assert.equal(good.entries[0].state, 'qualified')

    writeFileSync(receipt, JSON.stringify({ ...qualification, status: 'FAIL' }))
    // Re-record the evidence hash so the read reaches the status check rather
    // than the (also correct) hash-refusal branch.
    manifest.assets[0].qualification_evidence = [{ receipt, sha256: sha256File(receipt) }]
    writeFileSync(manifestPath, JSON.stringify(manifest))
    const bad = readAssetCatalog(sandboxPolicy, { manifests: [manifestPath], draftRoot: join(sandbox, 'drafts'), pathBase: sandbox }, manifestPath)
    assert.equal(bad.entries[0].state, 'unavailable')
    assert.ok(bad.entries[0].reasons.includes('QUALIFICATION_RECEIPT_NONQUALIFYING'))
  } finally {
    rmSync(sandbox, { recursive: true, force: true })
  }
})

test('unconfigured manifest path refuses with CATALOG_NOT_CONFIGURED', () => {
  const draftRoot = mkdtempSync(join(tmpdir(), 'pd-ux-02-cfg-'))
  try {
    assert.throws(
      () => readAssetCatalog(policy(), options(draftRoot), '/etc/passwd'),
      (error: unknown) => error instanceof AssetCatalogError && error.code === 'CATALOG_NOT_CONFIGURED',
    )
  } finally {
    rmSync(draftRoot, { recursive: true, force: true })
  }
})

function draftRootContents(draftRoot: string): string[] {
  try {
    return readdirSync(draftRoot)
  } catch {
    return []
  }
}
