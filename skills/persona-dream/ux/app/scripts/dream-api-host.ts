/** Dev host for the persona-dream pipeline API.
 *
 * The pipeline workspace (ui/src/DreamWorkspace.tsx) reads
 * /api/projects/dream/*, served by server/src (an express Router with no
 * standalone binary). This host mounts that router for local development;
 * vite.config.ts proxies /api/projects/dream here. Same pattern as the
 * receipted e2e host in reports/goal_v5/emotion_proof/http_host_e2e/boot.ts.
 */
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import express from 'express'
import { createPersonaDreamRouter } from '../../../server/src/index'

const here = dirname(fileURLToPath(import.meta.url))
const skillRoot = resolve(here, '../../..')

const outputRoot = '/mnt/storage12tb/skills/persona-dream/outputs'
const voiceRefs = resolve(skillRoot, '../../../chatterbox/persona_dream_voice_refs')
// Persona memory documents reference their media under this root (see
// persona_memory.media_asset.v1 source_path values).
const personaMedia = '/mnt/storage12tb/media/personas'

const repoRoot = resolve(skillRoot, '../../..')
// PD-UX-01 canonical manifests the asset library (PD-UX-02) may browse. The
// fixture manifest is the hash-locked qualified catalog used as acceptance
// evidence; the asset-root manifest is the live projection target.
const assetLibraryManifests = [
  resolve(skillRoot, 'fixtures/pd-ux-01/canonical_asset_manifest.json'),
  '/mnt/storage12tb/media/personas/embry/assets/contact_sheets/embry-gpt-image-2-v3/reference_asset_manifest.json',
].filter((p) => existsSync(p))

const app = express()
app.use(express.json({ limit: '4mb' }))
app.use(
  '/api/projects/dream',
  createPersonaDreamRouter({
    reportRoots: [resolve(skillRoot, 'reports')],
    outputRoots: existsSync(outputRoot) ? [outputRoot] : [],
    assetRoots: [voiceRefs, personaMedia].filter((p) => existsSync(p)),
    // Draft bindings are working state, not acceptance records: keep them out
    // of the repo, under the skill's scratch area.
    assetLibrary: {
      manifests: assetLibraryManifests,
      draftRoot: process.env.PERSONA_DREAM_DRAFT_ROOT ?? '/tmp/persona-dream-draft-bindings',
      pathBase: repoRoot,
    },
  }),
)

const port = Number(process.env.DREAM_API_PORT ?? 8791)
app.listen(port, '127.0.0.1', () => console.log(`dream api host listening on 127.0.0.1:${port}`))
