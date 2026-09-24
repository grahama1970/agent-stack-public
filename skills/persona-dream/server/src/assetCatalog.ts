/** Read-side reusable-asset catalog for the PD-UX-01/02 surface.
 *
 * This is a view/command surface over the PD-UX-01 canonical catalog rules in
 * scripts/contact_sheet.py: canonical manifest records, artifact hashes, and
 * qualification receipts stay authoritative; this module only re-verifies them
 * (pure local file reads, sha256) and projects what the UI may display. It
 * never calls a provider, never writes persona memory, and the only write it
 * performs is a DRAFT scene-binding file that is explicitly
 * not-approved / not-accepted.
 *
 * The typed error codes deliberately mirror contact_sheet.AssetCatalogError so
 * a refusal in the UI means the same thing as a refusal at the CLI.
 */
import { createHash, type BinaryLike } from 'node:crypto'
import { existsSync, mkdirSync, readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'

import type { DreamPathPolicy } from './paths'

export const ASSET_CATALOG_SCHEMA = 'persona_dream.reusable_asset_catalog_projection.v1'
export const UX_ASSET_CATALOG_SCHEMA = 'persona_dream.ux_asset_catalog.v1'
export const DRAFT_BINDING_SCHEMA = 'persona_dream.draft_scene_binding.v1'
export const DRAFT_BINDING_STATUS = 'DRAFT_BOUND_NOT_APPROVED_NOT_ACCEPTED'
export const QUALIFIED_LIFECYCLE = 'qualified'

export type AssetCatalogEntry = {
  canonical_id: string
  version_content_hash: string
  media_kind: string
  persona_scope: string[]
  project_scope: string[]
  provenance: Record<string, unknown>
  qualification_evidence: Array<Record<string, unknown>>
  lifecycle_status: string
  reuse_restrictions: string[]
  reference_roles: string[]
}

export type VerifiedAssetEntry = AssetCatalogEntry & {
  /** 'qualified' means every PD-UX-01 bind check passes; otherwise reasons hold. */
  state: 'qualified' | 'unavailable'
  reasons: string[]
  media_path: string | null
}

export type AssetCatalog = {
  schema: typeof UX_ASSET_CATALOG_SCHEMA
  canonical_manifest_path: string
  canonical_manifest_sha256: string
  read_only: true
  live: true
  mocked: false
  provider_calls: 0
  memory_writes: 0
  entries: VerifiedAssetEntry[]
}

export class AssetCatalogError extends Error {
  readonly code: string
  readonly details: Record<string, unknown>

  constructor(code: string, details: Record<string, unknown> = {}) {
    super(code)
    this.code = code
    this.details = details
  }
}

export function sha256File(path: string): string {
  return createHash('sha256').update(readFileSync(path) as unknown as BinaryLike).digest('hex')
}

/** Mirror of contact_sheet.project_asset_catalog: same fields, same defaults. */
export function projectCatalogFromManifest(manifest: Record<string, unknown>): AssetCatalogEntry[] {
  const assets = manifest.assets
  if (!Array.isArray(assets)) throw new AssetCatalogError('CANONICAL_MANIFEST_MISMATCH', { reason: 'assets_missing' })
  return assets.map((raw) => {
    const asset = raw as Record<string, unknown>
    const provenance = asset.provenance ?? {
      source_manifest: manifest.root,
      canonical_record_key: asset._key,
      image_path: asset.image_path,
    }
    return {
      canonical_id: String(asset.canonical_id ?? asset.asset_id ?? ''),
      version_content_hash: String(asset.version_content_hash ?? asset.sha256 ?? ''),
      media_kind: String(asset.media_kind ?? 'image'),
      persona_scope: Array.isArray(asset.persona_scope) ? asset.persona_scope.map(String) : [],
      project_scope: Array.isArray(asset.project_scope) ? asset.project_scope.map(String) : ['persona-dream'],
      provenance: (provenance ?? {}) as Record<string, unknown>,
      qualification_evidence: Array.isArray(asset.qualification_evidence)
        ? asset.qualification_evidence as Array<Record<string, unknown>>
        : [],
      lifecycle_status: String(asset.lifecycle_status ?? 'unqualified'),
      reuse_restrictions: Array.isArray(asset.reuse_restrictions) ? asset.reuse_restrictions.map(String) : [],
      reference_roles: Array.isArray(asset.reference_roles) ? asset.reference_roles.map(String) : [],
    }
  })
}

/** Resolve a catalog-internal path (receipt/media) the way _resolve_catalog_path does:
 * absolute paths directly, repo-relative paths against pathBase, then policy check. */
function resolveCatalogPath(policy: DreamPathPolicy, value: string, pathBase: string): string {
  const candidates = []
  if (value.startsWith('/')) candidates.push(value)
  else {
    candidates.push(resolve(pathBase, value))
    candidates.push(resolve(value))
  }
  for (const candidate of candidates) {
    if (existsSync(candidate)) return policy.resolveFile(candidate)
  }
  throw new AssetCatalogError('CANONICAL_ARTIFACT_MISSING', { path: value })
}

/** Mirror of contact_sheet._verify_bound_asset. Pure local reads. */
function verifyBoundAsset(policy: DreamPathPolicy, asset: AssetCatalogEntry, pathBase: string): string[] {
  const reasons: string[] = []
  const canonicalId = asset.canonical_id
  const evidence = asset.qualification_evidence
  if (!Array.isArray(evidence) || evidence.length === 0) {
    return ['MISSING_ASSET_QUALIFICATION']
  }
  const mediaHash = asset.version_content_hash.replace(/^sha256:/, '')
  for (const record of evidence) {
    try {
      const receiptPath = resolveCatalogPath(policy, String(record.receipt), pathBase)
      const expected = String(record.sha256 ?? '').replace(/^sha256:/, '')
      if (sha256File(receiptPath) !== expected) {
        reasons.push('QUALIFICATION_RECEIPT_INVALID')
        continue
      }
      const receipt = JSON.parse(readFileSync(receiptPath, 'utf-8')) as Record<string, unknown>
      if (
        receipt.schema !== 'persona_dream.contact_sheet_identity_qualification.v1'
        || receipt.status !== 'PASS_CONTACT_SHEET_IDENTITY_QUALIFIED'
      ) {
        reasons.push('QUALIFICATION_RECEIPT_NONQUALIFYING')
        continue
      }
      const receiptHash = String((receipt.image as Record<string, unknown> | undefined)?.sha256 ?? '').replace(/^sha256:/, '')
      if (receipt.asset_id !== canonicalId || receiptHash !== mediaHash) {
        reasons.push('QUALIFICATION_IDENTITY_MISMATCH')
      }
    } catch (error) {
      if (error instanceof AssetCatalogError && error.code === 'CANONICAL_ARTIFACT_MISSING') {
        reasons.push('QUALIFICATION_RECEIPT_INVALID')
      } else {
        reasons.push('QUALIFICATION_RECEIPT_INVALID')
      }
    }
  }
  const provenance = asset.provenance
  const imagePath = provenance?.image_path
  if (typeof imagePath !== 'string' || !imagePath) {
    return reasons.length > 0 ? reasons : ['MEDIA_HASH_MISMATCH']
  }
  try {
    const mediaPath = resolveCatalogPath(policy, imagePath, pathBase)
    if (sha256File(mediaPath) !== mediaHash) reasons.push('MEDIA_HASH_MISMATCH')
    else if (reasons.length === 0) return []
    return reasons
  } catch {
    reasons.push('MEDIA_HASH_MISMATCH')
    return reasons
  }
}

export type AssetLibraryOptions = {
  /** Canonical manifest paths the UI may browse. */
  manifests: readonly string[]
  /** Where DRAFT scene bindings are written. Drafts only; never accepted scenes. */
  draftRoot: string
  /** Base for repo-relative paths inside manifests (the agent-skills repo root). */
  pathBase: string
}

export function readAssetCatalog(policy: DreamPathPolicy, options: AssetLibraryOptions, requested: string): AssetCatalog {
  if (!options.manifests.includes(requested)) {
    throw new AssetCatalogError('CATALOG_NOT_CONFIGURED', { manifest: requested })
  }
  const manifestPath = policy.resolveFile(requested.startsWith('/') ? requested : resolve(options.pathBase, requested))
  let manifest: Record<string, unknown>
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf-8')) as Record<string, unknown>
  } catch {
    throw new AssetCatalogError('CANONICAL_MANIFEST_MISMATCH', { reason: 'invalid_json' })
  }
  if (manifest.schema !== 'persona_dream.reference_asset_manifest.v1') {
    throw new AssetCatalogError('CANONICAL_MANIFEST_MISMATCH', { reason: 'schema' })
  }
  const entries = projectCatalogFromManifest(manifest).map((entry) => {
    const reasons = verifyBoundAsset(policy, entry, options.pathBase)
    const mediaPath = typeof entry.provenance?.image_path === 'string' ? entry.provenance.image_path : null
    let resolvedMedia: string | null = null
    if (mediaPath) {
      try {
        resolvedMedia = resolveCatalogPath(policy, mediaPath, options.pathBase)
      } catch {
        resolvedMedia = null
      }
    }
    return {
      ...entry,
      state: (reasons.length === 0 && entry.lifecycle_status === QUALIFIED_LIFECYCLE ? 'qualified' : 'unavailable') as 'qualified' | 'unavailable',
      reasons: entry.lifecycle_status !== QUALIFIED_LIFECYCLE && reasons.length === 0
        ? ['MISSING_ASSET_QUALIFICATION']
        : reasons,
      media_path: resolvedMedia,
    }
  })
  return {
    schema: UX_ASSET_CATALOG_SCHEMA,
    canonical_manifest_path: requested,
    canonical_manifest_sha256: sha256File(manifestPath),
    read_only: true,
    live: true,
    mocked: false,
    provider_calls: 0,
    memory_writes: 0,
    entries,
  }
}

export type DraftBindingRequest = {
  manifest: string
  scene_id: string
  persona: string
  project: string
  duty: string
  canonical_id: string
  version_content_hash: string
}

/** Fail-closed DRAFT binding. Mirrors contact_sheet.compile_asset_bindings
 * scope/duty/stale checks, then writes a draft file — the only mutation this
 * module can perform, and it is explicitly not approved, not accepted. */
export function writeDraftBinding(
  policy: DreamPathPolicy,
  options: AssetLibraryOptions,
  request: DraftBindingRequest,
): Record<string, unknown> {
  const catalog = readAssetCatalog(policy, options, request.manifest)
  const entry = catalog.entries.find((item) => item.canonical_id === request.canonical_id)
  if (!entry) throw new AssetCatalogError('ASSET_NOT_FOUND', { canonical_id: request.canonical_id })
  if (request.version_content_hash !== entry.version_content_hash) {
    throw new AssetCatalogError('STALE_ASSET_HASH', {
      canonical_id: request.canonical_id,
      expected: request.version_content_hash,
      actual: entry.version_content_hash,
    })
  }
  if (entry.lifecycle_status !== QUALIFIED_LIFECYCLE || entry.qualification_evidence.length === 0) {
    throw new AssetCatalogError('MISSING_ASSET_QUALIFICATION', { canonical_id: request.canonical_id })
  }
  if (entry.state !== 'qualified') {
    throw new AssetCatalogError(entry.reasons[0] ?? 'MISSING_ASSET_QUALIFICATION', { canonical_id: request.canonical_id })
  }
  if (entry.persona_scope.length > 0 && !entry.persona_scope.includes(request.persona)) {
    throw new AssetCatalogError('WRONG_ASSET_SCOPE', { canonical_id: request.canonical_id, scope: 'persona', requested: request.persona })
  }
  if (entry.project_scope.length > 0 && !entry.project_scope.includes(request.project)) {
    throw new AssetCatalogError('WRONG_ASSET_SCOPE', { canonical_id: request.canonical_id, scope: 'project', requested: request.project })
  }
  if (!entry.reference_roles.includes(request.duty)) {
    throw new AssetCatalogError('DUTY_NOT_QUALIFIED', { canonical_id: request.canonical_id, duty: request.duty })
  }
  const binding = {
    schema: DRAFT_BINDING_SCHEMA,
    status: DRAFT_BINDING_STATUS,
    scene_id: request.scene_id,
    persona: request.persona,
    project: request.project,
    duty: request.duty,
    canonical_id: request.canonical_id,
    version_content_hash: request.version_content_hash,
    canonical_manifest_path: catalog.canonical_manifest_path,
    canonical_manifest_sha256: catalog.canonical_manifest_sha256,
    compiler_input: entry.media_path,
    qualification_evidence: entry.qualification_evidence,
    media_duplicated: false,
    approved: false,
    accepted: false,
    provider_calls: 0,
    memory_writes: 0,
    selected_is_not_approved: true,
    rendered_is_not_accepted: true,
    created_at_utc: new Date().toISOString(),
  }
  mkdirSync(options.draftRoot, { recursive: true })
  const name = `${request.scene_id}__${request.canonical_id}__${request.duty}.json`
  const tmp = join(options.draftRoot, `.${name}.tmp`)
  writeFileSync(tmp, JSON.stringify(binding, null, 2) + '\n')
  renameSync(tmp, join(options.draftRoot, name))
  return binding
}

export function listDraftBindings(draftRoot: string, sceneId?: string): Array<Record<string, unknown>> {
  if (!existsSync(draftRoot)) return []
  const rows: Array<Record<string, unknown>> = []
  for (const name of readdirSync(draftRoot).sort()) {
    if (!name.endsWith('.json') || name.startsWith('.')) continue
    try {
      const row = JSON.parse(readFileSync(join(draftRoot, name), 'utf-8')) as Record<string, unknown>
      if (!sceneId || row.scene_id === sceneId) rows.push({ ...row, draft_file: basename(name) })
    } catch {
      continue
    }
  }
  return rows
}
