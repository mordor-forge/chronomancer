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
export const Tip = marker('Tip'), CopyButton = marker('CopyButton')
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
const REACT = `
const G = globalThis.__cw
export const useState = initial => G.useState(initial)
export const useRef = initial => G.useRef(initial)
export const useEffect = () => {}
`
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
    // Hooks by call order, as React keeps them. Every hook of the plugin is in the page component,
    // so the order is the same on every render; `page()` restarts the count.
    slots: [],
    hook: 0,
    useState: initial => {
      const i = G.hook++
      if (!(i in G.slots)) G.slots[i] = initial
      return [G.slots[i], value => { G.slots[i] = value }]
    },
    useRef: initial => {
      const i = G.hook++
      if (!(i in G.slots)) G.slots[i] = { current: initial }
      return G.slots[i]
    },
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
  // "Today" starts ten hours before now, so the fixed offsets below stay inside the first day.
  const DAY0 = NOW - 10 * 3600
  const DAYS = [{ start: DAY0, end: DAY0 + 86400, label: 'Tue 10 Nov' }, { start: DAY0 + 86400, end: DAY0 + 172800, label: 'Wed 11 Nov' }]
  // What change_windows/overlaps.py `explain` serves for one conflict (the page writes none of these words).
  const conflictWith = (id, state, mine, consequence = `Approving this window is refused while 'name ${id}' is approved.`) => ({
    id, name: `name ${id}`, scope: 'k8s-prod', state, span: `span ${id}`, proposer: 'dba', exclusive: true,
    start_ts: NOW + 3600, end_ts: NOW + 7200, overlap: '11:00-12:00', length: '1 h', text: 'overlaps 11:00-12:00, 1 h',
    label: `Overlaps 'name ${id}' 11:00-12:00, 1 h`,
    why: 'same scope k8s-prod; both windows are exclusive', consequence,
    way_out: 'Decline one of them, or ask the agent to propose another time.',
    title: `This window and 'name ${id}' overlap`,
    rows: [['Other window', `name ${id} · ${state} · span ${id} · scope k8s-prod · proposed by dba`], ['Shared span', '11:00-12:00, 1 h'],
           ['Rule', 'same scope k8s-prod; both windows are exclusive'], ['What it means', consequence],
           ['Ways out', 'Decline one of them, or ask the agent to propose another time.']],
    pair: `Overlap: 'name ${mine}' and 'name ${id}', 11:00-12:00, 1 h`, note: 'overlap 1 h'
  })
  const win = (id, extra) => ({
    id, name: `name ${id}`, scope: 'k8s-prod', start_ts: NOW + 3630, end_ts: NOW + 7230, exclusive: true, presence: false,
    state: 'pending', kind: 'planned', reason: `reason ${id}`, cards: [], checked_in_at: null, closed_at: null,
    decided_via: null, open: false, span: `span ${id}`, overlaps: [], conflicts: [], proposer: 'ops',
    starts: 'Tue 10 Nov 11:00', decided_at: null, decided_when: '', closed_when: '', card_details: [], ...extra
  })
  const DATA = {
    now: NOW, approval: true, mode: 'enforce', start: DAY0, days: DAYS, zone: 'UTC',
    windows: [
      win('WAAAAA1', { overlaps: ['WAAAAA2'], conflicts: [conflictWith('WAAAAA2', 'approved', 'WAAAAA1')],
                       cards: [{ task_id: 't_pending', board: '' }],
                       card_details: [{ task_id: 't_pending', title: '<b>Upgrade Postgres</b>', status: 'blocked', assignee: 'dba' }] }),
      win('WAAAAA2', { state: 'approved', decided_via: 'telegram', decided_at: NOW - 3000, decided_when: 'Tue 10 Nov 09:10',
                       overlaps: ['WAAAAA1'], conflicts: [conflictWith('WAAAAA1', 'pending', 'WAAAAA2', "Approving 'name WAAAAA1' is refused while this window is approved.")],
                       cards: [{ task_id: 't_next', board: '' }, { task_id: 't_both', board: '' }] }),
      win('WAAAAA3', { state: 'approved', decided_via: 'desktop', decided_at: NOW - 900, decided_when: 'Tue 10 Nov 09:45',
                       open: true, scope: 'infra', start_ts: NOW - 600, end_ts: NOW + 2550, starts: 'Tue 10 Nov 09:50',
                       cards: [{ task_id: 't_open', board: '' }, { task_id: 't_both', board: '' }] }),
      win('WAAAAA4', { state: 'declined', decided_via: 'miniapp', decided_at: NOW - 9900, decided_when: 'Mon 09 Nov 21:15',
                       cards: [{ task_id: 't_done', board: '' }] }),
      win('WAAAAA5', { state: 'pending', start_ts: NOW - 7200, end_ts: NOW - 3600 }),
      win('WAAAAA6', { state: 'closed', closed_at: NOW - 60, closed_when: 'Tue 10 Nov 09:59', decided_via: 'telegram',
                       decided_at: NOW - 9000, decided_when: 'Tue 10 Nov 07:30', start_ts: NOW - 7200, end_ts: NOW + 60 }),
      win('WAAAAA7', { state: 'approved', decided_at: NOW - 100, decided_when: 'Tue 10 Nov 09:58', starts: 'Wed 11 Nov 08:00',
                       scope: 'staging', start_ts: DAY0 + 86400 + 8 * 3600, end_ts: DAY0 + 86400 + 9 * 3600 })
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
  const having = (tree, prop) => { const all = []; walk(tree, n => { if (n && n.props && n.props[prop] !== undefined) all.push(n) }); return all }
  const runway = tree => {
    const svg = having(tree, 'data-runway')[0]
    if (!svg) return null
    return {
      kind: svg.props['data-runway'], label: svg.props['aria-label'], role: svg.props.role, viewBox: svg.props.viewBox,
      days: having(svg, 'data-day').length,
      bars: having(svg, 'data-bar').map(b => ({ id: b.props['data-bar'], kind: b.props['data-kind'], lit: b.props['data-lit'],
        label: b.props['aria-label'], role: b.props.role, tabIndex: b.props.tabIndex, dim: b.props.style.opacity < 1,
        keys: typeof b.props.onKeyDown === 'function' && typeof b.props.onClick === 'function' })),
      bands: having(svg, 'data-band').filter(b => b.props['data-band'] === 'overlap')
        .map(b => ({ label: b.props['aria-label'], strong: b.props['data-strong'], text: texts(b.props.children).filter(x => x.includes('overlap')) })),
      night: having(svg, 'data-band').filter(b => b.props['data-band'] === 'night').length,
      needle: having(svg, 'data-needle').map(n => n.props['data-needle']),
      patterns: nodes(svg, 'pattern').map(n => n.props.id),
      readout: having(tree, 'data-readout').map(n => label(n))[0]
    }
  }
  const door = tree => having(tree, 'data-door').map(n => ({ kind: n.props['data-door'], mode: n.props['data-mode'], role: n.props.role,
    icon: nodes(n, 'Codicon')[0].props.name, lines: nodes(n, 'p').map(label) }))[0] || null
  const cardsLit = tree => having(tree, 'data-window').filter(n => n.props['data-lit'] === 'yes').map(n => n.props['data-window'])
  const panel = tree => having(tree, 'data-conflict').map(n => ({ on: n.props['data-conflict'], title: n.props['aria-label'], role: n.props.role,
    rows: nodes(n, 'dt').map(label).map((term, i) => [term, label(nodes(n, 'dd')[i])]), buttons: nodes(n, 'Button').map(label) }))
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
  const page = () => { G.hook = 0; return byId('page').render() }

  setQueries({ ...DATA, skew: 0 })
  out.wide = summary(page())
  out.door = door(page())
  out.runway = runway(page())
  out.groups = having(page(), 'data-group').map(n => n.props['data-group'])
  out.stamps = Object.fromEntries(having(page(), 'data-window').map(n => [n.props['data-window'], label(having(n, 'data-stamp')[0])]))
  out.cardChips = having(page(), 'data-card').map(n => ({ id: n.props['data-card'], type: n.type, text: label(n) }))
  out.overlapChips = having(page(), 'data-overlap').map(n => ({ to: n.props['data-overlap'], type: n.type, text: label(n), expanded: n.props['aria-expanded'] }))
  out.copies = nodes(page(), 'CopyButton').map(n => n.props.text)
  out.ledger = having(page(), 'data-verdict').map(n => ({ verdict: n.props['data-verdict'], cells: nodes(n, 'span').map(label), icon: nodes(n, 'Codicon')[0].props.name }))
  out.articles = having(page(), 'data-window').map(n => ({ type: n.type, tabIndex: n.props.tabIndex, label: n.props['aria-label'] }))[0]

  // the runway: focus reads a bar out, a click marks its card
  const barOf = id => having(page(), 'data-bar').find(b => b.props['data-bar'] === id)
  barOf('WAAAAA2').props.onFocus()
  out.focused = runway(page())
  barOf('WAAAAA2').props.onBlur()
  let prevented = 0
  barOf('WAAAAA3').props.onKeyDown({ key: 'Enter', preventDefault: () => { prevented += 1 } })
  barOf('WAAAAA3').props.onKeyDown({ key: 'a', preventDefault: () => { prevented += 1 } })
  out.picked = { lit: cardsLit(page()), prevented }

  // an overlap chip leads to the other window's card and explains the conflict there
  const chipOn = (from, to) => having(page(), 'data-window').find(n => n.props['data-window'] === from)
  const overlapButton = (from, to) => having(chipOn(from), 'data-overlap').find(n => n.props['data-overlap'] === to)
  overlapButton('WAAAAA1', 'WAAAAA2').props.onClick()
  out.conflict = { panel: panel(page()), lit: cardsLit(page()), runway: runway(page()),
                   expanded: having(page(), 'data-overlap').map(n => [n.props['data-overlap'], n.props['aria-expanded']]) }
  nodes(having(page(), 'data-conflict')[0], 'Button').find(b => label(b).startsWith('Show')).props.onClick()
  out.conflictBack = { panel: panel(page()).map(x => x.on), lit: cardsLit(page()) }
  nodes(having(page(), 'data-conflict')[0], 'Button').find(b => label(b) === 'Close explanation').props.onClick()
  out.conflictClosed = { panels: panel(page()).length, bands: runway(page()).bands.map(b => b.strong), dim: runway(page()).bars.some(b => b.dim) }
  G.slots = []

  // the door in its other states
  setQueries({ ...DATA, windows: DATA.windows.filter(w => !w.open), skew: 0 })
  out.doorNext = door(page())
  setQueries({ ...DATA, windows: DATA.windows.filter(w => w.state !== 'approved'), skew: 0 })
  out.doorClosed = door(page())
  setQueries({ ...DATA, mode: 'warn', skew: 0 })
  out.doorWarn = door(page())
  setQueries({ ...DATA, mode: 'off', windows: [], skew: 0 })
  out.doorOff = door(page())
  // a backend from before the runway: no days, no conflict details, no formatted moments
  const old = w => { const { conflicts, proposer, starts, decided_when, closed_when, card_details, decided_at, ...rest } = w; return rest }
  setQueries({ now: NOW, approval: true, windows: DATA.windows.map(old), skew: 0 })
  out.oldBackend = { runway: runway(page()), door: door(page()), chips: having(page(), 'data-overlap').map(label),
                     buttons: summary(page()).buttons.length }
  setQueries({ ...DATA, skew: 0 })
  out.pollMs = [...new Set(G.queryOptions.map(o => o.refetchInterval))]
  out.queryKeys = [...new Set(G.queryOptions.map(o => JSON.stringify(o.queryKey)))]

  G.viewport = { width: 390, height: 800, narrow: true }
  out.narrow = summary(page())
  out.ladder = runway(page())
  G.viewport = { width: 1400, height: 900, narrow: false }
  // a wide window with a small content column (a docked sidebar) also gets the ladder
  G.slots = [null, null, null, null, 420]
  out.squeezed = runway(page()).kind
  G.slots = [null, null, null, null, 900]
  out.measured = runway(page()).viewBox
  G.slots = []

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
  const chip = () => { G.hook = 0; return byId('chip').render() }
  const chipOf = tree => (tree ? { tip: tree.props.label, text: texts(tree.props.children).join(''), icon: nodes(tree, 'Codicon')[0].props.name } : null)
  out.chipOpen = chipOf(chip())
  const tree = chip()
  nodes(tree, 'button')[0].props.onClick()
  setQueries({ ...DATA, windows: DATA.windows.filter(w => !w.open), skew: 0 })
  out.chipNext = chipOf(chip())
  setQueries({ ...DATA, windows: DATA.windows.filter(w => w.state !== 'approved'), skew: 0 })
  out.chipNone = chipOf(chip())
  setQueries({ ...DATA, mode: 'warn', skew: 0 })
  out.chipWarn = chipOf(chip())
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
