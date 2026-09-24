/** Focused component test for PD-UX-02 review follow-up: Escape while the
 *  preview modal is open closes only the preview, not the drawer.
 *  Run: NODE_PATH="$PWD/ux/app/node_modules:<any jsdom install>" \
 *    ux/app/node_modules/.bin/tsx --test ui/tests/asset-library-drawer.test.tsx */
import assert from 'node:assert/strict'
import test from 'node:test'

// ponytail: bootstrap a jsdom global when no DOM exists (tsx --test runs in Node)
if (typeof document === 'undefined') {
  const { JSDOM } = require('jsdom')
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    KeyboardEvent: dom.window.KeyboardEvent,
    localStorage: dom.window.localStorage,
  })
}

const React = require('react') as typeof import('react')
const { act } = require('react') as typeof import('react')
const { createRoot } = require('react-dom/client') as typeof import('react-dom/client')
const { AssetLibraryDrawer } = require('../src/panels/AssetLibraryDrawer') as typeof import('../src/panels/AssetLibraryDrawer')

const ENTRY = {
  canonical_id: 'embry_contact_sheet_v3',
  version_content_hash: '3ce40b3b6839ebba0f468d75a1adbb7f82e0d95457aefd3627e222eb569de00c',
  media_kind: 'image',
  persona_scope: ['embry'],
  project_scope: ['persona-dream'],
  lifecycle_status: 'qualified',
  reference_roles: ['identity'],
  reuse_restrictions: ['selected-is-not-approved'],
  provenance: { source_manifest: 'reports/q.json', canonical_record_key: 'k1' },
  state: 'qualified',
  reasons: [],
  media_path: null,
}

function stubFetch() {
  const calls: string[] = []
  globalThis.fetch = (async (url: string) => {
    calls.push(url)
    if (url.includes('asset-catalogs')) {
      return { ok: true, json: async () => ({ manifests: ['m1'] }) } as Response
    }
    return { ok: true, json: async () => ({ manifest: 'm1', entries: [ENTRY] }) } as Response
  }) as typeof fetch
  return calls
}

function key(keyName: string, target: Element) {
  target.dispatchEvent(new KeyboardEvent('keydown', {
    key: keyName, bubbles: true, cancelable: true,
  }))
}

test('Escape with preview open closes only the preview; Escape again closes the drawer', async () => {
  stubFetch()
  const originalError = console.error
  console.error = () => {}
  let closed = false
  const host = document.createElement('div')
  document.body.appendChild(host)
  let root: any
  await act(async () => {
    root = createRoot(host)
    root.render(
      <AssetLibraryDrawer sceneId="s" persona="embry" project="persona-dream" onClose={() => { closed = true }} />,
    )
  })
  await act(async () => {})

  const list = host.querySelector('[data-qid="dream:asset-library:list"]')
  assert.ok(list, 'list rendered')
  assert.equal(host.querySelector('[data-qid="dream:asset-library:preview-modal"]'), null)

  // Enter opens the preview.
  await act(async () => { key('Enter', list!) })
  assert.ok(host.querySelector('[data-qid="dream:asset-library:preview-modal"]'), 'preview opened')

  // Escape while preview open: preview closes, drawer stays, onClose NOT called.
  await act(async () => { key('Escape', list!) })
  assert.equal(host.querySelector('[data-qid="dream:asset-library:preview-modal"]'), null, 'preview closed')
  assert.ok(host.querySelector('[data-qid="dream:asset-library:drawer"]'), 'drawer still open')
  assert.equal(closed, false, 'onClose must not fire while closing the preview')

  // Escape with no preview: closes the drawer.
  await act(async () => { key('Escape', list!) })
  assert.equal(closed, true, 'second Escape closes the drawer')

  await act(async () => { root.unmount() })
  host.remove()
  console.error = originalError
})
