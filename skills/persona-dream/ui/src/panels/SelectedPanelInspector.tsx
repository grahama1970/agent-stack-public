/** SelectedPanelInspector — the PD-UX-04 selected-panel inspector for the
 * storyboard/review surface.
 *
 * A view/command surface only:
 *
 *  - every status, count, and hash comes from the backend projection
 *    (GET /panel-review) or the planner response (POST /edit-intent-plan);
 *  - review status (draft/candidate/accepted/rejected) is shown separately
 *    from render status (rendered/not_rendered);
 *  - selecting a candidate is not accepting it; requesting an edit is not
 *    authorizing generation — the planner runs at planning_only and the plan
 *    is a preview with backend-derived counts;
 *  - a revision change underneath makes old selections visibly STALE and
 *    forces replanning (the backend refuses stale targets; the UI refuses
 *    first and shows the refusal code if it loses the race);
 *  - reload reconstructs all state from backend evidence — nothing
 *    authoritative is kept in the browser.
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Layers, RefreshCw } from 'lucide-react'

import { dreamAssetUrl } from '../lib/asset'
import {
  buildEditIntent,
  fetchEditPlanJobStatus,
  fetchPanelReview,
  isSelectionStale,
  planCountsFrom,
  postEditIntentPlan,
  postEditPlanExecution,
  type PanelReview,
  type PanelReviewCandidate,
  type PlanResult,
} from '../lib/panelReview'

const ALL_CONSTRAINTS = [
  'identity_source_unchanged',
  'untouched_panels_reused_by_hash',
  'no_provider_submission',
  'historical_receipts_preserved',
  'target_only_no_downstream_propagation',
] as const

const REVIEW_LABEL: Record<PanelReviewCandidate['review_status'], string> = {
  draft: 'draft',
  candidate: 'candidate',
  accepted: 'accepted',
  rejected: 'rejected — stays distinct, never reused silently',
  invalid: 'invalid (unrecognized backend status)',
}

function CandidateChip({ candidate }: { candidate: PanelReviewCandidate }) {
  return (
    <span
      data-qid={`dream:panel-inspector:status:${candidate.candidate_id}`}
      style={{
        fontSize: 10, padding: '1px 6px', borderRadius: 8, marginRight: 6,
        background: candidate.review_status === 'accepted' ? '#1d4d2b'
          : candidate.review_status === 'rejected' ? '#5a1f1f'
            : candidate.review_status === 'candidate' ? '#43370f'
              : '#333',
        color: '#eee',
      }}
    >
      {REVIEW_LABEL[candidate.review_status]}
    </span>
  )
}

export function SelectedPanelInspector({ runRoot, onClose }: {
  runRoot: string
  onClose?: () => void
}) {
  const [review, setReview] = useState<PanelReview | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [panelId, setPanelId] = useState<string | null>(null)
  // Selection remembers the revision it was made in; that is what lets a
  // revision change turn it stale instead of silently re-targeting.
  const [selection, setSelection] = useState<{ candidateId: string; revisionId: string } | null>(null)
  const [compare, setCompare] = useState<[string, string] | null>(null)
  const [description, setDescription] = useState('')
  const [constraints, setConstraints] = useState<string[]>([...ALL_CONSTRAINTS].slice(0, 4))
  const [plan, setPlan] = useState<PlanResult | null>(null)
  const [planning, setPlanning] = useState(false)
  // PD-UX-06: execution handoff. The plan + authorization are files the
  // operator produced/approved (the authorization must live inside the run
  // root); the UI forwards paths to the shared CLI and renders its verdict.
  const [execPlanPath, setExecPlanPath] = useState('')
  const [execAuthPath, setExecAuthPath] = useState('')
  const [executing, setExecuting] = useState(false)
  const [execution, setExecution] = useState<Record<string, unknown> | null>(null)
  const [jobStatus, setJobStatus] = useState<Record<string, unknown> | null>(null)

  const refresh = useCallback(async () => {
    setLoadError(null)
    try {
      setReview(await fetchPanelReview(runRoot))
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error))
    }
  }, [runRoot])

  useEffect(() => { void refresh() }, [refresh])

  const panel = useMemo(
    () => review?.panels.find((p) => p.panel_id === panelId) ?? review?.panels[0] ?? null,
    [review, panelId],
  )
  const selectedCandidate = useMemo(
    () => panel?.candidates.find((c) => c.candidate_id === selection?.candidateId) ?? null,
    [panel, selection],
  )
  const stale = review ? isSelectionStale(review, selection?.revisionId ?? null) : false

  const [slotA, slotB] = compare ?? [panel?.accepted.candidate_id ?? '', panel?.candidates[1]?.candidate_id ?? '']

  const onRequestChange = async () => {
    if (!review || !panel || !selectedCandidate || stale) return
    setPlanning(true)
    setPlan(null)
    try {
      // Re-read backend evidence before submitting: if the revision moved, the
      // selection turns stale and we refuse locally rather than firing a
      // doomed intent (the backend refusal is the backstop, not the plan).
      const fresh = await fetchPanelReview(runRoot)
      setReview(fresh)
      if (isSelectionStale(fresh, selection?.revisionId ?? null)) return
      const intent = buildEditIntent({
        runId: fresh.run_id,
        revisionId: selection?.revisionId ?? fresh.active_revision_id,
        panelId: panel.panel_id,
        // The edit targets the panel slot artifact recorded in the frozen
        // revision (the accepted frame occupying it); the selected candidate
        // is reviewer context, not the hash-bound target.
        artifactRole: panel.accepted.artifact_role,
        expectedSha256: panel.accepted.sha256,
        description,
        constraints,
        intentId: `ux-${panel.panel_id}-${selectedCandidate.candidate_id}-${fresh.active_revision_id}`,
      })
      setPlan(await postEditIntentPlan(runRoot, intent))
    } catch (error) {
      setPlan({
        schema: 'persona_dream.ux_edit_intent_plan_result.v1',
        blocked: true,
        code: 'UI_SUBMIT_FAILED',
        plan: null,
        receipt: null,
        details: { error: String(error) },
        boundary: {},
      })
    } finally {
      setPlanning(false)
    }
  }

  const onExecute = async () => {
    if (!execPlanPath.trim() || !execAuthPath.trim()) return
    setExecuting(true)
    setExecution(null)
    setJobStatus(null)
    try {
      const result = await postEditPlanExecution(runRoot, execPlanPath.trim(), execAuthPath.trim())
      setExecution(result)
      const planId = String((result.result as Record<string, unknown> | undefined)?.plan_id ?? '')
      if (planId) setJobStatus(await fetchEditPlanJobStatus(runRoot, planId))
    } catch (error) {
      setExecution({ blocked: true, code: 'UI_EXECUTE_FAILED', details: { error: String(error) } })
    } finally {
      setExecuting(false)
    }
  }

  const counts = planCountsFrom(plan?.plan ?? null)

  return (
    <div role="complementary" aria-label="Selected panel inspector" data-qid="dream:panel-inspector"
      style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 12, background: '#14161a', color: '#ddd', fontSize: 12, borderRadius: 8, maxWidth: 760 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <h3 style={{ margin: 0, fontSize: 13 }}><Layers size={12} /> Selected-panel inspector</h3>
        <button type="button" data-qid="dream:panel-inspector:refresh" onClick={() => void refresh()}
          title="Reload review state from backend evidence">
          <RefreshCw size={12} /> Refresh
        </button>
        {onClose && <button type="button" data-qid="dream:panel-inspector:close" onClick={onClose}>Close</button>}
      </div>

      <div data-qid="dream:panel-inspector:boundary"
        style={{ fontSize: 10, color: '#9aa', border: '1px solid #333', borderRadius: 6, padding: 6 }}>
        View/command surface. Selected is not approved; rendered is not accepted; requesting an edit
        authorizes nothing — the planner runs planning_only and never generates, submits, or activates.
      </div>

      {loadError && <p role="alert" data-qid="dream:panel-inspector:error" style={{ color: '#f88' }}>{loadError}</p>}
      {!review && !loadError && <p data-qid="dream:panel-inspector:loading">Loading review state…</p>}

      {review && (
        <>
          <div data-qid="dream:panel-inspector:header">
            run <strong>{review.run_id}</strong> · active revision{' '}
            <strong data-qid="dream:panel-inspector:active-revision">{review.active_revision_id}</strong>
            {' · lineage: '}
            {review.revision_lineage.map((r) => `${r.revision_id} (${r.status})`).join(' → ')}
          </div>

          <div data-qid="dream:panel-inspector:references">
            identity reference:{' '}
            <span data-qid="dream:panel-inspector:identity">
              {review.identity_reference ? String(review.identity_reference.canonical_id) : '—'}
            </span>
            {' · continuity reference: '}
            <span data-qid="dream:panel-inspector:continuity">
              {review.continuity_reference ? String(review.continuity_reference.kind) : '—'}
            </span>
          </div>

          <label>
            Panel{' '}
            <select data-qid="dream:panel-inspector:panel" value={panel?.panel_id ?? ''}
              onChange={(event) => { setPanelId(event.target.value); setSelection(null); setCompare(null); setPlan(null) }}>
              {review.panels.map((p) => <option key={p.panel_id} value={p.panel_id}>{p.panel_id}</option>)}
            </select>
          </label>

          {stale && (
            <div role="alert" data-qid="dream:panel-inspector:stale-banner"
              style={{ border: '1px solid #a80', background: '#3a2f10', padding: 8, borderRadius: 6 }}>
              STALE selection: revision is now <strong>{review.active_revision_id}</strong> but the selection was
              made in <strong>{selection?.revisionId}</strong>. The selection is not re-targeted silently —
              replan against the active revision before requesting a change.
            </div>
          )}

          {panel && (
            <>
              <div data-qid="dream:panel-inspector:accepted">
                Accepted version: <strong>{panel.accepted.candidate_id}</strong> ({panel.accepted.artifact_role},{' '}
                {panel.accepted.sha256.slice(0, 19)}…) · index_verified={String(panel.accepted.index_verified)}
                {panel.accepted.receipt_path && (
                  <> · receipt:{' '}
                    <a data-qid="dream:panel-inspector:accepted-receipt" href={dreamAssetUrl(`${review.run_root}/${panel.accepted.receipt_path}`)} target="_blank" rel="noreferrer">
                      {panel.accepted.receipt_path.split('/').pop()}
                    </a>
                  </>
                )}
              </div>

              <table data-qid="dream:panel-inspector:candidates" style={{ borderCollapse: 'collapse', width: '100%' }}>
                <thead>
                  <tr>
                    <th /> <th>candidate</th> <th>review status</th> <th>render status</th> <th>verified</th> <th>findings</th> <th>receipt</th>
                  </tr>
                </thead>
                <tbody>
                  {panel.candidates.map((candidate) => (
                    <tr key={candidate.candidate_id}
                      data-qid={`dream:panel-inspector:row:${candidate.candidate_id}`}
                      style={{ background: selection?.candidateId === candidate.candidate_id ? '#24304a' : undefined }}>
                      <td>
                        <input type="radio" name="pd-panel-candidate"
                          data-qid={`dream:panel-inspector:select:${candidate.candidate_id}`}
                          aria-label={`Select ${candidate.candidate_id} for inspection — selecting is not accepting`}
                          checked={selection?.candidateId === candidate.candidate_id}
                          onChange={() => {
                            setSelection({ candidateId: candidate.candidate_id, revisionId: review.active_revision_id })
                            setPlan(null)
                          }} />
                      </td>
                      <td>{candidate.candidate_id}</td>
                      <td><CandidateChip candidate={candidate} /></td>
                      <td data-qid={`dream:panel-inspector:render-status:${candidate.candidate_id}`}>{candidate.render_status}</td>
                      <td title={candidate.verify_note}>{String(candidate.hash_verified)}</td>
                      <td>{candidate.findings.join('; ') || '—'}</td>
                      <td>
                        {candidate.receipt_path && (
                          <a data-qid={`dream:panel-inspector:receipt:${candidate.candidate_id}`}
                            href={dreamAssetUrl(`${review.run_root}/${candidate.receipt_path}`)} target="_blank" rel="noreferrer">
                            link
                          </a>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              <div>
                Side-by-side:{' '}
                <select data-qid="dream:panel-inspector:compare-a" value={slotA}
                  onChange={(event) => setCompare([event.target.value, slotB])}>
                  {panel.candidates.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{c.candidate_id}</option>)}
                </select>
                {' vs '}
                <select data-qid="dream:panel-inspector:compare-b" value={slotB}
                  onChange={(event) => setCompare([slotA, event.target.value])}>
                  {panel.candidates.map((c) => <option key={c.candidate_id} value={c.candidate_id}>{c.candidate_id}</option>)}
                </select>
              </div>
              <div data-qid="dream:panel-inspector:comparison" style={{ display: 'flex', gap: 12 }}>
                {[slotA, slotB].map((slot, index) => {
                  const candidate = panel.candidates.find((c) => c.candidate_id === slot)
                  const url = candidate?.media_path ? dreamAssetUrl(`${review.run_root}/${candidate.media_path}`) : undefined
                  return (
                    <figure key={index} style={{ margin: 0 }} data-qid={`dream:panel-inspector:compare-figure-${index === 0 ? 'a' : 'b'}`}>
                      {url
                        ? <img src={url} alt={`${slot} frame`} width={140} data-qid={`dream:panel-inspector:compare-image-${index === 0 ? 'a' : 'b'}`} />
                        : <div style={{ width: 140, height: 80, border: '1px dashed #555', display: 'grid', placeItems: 'center' }}>not rendered</div>}
                      <figcaption style={{ fontSize: 10 }}>
                        {slot} · {candidate?.review_status} / {candidate?.render_status}
                      </figcaption>
                    </figure>
                  )
                })}
              </div>

              <fieldset data-qid="dream:panel-inspector:request-change" style={{ border: '1px solid #444', borderRadius: 6 }}>
                <legend>Request change (planning only)</legend>
                <div data-qid="dream:panel-inspector:request-target">
                  target: {selectedCandidate
                    ? `${review.run_id} · ${selection?.revisionId} · ${panel.panel_id} · ${panel.accepted.artifact_role} · ${panel.accepted.sha256.slice(0, 19)}… (reviewing ${selectedCandidate.candidate_id})`
                    : 'select a candidate first'}
                </div>
                <label style={{ display: 'block', marginTop: 6 }}>
                  Requested change
                  <textarea data-qid="dream:panel-inspector:request-description" rows={2} style={{ width: '100%' }}
                    value={description}
                    onChange={(event) => setDescription(event.target.value)}
                    placeholder="What the reviewer wants changed about this frame" />
                </label>
                <div style={{ marginTop: 6 }}>
                  Preservation constraints (editable):
                  {ALL_CONSTRAINTS.map((constraint) => (
                    <label key={constraint} style={{ display: 'block' }}>
                      <input type="checkbox" data-qid={`dream:panel-inspector:constraint:${constraint}`}
                        checked={constraints.includes(constraint)}
                        onChange={(event) => {
                          setConstraints((current) => event.target.checked
                            ? [...current, constraint]
                            : current.filter((c) => c !== constraint))
                        }} />
                      {constraint}
                    </label>
                  ))}
                </div>
                <button type="button" data-qid="dream:panel-inspector:request-submit"
                  onClick={() => void onRequestChange()}
                  disabled={!selectedCandidate || stale || planning || description.trim().length === 0}
                  title={stale ? 'Selection is stale — replan against the active revision first' : 'Submit a planning-only edit intent'}>
                  {planning ? 'Planning…' : 'Plan edit (no generation, no submission)'}
                </button>
              </fieldset>

              <fieldset data-qid="dream:panel-inspector:execution" style={{ border: '1px solid #444', borderRadius: 6 }}>
                <legend>Execution handoff (approved plans only)</legend>
                <div style={{ fontSize: 10, color: '#9aa' }} data-qid="dream:panel-inspector:execution-boundary">
                  Handing off queues the existing sanctioned revision/Tau workflow — it runs nothing.
                  The authorization must live inside the run root and bind the exact plan hashes; a changed
                  plan never inherits approval. No generation, no submission, no successor activation.
                </div>
                <label style={{ display: 'block', marginTop: 6 }}>
                  Plan directory
                  <input data-qid="dream:panel-inspector:execution-plan" style={{ width: '100%' }}
                    value={execPlanPath} onChange={(event) => setExecPlanPath(event.target.value)}
                    placeholder="path to the compiled edit_intent_plan directory inside the run root" />
                </label>
                <label style={{ display: 'block' }}>
                  Authorization record
                  <input data-qid="dream:panel-inspector:execution-authorization" style={{ width: '100%' }}
                    value={execAuthPath} onChange={(event) => setExecAuthPath(event.target.value)}
                    placeholder="path to the approved edit_plan_authorization.json inside the run root" />
                </label>
                <button type="button" data-qid="dream:panel-inspector:execution-submit"
                  onClick={() => void onExecute()}
                  disabled={executing || !execPlanPath.trim() || !execAuthPath.trim()}
                  title="Hand the approved plan to the sanctioned workflow via the shared CLI">
                  {executing ? 'Handing off…' : 'Hand off for execution'}
                </button>
                {execution && (
                  <div data-qid="dream:panel-inspector:execution-result" role={execution.blocked ? 'alert' : undefined}
                    style={{ marginTop: 6, border: '1px solid #444', borderRadius: 6, padding: 6 }}>
                    <div data-qid="dream:panel-inspector:execution-status" style={{ color: execution.blocked ? '#fb8' : '#a7f3d0' }}>
                      {String(execution.code ?? execution.status ?? 'unknown')} · blocked={String(execution.blocked)}
                    </div>
                    <div data-qid="dream:panel-inspector:execution-detail">
                      state {String((execution.result as Record<string, unknown> | undefined)?.state ?? '—')}
                      {' · '}provider_call_attempts {String((execution.result as Record<string, unknown> | undefined)?.actual_provider_call_attempts ?? (execution.details ? '0' : '—'))}
                      {' · '}reconciled {String((execution.result as Record<string, unknown> | undefined)?.reconciled ?? false)}
                    </div>
                    {jobStatus && (
                      <div data-qid="dream:panel-inspector:execution-job-status">
                        job status (read-only record): state {String(jobStatus.state ?? '—')} · read_only {String(jobStatus.read_only ?? '—')}
                      </div>
                    )}
                  </div>
                )}
              </fieldset>

              {plan && (
                <div data-qid="dream:panel-inspector:plan-result"
                  style={{ border: '1px solid #444', borderRadius: 6, padding: 8 }}>
                  {plan.blocked ? (
                    <div data-qid="dream:panel-inspector:plan-blocked" role="alert" style={{ color: '#fb8' }}>
                      BLOCKED by the planner: <strong>{plan.code}</strong>{' '}
                      {Object.keys(plan.details).length > 0 && JSON.stringify(plan.details)}
                    </div>
                  ) : (
                    <div data-qid="dream:panel-inspector:plan-preview">
                      plan <strong>{String(plan.plan?.plan_id)}</strong> · {String((plan.plan?.boundary as Record<string, unknown> | undefined)?.execution ?? 'planning_only')} ·{' '}
                      source revision {String((plan.plan?.lineage as Record<string, unknown>)?.source_revision_id)} ·{' '}
                      successor <strong>null (planning only)</strong>
                      <div data-qid="dream:panel-inspector:plan-counts">
                        backend counts — regenerate {counts?.regenerate ?? '—'}, revalidate {counts?.revalidate ?? '—'},{' '}
                        stale {counts?.stale ?? '—'}, reuse {counts?.reuse ?? '—'}
                      </div>
                      <div data-qid="dream:panel-inspector:plan-boundary">
                        provider_call_attempts {String((plan.plan?.boundary as Record<string, unknown>)?.provider_call_attempts ?? 0)} ·{' '}
                        memory_writes {String((plan.plan?.boundary as Record<string, unknown>)?.memory_writes ?? 0)} ·{' '}
                        successor_activation {String((plan.plan?.boundary as Record<string, unknown>)?.successor_activation ?? false)}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}
