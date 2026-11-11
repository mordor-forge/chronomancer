// Runs desktop/plugin.js in Node against stand-ins for the three modules the Desktop app provides,
// and prints what it registered and rendered as JSON. Used by test_desktop_plugin.py.
//   node desktop_harness.mjs <path to plugin.js> <path to a plan view as JSON>
// The plan view is what the plugin's backend serves for the demo scenario, so the page is rendered
// from real backend output, not from hand-written data.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const SDK = `
const G = globalThis.__chm
const marker = name => name
export const Badge = marker('Badge'), Button = marker('Button'), Codicon = marker('Codicon')
export const ConfirmDialog = marker('ConfirmDialog'), ErrorState = marker('ErrorState'), Tip = marker('Tip')
export const ROUTES_AREA = 'routes', SIDEBAR_NAV_AREA = 'sidebar.nav', PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const atom = value => ({ get: () => value })
export const useValue = a => a.get()
export const host = G.host
export const queryClient = G.queryClient
export const usePluginI18n = () => G.t
export const useQuery = options => { G.queryOptions.push(options); return G.query }
`
const REACT = `
const G = globalThis.__chm
export const useState = initial => G.useState(initial)
export const useRef = initial => G.useRef(initial)
export const useEffect = () => {}
export const useMemo = make => make()
`
const JSX = `
const render = (type, props, key) => (typeof type === 'function' ? type(props) : { type, props, key })
export const jsx = render, jsxs = render
`

const dir = mkdtempSync(join(tmpdir(), 'chm-desktop-'))
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
  const VIEW = JSON.parse(readFileSync(process.argv[3], 'utf8'))

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
  const listeners = {}
  globalThis.window = {
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn) },
    removeEventListener: (type, fn) => { listeners[type] = (listeners[type] || []).filter(f => f !== fn) }
  }
  const fire = (type, event) => (listeners[type] || []).slice().forEach(fn => fn({ type, ...event }))
  const G = (globalThis.__chm = {
    t,
    query: {},
    queryOptions: [],
    // Hooks by call order, as React keeps them; `page()` restarts the count.
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
    invalidated: [],
    queryClient: {
      prefetchQuery: async options => options.queryFn(),
      invalidateQueries: async filter => { G.invalidated.push(filter) }
    }
  })
  const restCalls = []
  let restError = null
  const contributions = []
  const stored = {}
  const ctx = {
    rest: async (path, opts) => {
      restCalls.push({ path, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || null })
      if (restError) throw restError
      if (path.startsWith('/plan')) return VIEW
      return { change: { action: 'move', message: `done ${path}`, clashes: path.includes('clashing') ? ['x'] : [] } }
    },
    storage: { get: key => stored[key], set: (key, value) => { stored[key] = value } },
    i18n: { register: b => Object.assign(bundles, b), t },
    register: c => { contributions.push(c); return () => {} },
    registerMany: cs => { contributions.push(...cs); return () => {} }
  }

  // -- helpers ------------------------------------------------------------------------------
  const walk = (node, visit) => {
    if (Array.isArray(node)) return node.forEach(n => walk(n, visit))
    if (node && typeof node === 'object') {
      visit(node)
      if (node.props) for (const value of Object.values(node.props)) walk(value, visit)
    } else if (typeof node === 'string') visit(node)
  }
  // The words a person reads: strings reached through `children` only (not class names or icon names).
  const texts = tree => {
    if (typeof tree === 'string') return [tree]
    if (typeof tree === 'number') return [String(tree)]
    if (Array.isArray(tree)) return tree.flatMap(texts)
    return tree && tree.props && tree.type !== 'style' ? texts(tree.props.children) : []
  }
  const nodes = (tree, type) => { const all = []; walk(tree, n => { if (n && n.type === type) all.push(n) }); return all }
  const withClass = (tree, name) => { const all = []; walk(tree, n => { if (n && n.props && n.props.className === name) all.push(n) }); return all }
  const label = node => texts(node.props.children).join('')
  const setQuery = (data, extra = {}) => { G.query = { data, isError: false, error: null, dataUpdatedAt: 1, refetch: () => {}, ...extra } }
  const settle = () => new Promise(resolve => setTimeout(resolve, 0))

  // -- run ----------------------------------------------------------------------------------
  const plugin = (await import(pathToFileURL(join(dir, 'plugin.mjs')).href)).default
  plugin.register(ctx)
  await settle()
  out.id = plugin.id
  out.name = plugin.name
  out.prefetch = restCalls.map(c => c.path)
  out.contributions = contributions.map(c => ({ id: c.id, area: c.area, data: c.data ? { ...c.data, run: undefined } : null, render: typeof c.render === 'function' }))
  const byId = id => contributions.filter(c => c.id === id).at(-1)
  const page = () => { G.hook = 0; return byId('page').render() }
  const DATA = { ...VIEW, skew: VIEW.now - Date.now() / 1000 }
  const bars = tree => withClass(tree, 'chm-bar')
  const barOf = (tree, id) => bars(tree).find(b => b.props['aria-label'].startsWith(VIEW.items.find(i => i.task_id === id).title))
  const press = (tree, text) => nodes(tree, 'Button').find(b => label(b) === text).props.onClick()

  setQuery(DATA)
  let tree = page()
  out.wide = {
    texts: texts(tree),
    style: nodes(tree, 'style').length,
    headline: label(withClass(tree, 'chm-headline')[0]),
    facts: withClass(tree, 'chm-fact').map(n => [n.props['data-kind'], label(n)]),
    horizon: nodes(withClass(tree, 'chm-seg')[0], 'button').map(b => [label(b), b.props['aria-pressed']]),
    lanes: withClass(tree, 'chm-lane-name').map(label),
    laneColors: withClass(tree, 'chm-lane').map(n => n.props.style['--lane']),
    bars: bars(tree).map(b => ({ label: b.props['aria-label'], phase: b.props['data-phase'], clash: b.props['data-clash'], move: b.props['data-move'],
      hatch: withClass(b, 'chm-hatch').length, over: withClass(b, 'chm-over').length, left: b.props.style.left, width: b.props.style.width, top: b.props.style.top,
      title: b.props.title, type: b.type })),
    nights: withClass(tree, 'chm-night').length,
    needle: withClass(tree, 'chm-now').length,
    nowLabel: withClass(tree, 'chm-now-label').map(label),
    days: withClass(tree, 'chm-day-label').map(label),
    tray: withClass(tree, 'chm-row').map(n => ({ title: label(withClass(n, 'chm-row-title')[0]), status: label(nodes(n, 'Badge')[0]) })),
    chronicle: withClass(tree, 'chm-entry').map(n => ({ text: label(nodes(n, 'span')[0]), owner: n.props['data-owner'], icon: nodes(n, 'Codicon')[0].props.name })),
    chronicleMore: withClass(tree, 'chm-plain').map(label).filter(x => x.startsWith('Show')),
    later: withClass(tree, 'chm-quiet').map(label),
    chips: withClass(tree, 'chm-chip').map(n => ({ text: label(n), pressed: n.props['aria-pressed'] })),
    scroll: withClass(tree, 'chm-card').length,
    loomHeight: withClass(tree, 'chm-loom')[0].props.style.height
  }

  // the chronicle opens to every change, label housekeeping included
  withClass(tree, 'chm-plain').find(n => label(n).startsWith('Show all')).props.onClick()
  out.chronicleAll = { entries: withClass(page(), 'chm-entry').length, toggle: withClass(page(), 'chm-plain').map(label).filter(x => x.startsWith('Show')) }
  withClass(page(), 'chm-plain').find(n => label(n) === 'Show fewer').props.onClick()
  tree = page()

  // select a clashing slot: the scroll shows it; its partner is marked
  const clashing = VIEW.items.find(i => i.clash && i.phase === 'planned' && i.next_free_text)
  barOf(tree, clashing.task_id).props.onClick()
  tree = page()
  const scroll = withClass(tree, 'chm-card')[0]
  out.selected = {
    id: clashing.task_id,
    title: label(nodes(scroll, 'h3')[0]),
    texts: texts(scroll),
    clashes: withClass(scroll, 'chm-clash').map(n => ({ kind: n.props['data-kind'], text: texts(n.props.children)[0] })),
    buttons: nodes(scroll, 'Button').map(label),
    inputs: nodes(scroll, 'input').map(n => [n.props.type || 'text', n.props.value]),
    partners: bars(tree).filter(b => b.props['data-partner'] === '1').map(b => b.props['aria-label'].split(',')[0]),
    pressed: bars(tree).filter(b => b.props['aria-pressed']).length
  }

  // the moves, each one request
  restCalls.length = 0
  press(tree, '15 min later')
  await settle()
  press(page(), nodes(withClass(page(), 'chm-card')[0], 'Button').map(label).find(x => x.startsWith('Next free start')))
  await settle()
  press(page(), 'Pin')
  await settle()
  press(page(), 'Unplan')
  await settle()
  const forms = nodes(withClass(page(), 'chm-card')[0], 'form')
  nodes(forms[0], 'input')[0].props.onChange({ target: { value: '2030-01-02T09:30' } })
  nodes(forms[0], 'input')[1].props.onChange({ target: { value: '75' } })
  nodes(withClass(page(), 'chm-card')[0], 'form')[0].props.onSubmit({ preventDefault: () => {} })
  await settle()
  nodes(nodes(withClass(page(), 'chm-card')[0], 'form')[1], 'input')[0].props.onChange({ target: { value: ' op:upgrade ' } })
  nodes(withClass(page(), 'chm-card')[0], 'form')[1].props.onSubmit({ preventDefault: () => {} })
  await settle()
  out.moves = restCalls.filter(c => !c.path.startsWith('/plan'))
  out.notices = G.host.notices.slice()
  out.invalidated = G.invalidated.length

  // cast now: a dialog first
  restCalls.length = 0
  press(page(), 'Cast now')
  const dialog = () => nodes(page(), 'ConfirmDialog')[0]
  out.dialog = { title: dialog().props.title, description: dialog().props.description, confirmLabel: dialog().props.confirmLabel, open: dialog().props.open }
  dialog().props.onClose()
  out.dialogClosed = nodes(page(), 'ConfirmDialog').length === 0
  press(page(), 'Cast now')
  await dialog().props.onConfirm()
  out.cast = restCalls.filter(c => !c.path.startsWith('/plan'))

  // a refused move shows the backend's sentence
  G.host.notices.length = 0
  restError = new Error('Error invoking remote method: Error: 409: {"detail":"The owner pinned this slot."}')
  press(page(), '15 min earlier')
  await settle()
  restError = new Error('socket hang up')
  press(page(), '15 min earlier')
  await settle()
  restError = null
  out.refused = G.host.notices.slice()

  // keyboard: arrows move the focused bar; other keys do nothing
  restCalls.length = 0
  let prevented = 0
  const key = k => barOf(page(), clashing.task_id).props.onKeyDown({ key: k, preventDefault: () => { prevented += 1 } })
  key('ArrowRight')
  key('a')
  await settle()
  out.keyboard = { prevented, calls: restCalls.filter(c => !c.path.startsWith('/plan')) }

  // drag a bar: 4 px is a click, a real drag moves the slot by whole quarter hours
  G.slots = []
  tree = page()
  const frame = G.slots.find(s => s && typeof s === 'object' && 'current' in s && s.current === null)
  frame.current = { getBoundingClientRect: () => ({ left: 100, top: 0, width: 960, height: 400 }) }
  restCalls.length = 0
  const future = VIEW.items.filter(i => i.phase === 'planned' && i.can.move).at(-1)
  const scale = (960 - 132) / (VIEW.horizon.end - VIEW.horizon.start)
  barOf(page(), future.task_id).props.onPointerDown({ button: 0, clientX: 500 })
  fire('pointermove', { clientX: 503 })
  fire('pointerup', { clientX: 503 })
  out.dragTiny = { calls: restCalls.length, listeners: Object.values(listeners).flat().length }
  barOf(page(), future.task_id).props.onPointerDown({ button: 0, clientX: 500 })
  fire('pointermove', { clientX: 500 + 3600 * scale })
  const during = page()
  out.dragDuring = { guide: withClass(during, 'chm-guide-label').map(label), dragging: bars(during).filter(b => b.props['data-drag'] === '1').length }
  fire('pointerup', { clientX: 500 + 3600 * scale })
  await settle()
  out.drag = { calls: restCalls.filter(c => !c.path.startsWith('/plan')), expected: future.start + 3600, id: future.task_id,
               listeners: Object.values(listeners).flat().filter(f => f.name !== 'swallow').length }
  // a running card does not move
  restCalls.length = 0
  const running = VIEW.items.find(i => i.phase === 'running')
  barOf(page(), running.task_id).props.onPointerDown({ button: 0, clientX: 500 })
  fire('pointermove', { clientX: 700 })
  fire('pointerup', { clientX: 700 })
  out.dragRunning = restCalls.length

  // drag a tray card onto the timeline: planned where it is dropped; dropped outside: nothing
  const trayRow = text => withClass(page(), 'chm-row').find(n => label(withClass(n, 'chm-row-title')[0]) === text)
  const loose = VIEW.tray.find(i => i.can.move)
  restCalls.length = 0
  trayRow(loose.short).props.onPointerDown({ button: 0, clientX: 300 })
  fire('pointermove', { clientX: 20 })
  fire('pointerup', { clientX: 20 })
  await settle()
  out.dropOutside = restCalls.length
  const dropTs = VIEW.horizon.end - 6 * 3600
  const dropX = 100 + 132 + (dropTs - VIEW.horizon.start) * scale
  trayRow(loose.short).props.onPointerDown({ button: 0, clientX: 300 })
  fire('pointermove', { clientX: dropX })
  out.dropDuring = withClass(page(), 'chm-guide-label').map(label)
  fire('pointerup', { clientX: dropX })
  await settle()
  out.drop = { calls: restCalls.filter(c => !c.path.startsWith('/plan')), id: loose.task_id, near: dropTs }
  const stuck = VIEW.tray.find(i => !i.can.move)
  restCalls.length = 0
  trayRow(stuck.short).props.onPointerDown({ button: 0, clientX: 300 })
  fire('pointermove', { clientX: dropX })
  fire('pointerup', { clientX: dropX })
  out.dropTriage = restCalls.length

  // a tray card in the scroll: a Plan form and no Unplan
  trayRow(loose.short).props.onClick()
  out.trayScroll = { buttons: nodes(withClass(page(), 'chm-card')[0], 'Button').map(label), inputs: nodes(withClass(page(), 'chm-card')[0], 'input').map(n => n.props.value) }

  // the horizon switch asks for another view and remembers it
  G.slots = []
  G.queryOptions.length = 0
  tree = page()
  nodes(withClass(tree, 'chm-seg')[0], 'button').find(b => label(b) === 'Week').props.onClick()
  page()
  out.horizon = { keys: [...new Set(G.queryOptions.map(o => JSON.stringify(o.queryKey)))], stored, poll: [...new Set(G.queryOptions.map(o => o.refetchInterval))] }

  // filters: a hidden profile loses its lane; a label dims the rest; clashes only
  G.slots = []
  const chip = text => withClass(page(), 'chm-chip').find(n => label(n) === text)
  chip('scribe').props.onClick()
  out.filterProfile = { lanes: withClass(page(), 'chm-lane-name').map(label), tray: withClass(page(), 'chm-row').length }
  chip('scribe').props.onClick()
  chip('db:main').props.onClick()
  out.filterLabel = bars(page()).filter(b => b.props['data-dim'] === '0').map(b => b.props['aria-label'].split(',')[0])
  chip('db:main').props.onClick()
  chip('Clashes only').props.onClick()
  out.filterClashes = bars(page()).filter(b => b.props['data-dim'] === '0').length
  out.clear = withClass(page(), 'chm-plain').map(label).includes('Clear filters')

  // narrow: a list, no loom
  G.slots = []
  G.viewport = { width: 390, height: 800, narrow: true }
  tree = page()
  out.narrow = { loom: withClass(tree, 'chm-loom').length, rows: withClass(withClass(tree, 'chm-agenda')[0], 'chm-row').length,
                 days: nodes(withClass(tree, 'chm-agenda')[0], 'h3').map(label), hint: texts(tree).some(x => x.startsWith('Drag a slot')) }
  G.viewport = { width: 1400, height: 900, narrow: false }
  G.slots = []

  // the edges: loading, an error, a stale plan, a board that cannot be read, an empty plan
  setQuery(undefined)
  out.loading = texts(page())
  setQuery(undefined, { isError: true, error: new Error('404: {"detail":"Plugin not found"}') })
  out.loadError = nodes(page(), 'ErrorState').map(n => ({ title: n.props.title, description: n.props.description }))
  setQuery(DATA, { isError: true })
  out.stale = withClass(page(), 'chm-notice').map(label)
  setQuery({ ...DATA, board_error: 'kanban_list failed (RuntimeError).' })
  out.boardDown = withClass(page(), 'chm-notice').map(label)
  setQuery({ ...DATA, items: [], tray: [], lanes: [], log: [], labels: [], later: { count: 0, text: '' },
             counts: { clashes: 0, busy: 0, planned: 0, running: 0, attention: 0, tray: 0 },
             summary: { headline: 'Nothing is planned.', facts: [], chip: 'no slots', tip: 'Nothing is planned.' } })
  out.empty = { texts: texts(page()), bars: bars(page()).length }

  // status-bar chip and palette
  setQuery(DATA)
  const chipTree = () => { G.hook = 0; return byId('chip').render() }
  out.chip = { tip: chipTree().props.label, text: label(chipTree()), icon: nodes(chipTree(), 'Codicon')[0].props.name }
  nodes(chipTree(), 'button')[0].props.onClick()
  byId('open').data.run()
  out.navigations = G.host.navigations.slice()
  setQuery(undefined)
  out.chipLoading = chipTree()
  out.missingKeys = [...new Set(missing)]
} finally {
  rmSync(dir, { recursive: true, force: true })
}
process.stdout.write(JSON.stringify(out))
