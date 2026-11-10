// Runs desktop/plugin.js in Node against stand-ins for the three modules the Desktop app provides,
// and prints what it registered and rendered as JSON. Used by test_desktop_plugin.py.
//   node desktop_harness.mjs <path to plugin.js>
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const SDK = `
const G = globalThis.__cw
const marker = name => name
export const Badge = marker('Badge'), Button = marker('Button'), Codicon = marker('Codicon')
export const ConfirmDialog = marker('ConfirmDialog'), EmptyState = marker('EmptyState'), ErrorState = marker('ErrorState')
export const Tip = marker('Tip')
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav', PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const KANBAN_CARD_AREAS = { badges: 'kanban.card.badges' }
export const atom = value => ({ get: () => value })
export const useValue = a => a.get()
export const host = G.host
export const queryClient = G.queryClient
export const usePluginI18n = () => G.t
export const useQuery = options => { G.queryOptions.push(options); return G.queries[options.queryKey[1]] }
`
const REACT = `export const useState = initial => globalThis.__cw.useState(initial)\n`
const JSX = `
const render = (type, props, key) => (typeof type === 'function' ? type(props) : { type, props, key })
export const jsx = render, jsxs = render
`

const dir = mkdtempSync(join(tmpdir(), 'cw-desktop-'))
const out = {}
try {
  const url = name => JSON.stringify(pathToFileURL(join(dir, name)).href)
  writeFileSync(join(dir, 'sdk.mjs'), SDK)
  writeFileSync(join(dir, 'react.mjs'), REACT)
  writeFileSync(join(dir, 'jsx.mjs'), JSX)
  const source = readFileSync(process.argv[2], 'utf8')
    .replace(/from '@hermes\/plugin-sdk'/, `from ${url('sdk.mjs')}`)
    .replace(/from 'react\/jsx-runtime'/, `from ${url('jsx.mjs')}`)
    .replace(/from 'react'/, `from ${url('react.mjs')}`)
  writeFileSync(join(dir, 'plugin.mjs'), source)

  // -- the stand-in app ---------------------------------------------------------------------
  const bundles = {}
  const missing = []
  const t = (key, ...args) => {
    const value = key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined), bundles.en)
    if (typeof value === 'string') return value
    if (typeof value === 'function') return value(...args)
    missing.push(key)
    return key
  }
  const G = (globalThis.__cw = {
    t,
    queries: {},
    queryOptions: [],
    state: null,
    useState: initial => [G.state === null ? initial : G.state, value => { G.state = value }],
    host: { state: { viewport: { get: () => G.viewport } }, navigations: [], notices: [],
            navigate: path => G.host.navigations.push(path), notify: n => G.host.notices.push(n) },
    viewport: { width: 1400, height: 900, narrow: false },
    cacheListeners: [],
    invalidated: [],
    queryClient: {
      getQueryCache: () => ({ subscribe: fn => { G.cacheListeners.push(fn); return () => { G.disposedCache = true } } }),
      prefetchQuery: async options => options.queryFn(),
      invalidateQueries: async filter => { G.invalidated.push(filter) }
    }
  })
  const restCalls = []
  let restError = null
  const contributions = []
  const disposers = []
  const ctx = {
    rest: async (path, opts) => {
      restCalls.push({ path, opts: opts || null })
      if (restError) throw restError
      return path === '/windows' ? DATA : { ok: true }
    },
    i18n: { register: b => Object.assign(bundles, b), t },
    register: c => { contributions.push(c); return () => {} },
    registerMany: cs => { contributions.push(...cs); return () => {} },
    onDispose: fn => disposers.push(fn)
  }

  // -- synthetic data -----------------------------------------------------------------------
  const NOW = Math.floor(Date.now() / 1000)
  const win = (id, extra) => ({
    id, name: `name ${id}`, scope: 'k8s-prod', start_ts: NOW + 3600, end_ts: NOW + 7200, exclusive: true, presence: false,
    state: 'pending', kind: 'planned', reason: `reason ${id}`, cards: [], checked_in_at: null, closed_at: null,
    decided_via: null, open: false, span: `span ${id}`, overlaps: [], ...extra
  })
  const DATA = {
    now: NOW, approval: true,
    windows: [
      win('WAAAAA1', { overlaps: ['WAAAAA2'], cards: [{ task_id: 't_pending', board: '' }] }),
      win('WAAAAA2', { state: 'approved', decided_via: 'telegram', cards: [{ task_id: 't_next', board: '' }, { task_id: 't_both', board: '' }] }),
      win('WAAAAA3', { state: 'approved', decided_via: 'desktop', open: true, scope: 'infra', start_ts: NOW - 600, end_ts: NOW + 2520,
                       cards: [{ task_id: 't_open', board: '' }, { task_id: 't_both', board: '' }] }),
      win('WAAAAA4', { state: 'declined', decided_via: 'miniapp', cards: [{ task_id: 't_done', board: '' }] }),
      win('WAAAAA5', { state: 'pending', start_ts: NOW - 7200, end_ts: NOW - 3600 }),
      win('WAAAAA6', { state: 'closed', closed_at: NOW - 60, start_ts: NOW - 7200, end_ts: NOW + 60 })
    ]
  }
  const AUDIT = {
    audit: [{ id: 1, ts: NOW - 30, verdict: 'blocked', tool: 'terminal', rule_ids: '["k8s-write"]', scopes: '["k8s-prod"]', window_id: null }],
    decisions: [{ window_id: 'WAAAAA3', name: 'name WAAAAA3', scope: 'infra', state: 'approved', decided_at: NOW - 900,
                  decided_by: 'desktop', decided_via: 'desktop', when: 'Tue 10 Nov 13:45 UTC' },
                { window_id: 'WAAAAA6', name: 'name WAAAAA6', scope: 'infra', state: 'closed', decided_at: NOW - 9000,
                  decided_by: 'telegram:100200300', decided_via: 'telegram', when: 'Tue 10 Nov 11:30 UTC' },
                { window_id: 'WAAAAA4', name: 'name WAAAAA4', scope: 'infra', state: 'declined', decided_at: NOW - 9900,
                  decided_by: 'telegram:100200300', decided_via: 'miniapp' }]
  }

  // -- helpers ------------------------------------------------------------------------------
  const walk = (node, visit) => {
    if (Array.isArray(node)) return node.forEach(n => walk(n, visit))
    if (node && typeof node === 'object') {
      visit(node)
      if (node.props) for (const value of Object.values(node.props)) walk(value, visit)
    } else if (typeof node === 'string') visit(node)
  }
  const texts = tree => { const all = []; walk(tree, n => { if (typeof n === 'string') all.push(n) }); return all }
  const nodes = (tree, type) => { const all = []; walk(tree, n => { if (n && n.type === type) all.push(n) }); return all }
  const label = node => texts(node.props.children).join('')
  const summary = tree => ({
    texts: texts(tree),
    types: [...new Set((() => { const all = []; walk(tree, n => { if (n && n.type) all.push(n.type) }); return all })())],
    buttons: nodes(tree, 'Button').map(b => ({ label: label(b), size: b.props.size, variant: b.props.variant || 'default' }))
  })
  const setQueries = (data, extra = {}) => {
    G.queries = { windows: { data, isError: false, error: null, refetch: () => {}, ...extra },
                  audit: { data: data ? AUDIT : undefined, isError: false } }
  }

  // -- run ----------------------------------------------------------------------------------
  const plugin = (await import(pathToFileURL(join(dir, 'plugin.mjs')).href)).default
  plugin.register(ctx)
  await new Promise(resolve => setTimeout(resolve, 0))
  out.id = plugin.id
  out.prefetch = restCalls.map(c => c.path)
  out.contributions = contributions.map(c => ({ id: c.id, area: c.area, data: c.data ? { ...c.data, run: undefined, decorate: undefined } : null,
                                               render: typeof c.render === 'function' }))
  const byId = id => contributions.filter(c => c.id === id).at(-1)
  const page = () => byId('page').render()

  setQueries({ ...DATA, skew: 0 })
  out.wide = summary(page())
  out.pollMs = [...new Set(G.queryOptions.map(o => o.refetchInterval))]
  out.queryKeys = [...new Set(G.queryOptions.map(o => JSON.stringify(o.queryKey)))]

  G.viewport = { width: 390, height: 800, narrow: true }
  out.narrow = summary(page())
  G.viewport = { width: 1400, height: 900, narrow: false }
  out.tables = nodes(page(), 'table').map(n => ({ layout: n.props.style.tableLayout,
    widths: nodes(n, 'th').map(th => th.props.style.width || 'auto') }))
  G.viewport = { width: 1400, height: 900, narrow: false }

  setQueries({ ...DATA, approval: false, skew: 0 })
  out.approvalOff = summary(page())

  setQueries(undefined)
  out.loading = summary(page())
  setQueries(undefined, { isError: true, error: new Error('404: {"detail":"Plugin not found"}') })
  out.loadError = summary(page())
  out.loadErrorProps = nodes(page(), 'ErrorState').map(n => ({ title: n.props.title, description: n.props.description }))
  setQueries({ ...DATA, windows: [], skew: 0 })
  out.empty = nodes(page(), 'EmptyState').map(n => n.props.title)

  // approve: dialog, confirm twice (same key), reopen (new key), then a refused request
  setQueries({ ...DATA, skew: 0 })
  restCalls.length = 0
  const press = text => nodes(page(), 'Button').find(b => label(b) === text).props.onClick()
  press('Approve')
  const dialog = () => nodes(page(), 'ConfirmDialog')[0]
  out.dialog = { title: dialog().props.title, description: dialog().props.description, confirmLabel: dialog().props.confirmLabel,
                 destructive: dialog().props.destructive, open: dialog().props.open }
  await dialog().props.onConfirm()
  await dialog().props.onConfirm()
  dialog().props.onClose()
  out.dialogClosed = nodes(page(), 'ConfirmDialog').length === 0
  press('Approve')
  await dialog().props.onConfirm()
  dialog().props.onClose()
  out.approveCalls = restCalls.slice()
  out.notices = G.host.notices.slice()
  out.invalidated = G.invalidated.length

  press('Close now')
  out.closeDialog = { title: dialog().props.title, destructive: dialog().props.destructive }
  restError = new Error('Error invoking remote method: Error: 409: {"detail":"window WAAAAA1 overlaps approved WAAAAA2"}')
  out.refused = await dialog().props.onConfirm().then(() => 'resolved', e => e.message)
  restError = new Error('socket hang up')
  out.unreachable = await dialog().props.onConfirm().then(() => 'resolved', e => e.message)
  restError = null
  dialog().props.onClose()
  press('Decline')
  out.declineDialog = { title: dialog().props.title, destructive: dialog().props.destructive }
  dialog().props.onClose()

  // status-bar chip
  const chip = () => byId('chip').render()
  const chipOf = tree => (tree ? { tip: tree.props.label, text: texts(tree.props.children).join(''), icon: nodes(tree, 'Codicon')[0].props.name } : null)
  out.chipOpen = chipOf(chip())
  const tree = chip()
  nodes(tree, 'button')[0].props.onClick()
  setQueries({ ...DATA, windows: DATA.windows.filter(w => !w.open), skew: 0 })
  out.chipNext = chipOf(chip())
  setQueries({ ...DATA, windows: DATA.windows.filter(w => w.state !== 'approved'), skew: 0 })
  out.chipNone = chipOf(chip())
  setQueries(undefined)
  out.chipLoading = chipOf(chip())

  // palette and Kanban badges
  byId('open').data.run()
  out.navigations = G.host.navigations.slice()
  const emit = data => G.cacheListeners.forEach(fn => fn({ type: 'updated', query: { queryKey: [plugin.id, 'windows'], state: { data } } }))
  const before = contributions.length
  emit({ ...DATA, skew: 0 })
  emit({ ...DATA, skew: 0 })
  out.badgeRegistrations = contributions.length - before
  G.cacheListeners.forEach(fn => fn({ type: 'updated', query: { queryKey: ['other-plugin', 'windows'], state: { data: { windows: [] } } } }))
  G.cacheListeners.forEach(fn => fn({ type: 'observerAdded', query: { queryKey: [plugin.id, 'windows'], state: {} } }))
  const decorate = byId('window').data.decorate
  out.badgeArea = byId('window').area
  out.badges = Object.fromEntries(['t_pending', 't_next', 't_open', 't_both', 't_done', 't_unknown'].map(id =>
    [id, decorate({ id, status: 'ready', tenant: null, assignee: null }, { tenants: [] })]))
  emit({ ...DATA, windows: [], skew: 0 })
  out.badgesAfterEmpty = byId('window').data.decorate({ id: 't_open' }, { tenants: [] })
  out.disposers = disposers.length
  out.missingKeys = [...new Set(missing)]
} finally {
  rmSync(dir, { recursive: true, force: true })
}
process.stdout.write(JSON.stringify(out))
