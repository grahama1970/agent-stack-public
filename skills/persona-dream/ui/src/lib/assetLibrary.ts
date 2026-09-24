/** Asset-library view logic for the PD-UX-02 drawer.
 *
 * Pure functions only — no React, no fetch — so node:test can exercise the
 * rules that matter: search/filter, selection persistence by
 * canonical id + version + duty, and the no-silent-substitution rule (a
 * catalog refresh whose version differs from the persisted selection marks the
 * selection stale instead of swapping it).
 */

export type UxAssetEntry = {
  canonical_id: string
  version_content_hash: string
  media_kind: string
  persona_scope: string[]
  project_scope: string[]
  lifecycle_status: string
  reference_roles: string[]
  reuse_restrictions: string[]
  provenance: {
    source_manifest?: string
    canonical_record_key?: string
    image_path?: string
  }
  state: 'qualified' | 'unavailable'
  reasons: string[]
  media_path: string | null
}

export type UxAssetCatalog = {
  schema: string
  canonical_manifest_path: string
  canonical_manifest_sha256: string
  read_only: true
  entries: UxAssetEntry[]
}

export type AssetSelection = {
  canonical_id: string
  version_content_hash: string
  duty: string
  /** Stable identity across reloads: manifest + version, not a row index. */
  canonical_manifest_sha256: string
}

export type AssetFilters = {
  text: string
  mediaKind: string
  scope: string
  qualification: 'all' | 'qualified' | 'unavailable'
}

export const EMPTY_FILTERS: AssetFilters = { text: '', mediaKind: 'all', scope: 'all', qualification: 'all' }

export const SELECTIONS_STORAGE_KEY = 'persona-dream.asset-library.selections.v1'

export function filterAssetEntries(entries: UxAssetEntry[], filters: AssetFilters): UxAssetEntry[] {
  const text = filters.text.trim().toLowerCase()
  return entries.filter((entry) => {
    if (filters.mediaKind !== 'all' && entry.media_kind !== filters.mediaKind) return false
    if (filters.qualification === 'qualified' && entry.state !== 'qualified') return false
    if (filters.qualification === 'unavailable' && entry.state !== 'unavailable') return false
    if (filters.scope !== 'all') {
      const haystack = [...entry.persona_scope, ...entry.project_scope].map((s) => s.toLowerCase())
      if (!haystack.includes(filters.scope.toLowerCase())) return false
    }
    if (!text) return true
    const searchable = [
      entry.canonical_id,
      entry.media_kind,
      entry.lifecycle_status,
      entry.provenance.source_manifest ?? '',
      entry.provenance.canonical_record_key ?? '',
      ...entry.reference_roles,
      ...entry.reasons,
    ].join(' ').toLowerCase()
    return searchable.includes(text)
  })
}

export function availableScopes(entries: UxAssetEntry[]): string[] {
  const scopes = new Set<string>()
  entries.forEach((entry) => {
    entry.persona_scope.forEach((s) => scopes.add(s))
    entry.project_scope.forEach((s) => scopes.add(s))
  })
  return [...scopes].sort()
}

/** Why an entry cannot be used as a reference right now, in plain words. */
export function explainUnavailableReason(code: string): string {
  switch (code) {
    case 'MISSING_ASSET_QUALIFICATION':
      return 'No qualifying receipt on record for this version. Selected is not approved.'
    case 'QUALIFICATION_RECEIPT_INVALID':
      return 'A qualification receipt is missing or its hash does not match the recorded evidence.'
    case 'QUALIFICATION_RECEIPT_NONQUALIFYING':
      return 'A qualification receipt exists but its status is not a qualification.'
    case 'QUALIFICATION_IDENTITY_MISMATCH':
      return 'The qualification receipt names a different asset or media hash than this version.'
    case 'MEDIA_HASH_MISMATCH':
      return 'The media on disk does not hash to this version. The canonical record is not contradicted; the bytes are.'
    case 'CANONICAL_ARTIFACT_MISSING':
      return 'A recorded artifact path cannot be found under an allowed root.'
    case 'STALE_ASSET_HASH':
      return 'This version\'s hash no longer matches the catalog. The record is stale; refresh the catalog rather than substituting a different version.'
    case 'WRONG_ASSET_SCOPE':
      return 'This asset is not qualified for the selected persona or project scope.'
    case 'DUTY_NOT_QUALIFIED':
      return 'The asset is not qualified for the requested duty (e.g. identity) in this scene.'
    case 'ASSET_NOT_FOUND':
      return 'The catalog has no entry for this asset under the current manifest.'
    case 'CATALOG_NOT_CONFIGURED':
      return 'No canonical asset catalog is configured for this project, so no reference assets can be offered.'
    default:
      return `Unavailable: ${code}`
  }
}

export function loadSelections(storage: Pick<Storage, 'getItem'> | null): AssetSelection[] {
  if (!storage) return []
  try {
    const raw = storage.getItem(SELECTIONS_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (row): row is AssetSelection =>
        !!row && typeof row.canonical_id === 'string' && typeof row.version_content_hash === 'string'
        && typeof row.duty === 'string' && typeof row.canonical_manifest_sha256 === 'string',
    )
  } catch {
    return []
  }
}

export function saveSelections(storage: Pick<Storage, 'setItem'> | null, selections: AssetSelection[]): void {
  storage?.setItem(SELECTIONS_STORAGE_KEY, JSON.stringify(selections))
}

/** No-silent-substitution: match a persisted selection against a freshly
 *  verified catalog. A selection whose canonical id vanished is 'missing';
 *  one whose version no longer matches is 'stale' — never silently rebound. */
export type SelectionReconciliation =
  | { kind: 'current'; selection: AssetSelection; entry: UxAssetEntry }
  | { kind: 'stale'; selection: AssetSelection; entry: UxAssetEntry }
  | { kind: 'missing'; selection: AssetSelection }

export function reconcileSelections(selections: AssetSelection[], entries: UxAssetEntry[]): SelectionReconciliation[] {
  return selections.map((selection) => {
    const entry = entries.find((item) => item.canonical_id === selection.canonical_id)
    if (!entry) return { kind: 'missing', selection }
    if (entry.version_content_hash !== selection.version_content_hash) return { kind: 'stale', selection, entry }
    return { kind: 'current', selection, entry }
  })
}
