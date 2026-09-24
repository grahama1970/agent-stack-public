import assert from 'node:assert/strict'
import test from 'node:test'

import {
  EMPTY_FILTERS,
  SELECTIONS_STORAGE_KEY,
  availableScopes,
  explainUnavailableReason,
  filterAssetEntries,
  loadSelections,
  reconcileSelections,
  saveSelections,
  type AssetSelection,
  type UxAssetEntry,
} from '../src/lib/assetLibrary'

function entry(overrides: Partial<UxAssetEntry> = {}): UxAssetEntry {
  return {
    canonical_id: 'embry_contact_sheet_v3',
    version_content_hash: '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c',
    media_kind: 'image',
    persona_scope: ['embry'],
    project_scope: ['persona-dream'],
    lifecycle_status: 'qualified',
    reference_roles: ['identity'],
    reuse_restrictions: ['selected-is-not-approved', 'rendered-is-not-accepted'],
    provenance: { source_manifest: 'reports/embry-contact-sheet-qualification-20260717.json', canonical_record_key: 'b11474f2fd5b54f332223a253fd743d1' },
    state: 'qualified',
    reasons: [],
    media_path: '/mnt/media/embry_contact_sheet_v3.png',
    ...overrides,
  }
}

const ENTRIES: UxAssetEntry[] = [
  entry(),
  entry({
    canonical_id: 'prop_teapot_v1',
    version_content_hash: 'aaaa'.repeat(16),
    media_kind: 'video',
    persona_scope: [],
    lifecycle_status: 'unqualified',
    reference_roles: [],
    provenance: { source_manifest: 'reports/prop-teapot.json', canonical_record_key: 'key-2' },
    state: 'unavailable',
    reasons: ['MISSING_ASSET_QUALIFICATION'],
  }),
]

test('filters by text, kind, scope, and qualification state', () => {
  assert.equal(filterAssetEntries(ENTRIES, EMPTY_FILTERS).length, 2)
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, text: 'teapot' }).map((e) => e.canonical_id),
    ['prop_teapot_v1'],
  )
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, text: 'embry-contact-sheet-qualification' }).map((e) => e.canonical_id),
    ['embry_contact_sheet_v3'],
    'provenance source_manifest must be searchable',
  )
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, mediaKind: 'video' }).map((e) => e.canonical_id),
    ['prop_teapot_v1'],
  )
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, scope: 'embry' }).map((e) => e.canonical_id),
    ['embry_contact_sheet_v3'],
  )
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, qualification: 'unavailable' }).map((e) => e.canonical_id),
    ['prop_teapot_v1'],
  )
  assert.deepEqual(
    filterAssetEntries(ENTRIES, { ...EMPTY_FILTERS, text: 'MISSING_ASSET_QUALIFICATION' }).map((e) => e.canonical_id),
    ['prop_teapot_v1'],
    'typed reasons must be searchable so a refusal is findable',
  )
})

test('availableScopes unions persona and project scopes, sorted', () => {
  assert.deepEqual(availableScopes(ENTRIES), ['embry', 'persona-dream'])
})

test('selections round-trip through storage and survive reload', () => {
  const memory = new Map<string, string>()
  const storage = {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => void memory.set(key, value),
  }
  const selection: AssetSelection = {
    canonical_id: 'embry_contact_sheet_v3',
    version_content_hash: '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c',
    duty: 'identity',
    canonical_manifest_sha256: 'deadbeef',
  }
  saveSelections(storage, [selection])
  assert.equal(memory.has(SELECTIONS_STORAGE_KEY), true)
  assert.deepEqual(loadSelections(storage), [selection], 'reload must restore id + version + duty exactly')
  assert.deepEqual(loadSelections(null), [], 'no storage, no crash')
  memory.set(SELECTIONS_STORAGE_KEY, 'not json')
  assert.deepEqual(loadSelections(storage), [], 'corrupt storage fails closed to empty')
})

test('catalog refresh never silently substitutes a changed version', () => {
  const selection: AssetSelection = {
    canonical_id: 'embry_contact_sheet_v3',
    version_content_hash: 'oldhash',
    duty: 'identity',
    canonical_manifest_sha256: 'm1',
  }
  const rows = reconcileSelections([selection], ENTRIES)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].kind, 'stale', 'version mismatch is stale, never rebound')
  if (rows[0].kind === 'stale') {
    assert.equal(rows[0].entry.version_content_hash, ENTRIES[0].version_content_hash)
  }
  const missing = reconcileSelections([selection], [])
  assert.equal(missing[0].kind, 'missing')
  const current = reconcileSelections(
    [{ ...selection, version_content_hash: ENTRIES[0].version_content_hash }],
    ENTRIES,
  )
  assert.equal(current[0].kind, 'current')
})

test('unavailable reasons carry plain-word explanations', () => {
  assert.match(explainUnavailableReason('MISSING_ASSET_QUALIFICATION'), /not approved/i)
  assert.match(explainUnavailableReason('MEDIA_HASH_MISMATCH'), /hash/i)
  assert.match(explainUnavailableReason('SOMETHING_NEW'), /SOMETHING_NEW/)
})

test('bind-refusal codes carry plain-language explanations, not raw codes', () => {
  const cases: Array<[string, RegExp]> = [
    ['STALE_ASSET_HASH', /stale/i],
    ['WRONG_ASSET_SCOPE', /scope/i],
    ['DUTY_NOT_QUALIFIED', /duty/i],
    ['ASSET_NOT_FOUND', /no entry/i],
    ['CATALOG_NOT_CONFIGURED', /catalog is configured/i],
  ]
  for (const [code, pattern] of cases) {
    const text = explainUnavailableReason(code)
    assert.match(text, pattern, `${code} must explain in plain words`)
    assert.doesNotMatch(text, new RegExp(code), `${code} explanation must not just echo the code`)
  }
})
