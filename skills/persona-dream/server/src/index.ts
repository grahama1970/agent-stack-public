import { createReadStream } from 'node:fs'
import { relative, resolve } from 'node:path'
import type { Router as ExpressRouter } from 'express'
import { Router } from 'express'
import { DreamPathPolicy, dreamContentType } from './paths'
import { buildRunDetail, collectRuns } from './runs'
import { enqueueRepairCandidate, FileTauRepairQueue, type TauRepairQueuePort } from './repair'
import { createPanelPromptBundle } from './bundles'
import { createDefaultResearchPort, createDefaultVoiceAuditionPort, type DreamResearchPort, type VoiceAuditionPort } from './services'
import { resolveRevisionArtifact } from './artifacts'
import {
  AssetCatalogError,
  listDraftBindings,
  readAssetCatalog,
  writeDraftBinding,
  type AssetLibraryOptions,
} from './assetCatalog'


import { PanelReviewError, applyEditPlan, editPlanJobStatus, planEditIntent, readPanelReview, type PanelReviewOptions } from './panelReview'

export type PersonaDreamRouterOptions = {
  reportRoots: readonly string[]
  outputRoots: readonly string[]
  assetRoots?: readonly string[]
  repairEnqueueMode?: 'disabled' | 'explicit-post-only'
  tauRepairQueue?: TauRepairQueuePort
  temporaryRoot?: string
  research?: DreamResearchPort
  voiceAudition?: VoiceAuditionPort
  /** PD-UX-02 reusable-asset library: canonical manifests + DRAFT binding root. */
  assetLibrary?: AssetLibraryOptions
  /** PD-UX-04 storyboard panel-review inspector + PD-UX-03 edit-intent planner. */
  panelReview?: PanelReviewOptions
}

export function createPersonaDreamRouter(options: PersonaDreamRouterOptions): ExpressRouter {
  const router = Router()
  const roots = [...options.reportRoots, ...options.outputRoots, ...(options.assetRoots ?? [])]
  const policy = new DreamPathPolicy(roots)
  const research = options.research ?? createDefaultResearchPort()
  const voiceAudition = options.voiceAudition ?? createDefaultVoiceAuditionPort(options)

  router.get('/runs', async (_req, res) => {
    try {
      const runs = await collectRuns(options.reportRoots, options.outputRoots)
      res.json({ status: 'ok', mocked: false, live: false, sourceRoots: [...options.reportRoots, ...options.outputRoots], runs })
    } catch (error) {
      res.status(500).json({ status: 'error', error: error instanceof Error ? error.message : String(error), runs: [] })
    }
  })

  router.get('/run-detail', async (req, res) => {
    try {
      const root = typeof req.query.root === 'string' ? req.query.root : ''
      if (!root) return res.status(400).json({ status: 'error', error: 'root required' })
      res.json(await buildRunDetail(policy, root))
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      const status = message === 'path_not_allowed' ? 403 : 500
      res.status(status).json({ status: 'error', error: message, stages: [] })
    }
  })

  router.get('/runs/:runId/revisions/:revisionId/artifacts/:artifactId', (req, res) => {
    try {
      const artifact = resolveRevisionArtifact(
        [...options.reportRoots, ...options.outputRoots],
        {
          runId: req.params.runId,
          revisionId: req.params.revisionId,
          artifactId: req.params.artifactId,
        },
      )
      res.setHeader('Content-Type', artifact.contentType)
      res.setHeader('Content-Length', String(artifact.sizeBytes))
      res.setHeader('ETag', `"${artifact.sha256.slice('sha256:'.length)}"`)
      res.setHeader('X-Persona-Dream-Run-Id', artifact.runId)
      res.setHeader('X-Persona-Dream-Revision-Id', artifact.revisionId)
      res.setHeader('X-Persona-Dream-Artifact-Id', artifact.artifactId)
      res.setHeader('X-Persona-Dream-Sha256', artifact.sha256)
      createReadStream(artifact.path).pipe(res)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      const status = message === 'artifact_identity_invalid'
        ? 400
        : message === 'artifact_sha256_mismatch' || message === 'artifact_index_identity_mismatch'
          ? 409
          : 404
      res.status(status).json({ status: 'error', error: message })
    }
  })

  router.get('/asset', (req, res) => {
    try {
      const path = typeof req.query.path === 'string' ? req.query.path : ''
      if (!path) return res.status(400).send('path required')
      const real = policy.resolveFile(path)
      res.setHeader('Content-Type', dreamContentType(real))
      createReadStream(real).pipe(res)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      res.status(message === 'path_not_allowed' ? 403 : 404).send('asset not found')
    }
  })

  if (options.assetLibrary) {
    const assetLibrary = options.assetLibrary
    const catalogError = (error: unknown) => {
      if (error instanceof AssetCatalogError) {
        return { status: error.code === 'CATALOG_NOT_CONFIGURED' || error.code === 'CANONICAL_ARTIFACT_MISSING' ? 404 : 409, body: { status: 'error', error: error.code, details: error.details } }
      }
      const message = error instanceof Error ? error.message : String(error)
      return { status: message === 'path_not_allowed' ? 403 : message === 'not_a_regular_file' ? 404 : 500, body: { status: 'error', error: message } }
    }

    // Read-side catalog projection with per-entry authority re-verification.
    // Pure local reads; no provider calls, no persona-memory writes.
    router.get('/asset-catalogs', (_req, res) => {
      res.json({ schema: 'persona_dream.ux_asset_catalogs.v1', live: true, mocked: false, manifests: [...assetLibrary.manifests], draft_status: 'DRAFT_NOT_APPROVED_NOT_ACCEPTED' })
    })

    router.get('/asset-catalog', (req, res) => {
      try {
        const manifest = typeof req.query.manifest === 'string' ? req.query.manifest : assetLibrary.manifests[0]
        if (!manifest) return res.status(400).json({ status: 'error', error: 'manifest required' })
        res.json(readAssetCatalog(policy, assetLibrary, manifest))
      } catch (error) {
        const result = catalogError(error)
        res.status(result.status).json(result.body)
      }
    })

    // Use-this-reference: writes a DRAFT scene binding only. It cannot mutate
    // accepted scenes, submit generation, approve assets, or write memory;
    // every refusal is a typed code matching the CLI catalog rules.
    router.post('/asset-catalog/draft-binding', (req, res) => {
      try {
        const body = req.body ?? {}
        res.json({ status: 'ok', live: true, mocked: false, binding: writeDraftBinding(policy, assetLibrary, body) })
      } catch (error) {
        const result = catalogError(error)
        res.status(result.status).json(result.body)
      }
    })

    router.get('/asset-catalog/draft-bindings', (req, res) => {
      const sceneId = typeof req.query.scene_id === 'string' ? req.query.scene_id : undefined
      res.json({ schema: 'persona_dream.ux_draft_bindings.v1', read_only: true, bindings: listDraftBindings(assetLibrary.draftRoot, sceneId) })
    })
  }

  if (options.panelReview) {
    const panelReview = options.panelReview
    const reviewError = (error: unknown) => {
      if (error instanceof PanelReviewError) {
        return { status: error.status, body: { status: 'error', error: error.code, details: error.details } }
      }
      const message = error instanceof Error ? error.message : String(error)
      return { status: message === 'path_not_allowed' ? 403 : 500, body: { status: 'error', error: message } }
    }

    // Read-only panel-review projection for the active revision. All counts,
    // statuses, and hashes come from this response; the browser never derives
    // authority of its own.
    router.get('/panel-review', (req, res) => {
      try {
        const runRoot = typeof req.query.run_root === 'string' ? req.query.run_root : panelReview.runRoots[0]
        const real = policy.resolveDirectory(runRoot)
        if (!panelReview.runRoots.some((root) => relative(resolve(root), real) === '')) {
          throw new PanelReviewError('run_root_not_configured', 403, { run_root: runRoot })
        }
        res.json(readPanelReview(real))
      } catch (error) {
        const result = reviewError(error)
        res.status(result.status).json(result.body)
      }
    })

    // Request-change: submits a typed edit intent to the PD-UX-03 planner.
    // Planning only — no generation, no submission, no activation, no memory.
    // The response is the backend verdict, blocked codes included.
    router.post('/edit-intent-plan', (req, res) => {
      try {
        const runRoot = typeof req.body?.run_root === 'string' ? req.body.run_root : panelReview.runRoots[0]
        const real = policy.resolveDirectory(runRoot)
        if (!panelReview.runRoots.some((root) => relative(resolve(root), real) === '')) {
          throw new PanelReviewError('run_root_not_configured', 403, { run_root: runRoot })
        }
        const intent = req.body?.intent
        if (!intent || typeof intent !== 'object') {
          throw new PanelReviewError('intent_required', 400)
        }
        res.json(planEditIntent({ skillRoot: panelReview.skillRoot, runRoot: real, intent }))
      } catch (error) {
        const result = reviewError(error)
        res.status(result.status).json(result.body)
      }
    })

    // PD-UX-05: apply an approved edit-intent plan through the SAME CLI the
    // operator runs; the server only forwards arguments, never re-implements
    // authorization/reconciliation/handoff rules. Blocked outcomes (typed
    // codes) are returned as-is with a 409 so the UI can surface them.
    router.post('/edit-plan-execution', (req, res) => {
      try {
        const runRoot = typeof req.body?.run_root === 'string' ? req.body.run_root : panelReview.runRoots[0]
        const real = policy.resolveDirectory(runRoot)
        if (!panelReview.runRoots.some((root) => relative(resolve(root), real) === '')) {
          throw new PanelReviewError('run_root_not_configured', 403, { run_root: runRoot })
        }
        const plan = typeof req.body?.plan === 'string' ? req.body.plan : ''
        const authorization = typeof req.body?.authorization === 'string' ? req.body.authorization : ''
        if (!plan || !authorization) {
          throw new PanelReviewError('plan_and_authorization_required', 400)
        }
        const result = applyEditPlan({ skillRoot: panelReview.skillRoot, runRoot: real, planPath: plan, authorizationPath: authorization })
        res.status(result.blocked ? 409 : 200).json(result)
      } catch (error) {
        const result = reviewError(error)
        res.status(result.status).json(result.body)
      }
    })

    // Read-only job status: durable record states + events; the UI renders
    // this record, it never infers execution state on its own.
    router.get('/edit-plan-execution', (req, res) => {
      try {
        const runRoot = typeof req.query.run_root === 'string' ? req.query.run_root : panelReview.runRoots[0]
        const planId = typeof req.query.plan_id === 'string' ? req.query.plan_id : ''
        const real = policy.resolveDirectory(runRoot)
        if (!panelReview.runRoots.some((root) => relative(resolve(root), real) === '')) {
          throw new PanelReviewError('run_root_not_configured', 403, { run_root: runRoot })
        }
        if (!planId) throw new PanelReviewError('plan_id_required', 400)
        res.json(editPlanJobStatus({ skillRoot: panelReview.skillRoot, runRoot: real, planId }))
      } catch (error) {
        const result = reviewError(error)
        res.status(result.status).json(result.body)
      }
    })
  }

  router.get('/report', (req, res) => {
    try {
      const path = typeof req.query.path === 'string' ? req.query.path : ''
      if (!path) return res.status(400).send('path required')
      const real = policy.resolveFile(path)
      if (!real.endsWith('/report.html')) return res.status(403).send('report path not allowed')
      res.sendFile(real)
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      res.status(message === 'path_not_allowed' ? 403 : 404).send('report not found')
    }
  })

  router.post('/panel-prompt-bundle', async (req, res) => {
    try {
      const result = await createPanelPromptBundle({
        policy,
        temporaryRoot: options.temporaryRoot ?? '/tmp/persona-dream-panel-prompt-bundles',
        filename: req.body?.filename,
        entries: req.body?.entries,
      })
      res.json({ status: 'ok', mocked: false, live: false, copied: false, browserDownloadRequired: true, ...result })
    } catch (error) {
      res.status(400).json({ status: 'error', error: error instanceof Error ? error.message : String(error) })
    }
  })

  router.post('/brave-search', async (req, res) => {
    try {
      const query = typeof req.body?.query === 'string' ? req.body.query.trim() : ''
      if (!query) return res.status(400).json({ error: 'query required' })
      res.json(await research.search(query))
    } catch (error) {
      res.status(502).json({ error: error instanceof Error ? error.message : String(error) })
    }
  })

  router.post('/voices/audition', async (req, res) => {
    try {
      res.json(await voiceAudition.audition(req.body))
    } catch (error) {
      res.status(503).json({ status: 'error', mocked: false, live: true, error: error instanceof Error ? error.message : String(error) })
    }
  })

  router.post('/stage-work-order', async (req, res) => {
    if (options.repairEnqueueMode !== 'explicit-post-only') {
      return res.status(503).json({ status: 'BLOCKED_REPAIR_ENQUEUE_DISABLED', mocked: false, live: false })
    }
    try {
      const root = typeof req.body?.runRoot === 'string' ? req.body.runRoot : ''
      if (!root) return res.status(400).json({ status: 'error', error: 'runRoot required' })
      const detail = await buildRunDetail(policy, root)
      const result = await enqueueRepairCandidate(detail, options.tauRepairQueue ?? new FileTauRepairQueue())
      res.status(result.reused ? 200 : 202).json({ status: 'REPAIR_WORK_ORDER_QUEUED', mocked: false, live: false, ...result })
    } catch (error) {
      res.status(409).json({ status: 'BLOCKED_REPAIR_WORK_ORDER', mocked: false, live: false, error: error instanceof Error ? error.message : String(error) })
    }
  })

  return router
}

export * from '../../contracts/src/index'
export * from './repair'
export * from './services'
export * from './personaMedia'
export * from './artifacts'
