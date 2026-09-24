/** AssetLibraryDrawer — the PD-UX-02 reusable-asset library surface.
 *
 * A drawer, not a dashboard: it grows the ContactSheetBoard workspace by
 * adding a searchable, keyboard-operable view over the PD-UX-01 canonical
 * asset catalog. It is a view/command surface only:
 *
 *  - browsing/filtering triggers no model generation, no uploads, no memory writes;
 *  - "use this reference" writes a DRAFT scene binding and nothing else —
 *    accepted scenes are never mutated, nothing is approved, nothing submitted;
 *  - incompatible/unavailable entries stay visible with their typed reason;
 *  - selections persist (canonical id + version + duty) across reloads, and a
 *    catalog refresh that changes a version marks the selection stale rather
 *    than substituting it.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Images, X } from 'lucide-react'

import { dreamAssetUrl } from '../lib/asset'
import {
  EMPTY_FILTERS,
  explainUnavailableReason,
  filterAssetEntries,
  loadSelections,
  reconcileSelections,
  saveSelections,
  availableScopes,
  type AssetFilters,
  type AssetSelection,
  type UxAssetCatalog,
  type UxAssetEntry,
} from '../lib/assetLibrary'
import { nvis } from '../styles'
import { AssetProvenancePreview } from './AssetProvenancePreview'

type DraftResult =
  | { ok: true; status: string }
  | { ok: false; error: string }

export function AssetLibraryDrawer({ sceneId, persona, project, onClose }: {
  sceneId: string
  persona: string
  project: string
  onClose: () => void
}) {
  const [catalog, setCatalog] = useState<UxAssetCatalog | null>(null)
  const [manifests, setManifests] = useState<string[]>([])
  const [manifest, setManifest] = useState<string>('')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filters, setFilters] = useState<AssetFilters>(EMPTY_FILTERS)
  const [selections, setSelections] = useState<AssetSelection[]>(() => loadSelections(typeof window === 'undefined' ? null : window.localStorage))
  const [preview, setPreview] = useState<UxAssetEntry | null>(null)
  const [draftDuty, setDraftDuty] = useState<Record<string, string>>({})
  const [draftResult, setDraftResult] = useState<DraftResult | null>(null)
  const [focused, setFocused] = useState(0)
  const listRef = useRef<HTMLDivElement | null>(null)

  const refresh = useCallback(async (chosen: string) => {
    setLoadError(null)
    try {
      const response = await fetch(`/api/projects/dream/asset-catalog?manifest=${encodeURIComponent(chosen)}`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.error ?? `HTTP ${response.status}`)
      setCatalog(payload as UxAssetCatalog)
      setManifest(chosen)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error))
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    fetch('/api/projects/dream/asset-catalogs')
      .then((response) => response.json())
      .then((payload: { manifests?: string[] }) => {
        if (cancelled) return
        const list = payload.manifests ?? []
        setManifests(list)
        if (list.length > 0) void refresh(list[0])
      })
      .catch(() => { if (!cancelled) setLoadError('asset-catalogs unreachable') })
    return () => { cancelled = true }
  }, [refresh])

  const entries = useMemo(() => catalog?.entries ?? [], [catalog])
  const filtered = useMemo(() => filterAssetEntries(entries, filters), [entries, filters])
  const scopes = useMemo(() => availableScopes(entries), [entries])
  const reconciliation = useMemo(
    () => (catalog ? reconcileSelections(selections, catalog.entries) : []),
    [catalog, selections],
  )
  const focusedEntry = filtered[focused] ?? null

  const updateSelections = (next: AssetSelection[]) => {
    setSelections(next)
    saveSelections(typeof window === 'undefined' ? null : window.localStorage, next)
  }

  const useAsReference = async (entry: UxAssetEntry) => {
    const duty = draftDuty[entry.canonical_id] ?? entry.reference_roles[0] ?? ''
    if (!duty || !catalog) return
    setDraftResult(null)
    const selection: AssetSelection = {
      canonical_id: entry.canonical_id,
      version_content_hash: entry.version_content_hash,
      duty,
      canonical_manifest_sha256: catalog.canonical_manifest_sha256,
    }
    const response = await fetch('/api/projects/dream/asset-catalog/draft-binding', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest,
        scene_id: sceneId,
        persona,
        project,
        duty,
        canonical_id: entry.canonical_id,
        version_content_hash: entry.version_content_hash,
      }),
    })
    const payload = await response.json()
    if (!response.ok) {
      setDraftResult({ ok: false, error: payload.error ?? `HTTP ${response.status}` })
      return
    }
    const rest = selections.filter((row) => !(row.canonical_id === entry.canonical_id && row.duty === duty))
    updateSelections([...rest, selection])
    setDraftResult({ ok: true, status: payload.binding?.status ?? 'DRAFT_BOUND_NOT_APPROVED_NOT_ACCEPTED' })
  }

  const onListKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setFocused((value) => Math.min(value + 1, filtered.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setFocused((value) => Math.max(value - 1, 0))
    } else if ((event.key === 'Enter' || event.key === ' ') && focusedEntry) {
      event.preventDefault()
      setPreview(focusedEntry)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      if (preview) {
        setPreview(null)
        return
      }
      onClose()
    }
  }

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(`[data-asset-index="${focused}"]`)
    node?.focus()
  }, [focused])

  const selectionFor = (entry: UxAssetEntry, duty?: string) =>
    selections.find((row) => row.canonical_id === entry.canonical_id && (!duty || row.duty === duty))

  return (
    <div
      role="dialog"
      aria-label="Reusable asset library"
      data-qid="dream:asset-library:drawer"
      style={nvis.assetLibraryDrawer}
    >
      <div style={nvis.assetLibraryHeader}>
        <h3 style={nvis.assetStripTitle}><Images size={12} /> Reusable Asset Library</h3>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <label style={nvis.assetLibraryLabel}>
            Catalog{' '}
            <select
              data-qid="dream:asset-library:manifest"
              value={manifest}
              onChange={(event) => void refresh(event.target.value)}
              style={nvis.assetLibrarySelect}
            >
              {manifests.map((item) => <option key={item} value={item}>{item.split('/').slice(-2).join('/')}</option>)}
            </select>
          </label>
          <button
            type="button"
            data-qid="dream:asset-library:refresh"
            aria-label="Refresh catalog; selections pinned to a changed version turn stale instead of substituting"
            onClick={() => manifest && void refresh(manifest)}
            style={nvis.contactSheetAction}
          >
            Refresh
          </button>
          <button type="button" data-qid="dream:asset-library:close" aria-label="Close asset library" onClick={onClose} style={nvis.contactSheetAction}>
            <X size={12} />
          </button>
        </div>
      </div>

      <div style={nvis.assetLibraryBoundary} data-qid="dream:asset-library:boundary">
        View only. Use-this-reference writes a DRAFT scene binding — never an accepted scene,
        never an approval, never a provider submission, never a persona-memory write.
        Selected is not approved; rendered is not accepted.
      </div>

      {loadError && <div role="alert" style={nvis.assetLibraryError}>{loadError}</div>}

      <div style={nvis.assetLibraryFilters}>
        <input
          type="search"
          data-qid="dream:asset-library:search"
          aria-label="Search assets by id, provenance, role, or reason"
          placeholder="Search id, provenance, role, reason…"
          value={filters.text}
          onChange={(event) => { setFilters({ ...filters, text: event.target.value }); setFocused(0) }}
          style={nvis.assetLibraryInput}
        />
        <label style={nvis.assetLibraryLabel}>
          Kind{' '}
          <select
            data-qid="dream:asset-library:filter-kind"
            aria-label="Filter by media kind"
            value={filters.mediaKind}
            onChange={(event) => { setFilters({ ...filters, mediaKind: event.target.value }); setFocused(0) }}
            style={nvis.assetLibrarySelect}
          >
            <option value="all">all</option>
            {[...new Set(entries.map((entry) => entry.media_kind))].sort().map((kind) => <option key={kind} value={kind}>{kind}</option>)}
          </select>
        </label>
        <label style={nvis.assetLibraryLabel}>
          Scope{' '}
          <select
            data-qid="dream:asset-library:filter-scope"
            aria-label="Filter by persona or project scope"
            value={filters.scope}
            onChange={(event) => { setFilters({ ...filters, scope: event.target.value }); setFocused(0) }}
            style={nvis.assetLibrarySelect}
          >
            <option value="all">all</option>
            {scopes.map((scope) => <option key={scope} value={scope}>{scope}</option>)}
          </select>
        </label>
        <label style={nvis.assetLibraryLabel}>
          Qualification{' '}
          <select
            data-qid="dream:asset-library:filter-qualification"
            aria-label="Filter by qualification state"
            value={filters.qualification}
            onChange={(event) => { setFilters({ ...filters, qualification: event.target.value as AssetFilters['qualification'] }); setFocused(0) }}
            style={nvis.assetLibrarySelect}
          >
            <option value="all">all</option>
            <option value="qualified">qualified</option>
            <option value="unavailable">unavailable</option>
          </select>
        </label>
      </div>

      <div
        ref={listRef}
        role="listbox"
        aria-label="Catalog assets"
        aria-activedescendant={focusedEntry ? `asset-option-${focusedEntry.canonical_id}` : undefined}
        tabIndex={0}
        data-qid="dream:asset-library:list"
        onKeyDown={onListKeyDown}
        style={nvis.assetLibraryList}
      >
        {filtered.map((entry, index) => {
          const unavailable = entry.state !== 'qualified'
          const selectedDuty = selectionFor(entry)?.duty
          const stale = reconciliation.some(
            (row) => row.kind === 'stale' && row.selection.canonical_id === entry.canonical_id,
          )
          const mediaUrl = entry.media_path ? dreamAssetUrl(entry.media_path) : undefined
          return (
            <div
              key={entry.canonical_id}
              id={`asset-option-${entry.canonical_id}`}
              data-qid={`dream:asset-library:entry:${entry.canonical_id}`}
              data-asset-index={index}
              role="option"
              aria-selected={!!selectedDuty}
              aria-disabled={unavailable}
              tabIndex={-1}
              onFocus={() => setFocused(index)}
              onClick={() => setFocused(index)}
              onDoubleClick={() => !unavailable && setPreview(entry)}
              style={{
                ...nvis.assetLibraryEntry,
                opacity: unavailable ? 0.65 : 1,
                outline: index === focused ? '1px solid #4a9eff' : 'none',
              }}
            >
              <button
                type="button"
                data-qid={`dream:asset-library:preview:${entry.canonical_id}`}
                aria-label={`Preview ${entry.canonical_id}`}
                onClick={() => setPreview(entry)}
                style={nvis.assetThumbButton}
              >
                {mediaUrl
                  ? <AssetProvenancePreview asset={{ id: entry.canonical_id, title: entry.canonical_id, url: mediaUrl, mediaType: entry.media_kind }} />
                  : <span style={nvis.assetLibraryNoMedia}>no preview</span>}
              </button>
              <div style={nvis.assetLibraryEntryBody}>
                <span style={nvis.assetTableTitle}>
                  {entry.canonical_id}
                  <span style={{ ...nvis.assetLibraryBadge, color: unavailable ? '#f87171' : '#a7f3d0' }}>
                    {unavailable ? 'unavailable' : 'qualified'}
                  </span>
                  {selectedDuty && (
                    <span style={{ ...nvis.assetLibraryBadge, color: '#93c5fd' }}>
                      draft: {selectedDuty}
                    </span>
                  )}
                  {stale && (
                    <span style={{ ...nvis.assetLibraryBadge, color: '#fbbf24' }} title="Catalog version changed; reselect to rebind">
                      version changed — reselect
                    </span>
                  )}
                </span>
                <span style={nvis.assetTableCaption}>
                  version {entry.version_content_hash.slice(0, 12)}… · {entry.media_kind} · lifecycle {entry.lifecycle_status}
                </span>
                <span style={nvis.assetTableCaption}>
                  provenance: {entry.provenance.source_manifest ?? 'unknown'} (key {entry.provenance.canonical_record_key ?? 'unknown'})
                </span>
                {unavailable && (
                  <span role="note" data-qid={`dream:asset-library:reasons:${entry.canonical_id}`} style={{ ...nvis.assetTableCaption, color: '#f87171' }}>
                    {entry.reasons.map((reason) => explainUnavailableReason(reason)).join(' ')}
                  </span>
                )}
                {selectedDuty && (
                  <span style={{ ...nvis.assetTableCaption, color: '#93c5fd' }}>
                    DRAFT binding on scene {sceneId} — not approved, not accepted.
                  </span>
                )}
              </div>
              <div style={nvis.assetLibraryEntryActions}>
                <label style={nvis.assetLibraryLabel}>
                  Duty{' '}
                  <select
                    data-qid={`dream:asset-library:duty:${entry.canonical_id}`}
                    aria-label={`Reference duty for ${entry.canonical_id}`}
                    value={draftDuty[entry.canonical_id] ?? entry.reference_roles[0] ?? ''}
                    onChange={(event) => setDraftDuty({ ...draftDuty, [entry.canonical_id]: event.target.value })}
                    style={nvis.assetLibrarySelect}
                  >
                    {entry.reference_roles.map((role) => <option key={role} value={role}>{role}</option>)}
                  </select>
                </label>
                <button
                  type="button"
                  data-qid={`dream:asset-library:use:${entry.canonical_id}`}
                  disabled={unavailable}
                  title={unavailable ? entry.reasons.map(explainUnavailableReason).join(' ') : 'Write a DRAFT scene binding only'}
                  onClick={() => void useAsReference(entry)}
                  style={nvis.contactSheetAction}
                >
                  Use this reference
                </button>
              </div>
            </div>
          )
        })}
        {filtered.length === 0 && (
          <div style={nvis.assetStripEmpty}>No catalog entries match these filters.</div>
        )}
      </div>

      {selections.length > 0 && (
        <div data-qid="dream:asset-library:selections" style={nvis.assetLibrarySelections}>
          {reconciliation.map((row) => (
            <span key={`${row.selection.canonical_id}:${row.selection.duty}`} style={nvis.assetLibraryBadge}>
              {row.selection.canonical_id}@{row.selection.version_content_hash.slice(0, 12)}… as {row.selection.duty}
              {row.kind === 'stale' ? ' (stale — version changed)' : ''}
              {row.kind === 'missing' ? ' (absent from refreshed catalog)' : ''}
            </span>
          ))}
        </div>
      )}

      {draftResult && (
        <div
          role="status"
          data-qid="dream:asset-library:draft-result"
          style={draftResult.ok ? nvis.assetLibraryDraftOk : nvis.assetLibraryError}
        >
          {draftResult.ok
            ? `${draftResult.status} — a DRAFT binding only. No accepted scene was mutated; no provider was called; nothing was approved.`
            : `Refused: ${draftResult.error}`}
        </div>
      )}

      {preview && (
        <div
          role="dialog"
          aria-label={`Preview ${preview.canonical_id}`}
          data-qid="dream:asset-library:preview-modal"
          style={nvis.assetLibraryPreviewBackdrop}
          onClick={() => setPreview(null)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault()
              event.stopPropagation()
              setPreview(null)
            }
          }}
        >
          <div style={nvis.assetLibraryPreview} onClick={(event) => event.stopPropagation()}>
            <div style={nvis.assetLibraryHeader}>
              <strong>{preview.canonical_id}</strong>
              <span style={nvis.assetTableCaption}>version {preview.version_content_hash.slice(0, 16)}…</span>
              <button type="button" aria-label="Close preview" onClick={() => setPreview(null)} style={nvis.contactSheetAction}><X size={12} /></button>
            </div>
            {preview.media_path && dreamAssetUrl(preview.media_path) && (
              <img
                src={dreamAssetUrl(preview.media_path)}
                alt={`${preview.canonical_id} media preview`}
                style={{ maxWidth: '100%', maxHeight: '60vh', objectFit: 'contain' }}
              />
            )}
            <div style={nvis.assetTableCaption}>
              provenance {preview.provenance.source_manifest ?? 'unknown'} · record {preview.provenance.canonical_record_key ?? 'unknown'}
              · qualification {preview.lifecycle_status}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
