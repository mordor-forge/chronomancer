/**
 * change-windows: the Hermes Desktop half.
 *
 * - Page `/change-windows` with a sidebar row: windows by state, overlap warnings, linked cards,
 *   recent decisions and matched calls (metadata only). Approve / Decline / Close now, each behind
 *   a confirm dialog and an idempotency key.
 * - Status-bar chip: the open window and its time left, else the next approved window.
 * - Kanban card badges: a card linked to a window shows the window's state and time.
 * - Palette command that opens the page.
 *
 * Plain ESM, loaded uncompiled: jsx() calls, no JSX. Imports only @hermes/plugin-sdk, react and
 * react/jsx-runtime. Data comes from the plugin's own backend (dashboard/plugin_api.py) by
 * polling; no socket. Layout and spacing are inline styles on theme variables, because an
 * uncompiled plugin cannot add utility classes to the app's stylesheet.
 */

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  host,
  KANBAN_CARD_AREAS,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Tip,
  usePluginI18n,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'change-windows'
const PATH = '/change-windows'
const POLL_MS = 7000
const WINDOWS_KEY = [ID, 'windows']
const AUDIT_KEY = [ID, 'audit']

const EN = {
  title: 'Change windows',
  subtitle: 'Approved time windows for production changes by agents.',
  open: 'Open change windows',
  loading: 'Loading change windows…',
  loadFailed: 'Change windows are unavailable',
  loadFailedHint:
    'The plugin backend did not answer. Enable the change-windows plugin for this profile and restart the gateway.',
  stale: 'Could not refresh. Showing the last data received.',
  retry: 'Retry',
  empty: 'No change windows',
  emptyHint: 'Nothing from today to seven days ahead. Agents propose windows; you approve them.',
  approvalOff: 'Desktop approval is off (approval.desktop: false). Approve or decline in Telegram.',
  trust: 'Approving here trusts this Desktop session. Set approval.desktop: false for Telegram-only approval.',
  group: {
    pending: 'Waiting for your decision',
    approved: 'Approved',
    open: 'Open now',
    finished: 'Closed, expired or declined'
  },
  state: {
    pending: 'pending',
    approved: 'approved',
    open: 'open',
    waiting: 'approved, waiting for check-in',
    ended: 'ended',
    expired: 'expired',
    closed: 'closed',
    declined: 'declined',
    cancelled: 'withdrawn'
  },
  col: { state: 'State', window: 'Window', when: 'When', notes: 'Notes', actions: 'Actions' },
  exclusive: 'exclusive',
  shared: 'shared',
  scope: scope => `scope ${scope}`,
  overlaps: ids => `Overlaps ${ids}`,
  cards: ids => `Cards: ${ids}`,
  via: { telegram: 'Telegram', miniapp: 'the Mini App', desktop: 'Hermes Desktop' },
  decidedIn: channel => `decided in ${channel}`,
  time: {
    startsIn: d => `starts in ${d}`,
    left: d => `${d} left`,
    endedAgo: d => `ended ${d} ago`
  },
  dur: {
    soon: 'under a minute',
    minutes: m => `${m} min`,
    hours: (h, m) => (m ? `${h} h ${m} min` : `${h} h`),
    days: (d, h) => (h ? `${d} d ${h} h` : `${d} d`)
  },
  action: { approve: 'Approve', decline: 'Decline', close: 'Close now' },
  confirm: {
    approveTitle: name => `Approve "${name}"?`,
    approveBody: (scope, span) =>
      `Matched production writes in scope ${scope} are allowed during ${span}. Linked cards are released when it opens.`,
    declineTitle: name => `Decline "${name}"?`,
    declineBody: 'The proposal is closed. The agent has to propose a new window.',
    closeTitle: name => `Close "${name}" now?`,
    closeBody: scope => `Matched production writes in scope ${scope} are blocked again at once.`
  },
  done: { approve: 'Window approved.', decline: 'Window declined.', close: 'Window closed.' },
  failed: 'The action failed. Check the window list and retry.',
  audit: {
    decisions: 'Recent decisions',
    noDecisions: 'No decisions yet.',
    calls: 'Recent matched calls',
    callsHint: 'Last 20 · metadata only',
    noCalls: 'No matched calls recorded yet.',
    decision: (state, channel) => (channel ? `${state} in ${channel}` : state),
    by: who => `by ${who}`
  },
  chip: {
    open: (scope, d) => `${scope} open · ${d} left`,
    more: n => ` +${n}`,
    next: (scope, d) => `${scope} window in ${d}`,
    tip: 'Change windows'
  },
  badge: {
    open: d => `Open · ${d} left`,
    approved: d => `Window in ${d}`,
    waiting: 'Window: check-in',
    pending: 'Window proposed',
    pendingIn: d => `Proposed, in ${d}`,
    ended: 'Window ended',
    closed: 'Window closed',
    declined: 'Window declined',
    cancelled: 'Window withdrawn',
    expired: 'Proposal expired'
  }
}

// Set in register(): the plugin's REST door and its non-React translator.
let rest = null

// ── data ────────────────────────────────────────────────────────────────────

async function fetchWindows() {
  const data = await rest('/windows')
  // Times are compared on the server's clock: keep the offset to this machine's clock.
  return { ...data, skew: (Number(data.now) || 0) - Date.now() / 1000 }
}

const fetchAudit = () => rest('/audit')
const nowOf = data => Math.floor(Date.now() / 1000 + ((data && data.skew) || 0))

function useWindows() {
  return useQuery({ queryKey: WINDOWS_KEY, queryFn: fetchWindows, refetchInterval: POLL_MS })
}

/** What the window is right now, from its stored state and the clock. */
function phase(w, now) {
  if (w.state === 'pending') {
    return w.end_ts <= now ? 'expired' : 'pending'
  }

  if (w.state === 'approved' && w.closed_at == null) {
    if (w.open) {
      return 'open'
    }

    if (w.end_ts <= now) {
      return 'ended'
    }

    return w.start_ts <= now ? 'waiting' : 'approved'
  }

  return w.state
}

const GROUP_OF = { pending: 'pending', approved: 'approved', waiting: 'approved', open: 'open' }
const GROUPS = ['pending', 'approved', 'open', 'finished']
const groupOf = p => GROUP_OF[p] || 'finished'

function duration(t, seconds) {
  const s = Math.max(0, Math.floor(seconds))

  if (s < 60) {
    return t('dur.soon')
  }

  const minutes = Math.floor(s / 60)

  if (minutes < 60) {
    return t('dur.minutes', minutes)
  }

  const hours = Math.floor(minutes / 60)

  return hours < 48 ? t('dur.hours', hours, minutes % 60) : t('dur.days', Math.floor(hours / 24), hours % 24)
}

function relative(t, w, p, now) {
  if (p === 'open') {
    return t('time.left', duration(t, w.end_ts - now))
  }

  if (p === 'approved' || p === 'pending') {
    return w.start_ts > now ? t('time.startsIn', duration(t, w.start_ts - now)) : t('time.left', duration(t, w.end_ts - now))
  }

  if (p === 'ended' || p === 'expired') {
    return t('time.endedAgo', duration(t, now - w.end_ts))
  }

  return ''
}

/** The server's message from a failed ctx.rest call (`… 409: {"detail":"…"}`), else a fallback. */
function errorText(error, fallback) {
  const match = /(\d{3}):\s*(\{[\s\S]*\})\s*$/.exec(String((error && error.message) || ''))

  if (match) {
    try {
      const body = JSON.parse(match[2])
      const text = body.detail || body.error

      if (typeof text === 'string' && text) {
        return text
      }
    } catch {
      // not JSON: use the fallback
    }
  }

  return fallback
}

function newKey() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }

  let key = ''

  while (key.length < 32) {
    key += Math.floor(Math.random() * 16).toString(16)
  }

  return key
}

function parseList(value) {
  if (Array.isArray(value)) {
    return value
  }

  try {
    const parsed = JSON.parse(value)

    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

const clock = ts => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

// ── styles (theme variables only) ───────────────────────────────────────────

const C = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  tertiary: 'var(--ui-text-tertiary)',
  stroke: 'var(--ui-stroke-secondary)'
}

const S = {
  page: { height: '100%', overflowY: 'auto', color: C.primary },
  inner: { maxWidth: '72rem', margin: '0 auto', padding: '1.25rem 1rem 3rem', display: 'grid', gap: '1.5rem' },
  h1: { fontSize: '1.25rem', fontWeight: 600, margin: 0 },
  h2: { fontSize: '0.875rem', fontWeight: 600, margin: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' },
  hint: { fontSize: '0.8125rem', color: C.tertiary, margin: 0 },
  notice: { fontSize: '0.8125rem', color: C.secondary, border: `1px solid ${C.stroke}`, borderRadius: '8px', padding: '0.625rem 0.75rem' },
  section: { display: 'grid', gap: '0.625rem' },
  stack: { display: 'grid', gap: '0.625rem' },
  card: { border: `1px solid ${C.stroke}`, borderRadius: '10px', padding: '0.875rem', display: 'grid', gap: '0.5rem' },
  row: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.375rem' },
  name: { fontSize: '0.9375rem', fontWeight: 600, overflowWrap: 'anywhere' },
  meta: { fontSize: '0.8125rem', color: C.secondary, overflowWrap: 'anywhere' },
  small: { fontSize: '0.75rem', color: C.tertiary, overflowWrap: 'anywhere' },
  reason: { fontSize: '0.8125rem', color: C.secondary, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  actions: { display: 'flex', flexWrap: 'wrap', gap: '0.5rem' },
  tableWrap: { border: `1px solid ${C.stroke}`, borderRadius: '10px', overflowX: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: '0.8125rem' },
  th: { textAlign: 'left', fontWeight: 500, color: C.tertiary, padding: '0.5rem 0.75rem', borderBottom: `1px solid ${C.stroke}`, whiteSpace: 'nowrap' },
  td: { padding: '0.625rem 0.75rem', verticalAlign: 'top', borderBottom: `1px solid ${C.stroke}` },
  list: { listStyle: 'none', margin: 0, padding: 0, display: 'grid' },
  item: { padding: '0.5rem 0', borderBottom: `1px solid ${C.stroke}`, display: 'grid', gap: '0.125rem' },
  columns: narrow => ({ display: 'grid', gap: '1.5rem', gridTemplateColumns: narrow ? '1fr' : '1fr 1fr', alignItems: 'start' }),
  chip: { display: 'inline-flex', alignItems: 'center', gap: '0.25rem', height: '100%', padding: '0 0.375rem', fontSize: '0.6875rem', color: C.secondary, background: 'transparent', border: 0, cursor: 'pointer', whiteSpace: 'nowrap' }
}

/** jsx() for one child, jsxs() for several: the calls an uncompiled plugin writes by hand. */
const el = (type, props, ...children) =>
  children.length > 1 ? jsxs(type, { ...props, children }) : jsx(type, { ...props, children: children[0] })

const BADGE_VARIANT = { open: 'success', pending: 'warn', approved: 'default', waiting: 'warn' }

function StateBadge({ p }) {
  const t = usePluginI18n(ID)

  return jsx(Badge, { variant: BADGE_VARIANT[p] || 'muted', children: t(`state.${p}`) })
}

// ── the page ────────────────────────────────────────────────────────────────

const $wide = atom({ width: 0, height: 0, narrow: false }) // hosts without host.state.viewport

function Notes({ w }) {
  const t = usePluginI18n(ID)
  const cards = (w.cards || []).map(c => c && c.task_id).filter(Boolean)
  const parts = []

  if (w.overlaps && w.overlaps.length) {
    parts.push(jsx(Badge, { variant: 'warn', children: t('overlaps', w.overlaps.join(', ')) }, 'overlaps'))
  }

  if (cards.length) {
    parts.push(jsx(Badge, { variant: 'outline', children: t('cards', cards.join(', ')) }, 'cards'))
  }

  if (w.decided_via && t(`via.${w.decided_via}`) !== `via.${w.decided_via}`) {
    parts.push(jsx('span', { style: S.small, children: t('decidedIn', t(`via.${w.decided_via}`)) }, 'via'))
  }

  return parts.length ? jsx('div', { style: S.row, children: parts }) : null
}

function Actions({ w, p, approval, touch, ask }) {
  const t = usePluginI18n(ID)
  const size = touch ? 'touch' : 'default'
  const buttons = []

  if (p === 'pending' && approval) {
    buttons.push(
      jsx(Button, { size, onClick: () => ask(w, 'approve'), children: t('action.approve') }, 'approve'),
      jsx(Button, { size, variant: 'outline', onClick: () => ask(w, 'decline'), children: t('action.decline') }, 'decline')
    )
  }

  if (p === 'approved' || p === 'waiting' || p === 'open') {
    buttons.push(jsx(Button, { size, variant: 'outline', onClick: () => ask(w, 'close'), children: t('action.close') }, 'close'))
  }

  return buttons.length ? jsx('div', { style: S.actions, children: buttons }) : null
}

function Meta({ w }) {
  const t = usePluginI18n(ID)

  return jsx('div', { style: S.small, children: `${w.id} · ${t('scope', w.scope)} · ${t(w.exclusive ? 'exclusive' : 'shared')}` })
}

function WindowCard({ w, now, approval, ask }) {
  const t = usePluginI18n(ID)
  const p = phase(w, now)
  const rel = relative(t, w, p, now)

  return el(
    'div',
    { style: S.card },
    el('div', { style: S.row }, jsx(StateBadge, { p }), jsx('span', { style: S.name, children: w.name })),
    jsx(Meta, { w }),
    jsx('div', { style: S.meta, children: rel ? `${w.span} · ${rel}` : w.span }),
    jsx(Notes, { w }),
    w.reason ? jsx('div', { style: S.reason, children: w.reason }) : null,
    jsx(Actions, { w, p, approval, touch: true, ask })
  )
}

function WindowTable({ rows, now, approval, ask }) {
  const t = usePluginI18n(ID)
  const head = ['state', 'window', 'when', 'notes', 'actions'].map(c => jsx('th', { style: S.th, children: t(`col.${c}`) }, c))

  const body = rows.map(w => {
    const p = phase(w, now)
    const rel = relative(t, w, p, now)

    return el(
      'tr',
      { key: w.id },
      jsx('td', { style: S.td, children: jsx(StateBadge, { p }) }),
      el('td', { style: S.td }, jsx('div', { style: S.name, children: w.name }), jsx(Meta, { w })),
      el('td', { style: S.td }, jsx('div', { style: S.meta, children: w.span }), rel ? jsx('div', { style: S.small, children: rel }) : null),
      el('td', { style: S.td }, jsx(Notes, { w }), w.reason ? jsx('div', { style: S.reason, children: w.reason }) : null),
      jsx('td', { style: S.td, children: jsx(Actions, { w, p, approval, touch: false, ask }) })
    )
  })

  return jsx('div', {
    style: S.tableWrap,
    children: el('table', { style: S.table }, jsx('thead', { children: jsx('tr', { children: head }) }), jsx('tbody', { children: body }))
  })
}

function Group({ name, rows, now, approval, narrow, ask }) {
  const t = usePluginI18n(ID)

  return el(
    'section',
    { style: S.section },
    el('h2', { style: S.h2 }, t(`group.${name}`), jsx(Badge, { variant: 'muted', children: String(rows.length) })),
    narrow
      ? jsx('div', { style: S.stack, children: rows.map(w => jsx(WindowCard, { w, now, approval, ask }, w.id)) })
      : jsx(WindowTable, { rows, now, approval, ask })
  )
}

function AuditLists({ narrow }) {
  const t = usePluginI18n(ID)
  const { data } = useQuery({ queryKey: AUDIT_KEY, queryFn: fetchAudit, refetchInterval: POLL_MS })
  const decisions = (data && data.decisions) || []
  const calls = (data && data.audit) || []

  const decisionItems = decisions.map(d => {
    const channel = d.decided_via && t(`via.${d.decided_via}`) !== `via.${d.decided_via}` ? t(`via.${d.decided_via}`) : ''

    return el(
      'li',
      { key: `${d.window_id}:${d.decided_at}`, style: S.item },
      jsx('div', { style: S.meta, children: `${d.window_id} · ${d.name}` }),
      jsx('div', { style: S.small, children: `${clock(d.decided_at)} · ${t('audit.decision', t(`state.${d.state}`), channel)} · ${t('audit.by', d.decided_by)}` })
    )
  })

  const callItems = calls.map(row =>
    el(
      'li',
      { key: row.id, style: S.item },
      jsx('div', { style: S.meta, children: `${row.verdict} · ${row.tool}` }),
      jsx('div', {
        style: S.small,
        children: [clock(row.ts), parseList(row.scopes).join(', '), parseList(row.rule_ids).join(', '), row.window_id].filter(Boolean).join(' · ')
      })
    )
  )

  return el(
    'div',
    { style: S.columns(narrow) },
    el(
      'section',
      { style: S.section },
      jsx('h2', { style: S.h2, children: t('audit.decisions') }),
      decisionItems.length ? jsx('ul', { style: S.list, children: decisionItems }) : jsx('p', { style: S.hint, children: t('audit.noDecisions') })
    ),
    el(
      'section',
      { style: S.section },
      jsx('h2', { style: S.h2, children: t('audit.calls') }),
      jsx('p', { style: S.hint, children: t('audit.callsHint') }),
      callItems.length ? jsx('ul', { style: S.list, children: callItems }) : jsx('p', { style: S.hint, children: t('audit.noCalls') })
    )
  )
}

function ChangeWindowsPage() {
  const t = usePluginI18n(ID)
  const viewport = useValue((host.state && host.state.viewport) || $wide)
  const narrow = Boolean(viewport && viewport.narrow)
  const query = useWindows()
  const [pending, setPending] = useState(null) // { w, action, key } while a confirm dialog is open
  const data = query.data
  const now = nowOf(data)
  const approval = Boolean(data && data.approval === true)

  // One key per opened dialog: a retry inside the dialog repeats the same request.
  const ask = (w, action) => setPending({ w, action, key: newKey() })

  const confirm = async () => {
    const { w, action, key } = pending

    try {
      await rest(`/windows/${w.id}/${action}`, { method: 'POST', body: { key } })
    } catch (error) {
      void queryClient.invalidateQueries({ queryKey: [ID] })
      throw new Error(errorText(error, t('failed')))
    }

    host.notify({ kind: 'success', message: t(`done.${action}`) })
    await queryClient.invalidateQueries({ queryKey: [ID] })
  }

  let content

  if (!data) {
    content = query.isError
      ? jsx(ErrorState, {
          title: t('loadFailed'),
          description: errorText(query.error, t('loadFailedHint')),
          children: jsx(Button, { size: 'touch', variant: 'outline', onClick: () => void query.refetch(), children: t('retry') })
        })
      : jsx('p', { style: S.hint, children: t('loading') })
  } else {
    const byGroup = { pending: [], approved: [], open: [], finished: [] }

    for (const w of data.windows || []) {
      byGroup[groupOf(phase(w, now))].push(w)
    }

    const groups = GROUPS.filter(name => byGroup[name].length).map(name =>
      jsx(Group, { name, rows: byGroup[name], now, approval, narrow, ask }, name)
    )

    content = groups.length ? groups : jsx(EmptyState, { title: t('empty'), description: t('emptyHint') })
  }

  const dialog = pending
    ? jsx(ConfirmDialog, {
        open: true,
        onClose: () => setPending(null),
        onConfirm: confirm,
        title: t(`confirm.${pending.action}Title`, pending.w.name),
        description:
          pending.action === 'approve'
            ? t('confirm.approveBody', pending.w.scope, pending.w.span)
            : pending.action === 'close'
              ? t('confirm.closeBody', pending.w.scope)
              : t('confirm.declineBody'),
        confirmLabel: t(`action.${pending.action}`),
        destructive: pending.action !== 'approve'
      })
    : null

  return jsx('div', {
    style: S.page,
    children: el(
      'div',
      { style: S.inner },
      el('header', { style: S.section }, jsx('h1', { style: S.h1, children: t('title') }), jsx('p', { style: S.hint, children: t('subtitle') })),
      data && !approval ? jsx('div', { style: S.notice, role: 'status', children: t('approvalOff') }) : null,
      data && approval ? jsx('p', { style: S.hint, children: t('trust') }) : null,
      data && query.isError ? jsx('div', { style: S.notice, role: 'status', children: t('stale') }) : null,
      content,
      data ? jsx(AuditLists, { narrow }) : null,
      dialog
    )
  })
}

// ── status-bar chip ─────────────────────────────────────────────────────────

/** The open window that ends first (and how many more are open), else the next approved one. */
function headline(data) {
  const now = nowOf(data)
  const rows = (data && data.windows) || []
  const open = rows.filter(w => phase(w, now) === 'open').sort((a, b) => a.end_ts - b.end_ts)

  if (open.length) {
    return { kind: 'open', w: open[0], more: open.length - 1, seconds: open[0].end_ts - now }
  }

  const next = rows.filter(w => phase(w, now) === 'approved').sort((a, b) => a.start_ts - b.start_ts)[0]

  return next ? { kind: 'next', w: next, more: 0, seconds: next.start_ts - now } : null
}

function StatusChip() {
  const t = usePluginI18n(ID)
  const { data } = useWindows()
  const head = headline(data)

  if (!head) {
    return null
  }

  const label =
    head.kind === 'open'
      ? t('chip.open', head.w.scope, duration(t, head.seconds)) + (head.more ? t('chip.more', head.more) : '')
      : t('chip.next', head.w.scope, duration(t, head.seconds))

  return jsx(Tip, {
    label: `${t('chip.tip')}: ${head.w.id} · ${head.w.name} · ${head.w.span}`,
    children: el(
      'button',
      { type: 'button', style: S.chip, onClick: () => host.navigate(PATH) },
      jsx(Codicon, { name: head.kind === 'open' ? 'unlock' : 'clock', size: 12 }),
      jsx('span', { children: label })
    )
  })
}

// ── Kanban card badges ──────────────────────────────────────────────────────

const BADGE_RANK = ['open', 'waiting', 'approved', 'pending', 'ended', 'closed', 'declined', 'expired', 'cancelled']
const BADGE_TONE = { open: 'green', waiting: 'yellow', approved: 'blue', pending: 'yellow' }

/** task id → one badge, for the card's most relevant window (an open one wins over a finished one). */
function cardBadges(data, t) {
  const now = nowOf(data)
  const best = {}

  for (const w of (data && data.windows) || []) {
    const p = phase(w, now)
    const rank = BADGE_RANK.indexOf(p)

    for (const card of w.cards || []) {
      const id = card && card.task_id

      if (id && rank >= 0 && (!best[id] || rank < best[id].rank || (rank === best[id].rank && w.start_ts < best[id].w.start_ts))) {
        best[id] = { rank, p, w }
      }
    }
  }

  const badges = {}

  for (const [id, { p, w }] of Object.entries(best)) {
    let label = t(`badge.${p}`)

    if (p === 'open') {
      label = t('badge.open', duration(t, w.end_ts - now))
    } else if (p === 'approved') {
      label = t('badge.approved', duration(t, w.start_ts - now))
    } else if (p === 'pending' && w.start_ts > now) {
      label = t('badge.pendingIn', duration(t, w.start_ts - now))
    }

    badges[id] = { label, tone: BADGE_TONE[p] || 'neutral', tooltip: `${w.id} · ${w.name} · ${w.span}` }
  }

  return badges
}

// ── the plugin ──────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Change windows',
  register(ctx) {
    rest = ctx.rest
    ctx.i18n.register({ en: EN })

    const t = ctx.i18n.t

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, title: t('title'), data: { path: PATH }, render: () => jsx(ChangeWindowsPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: t('title'), codicon: 'calendar' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: `${ID}.open`, label: t('open'), keywords: ['change', 'window', 'approve', 'maintenance'], run: () => host.navigate(PATH) }
      }
    ])

    // Card badges are a pure lookup, so they are rebuilt from each poll result (the status-bar
    // chip and the page keep the query polling) and registered again only when a label changes.
    let painted = ''

    const paint = data => {
      const badges = cardBadges(data, t)
      const signature = JSON.stringify(badges)

      if (signature === painted) {
        return
      }

      painted = signature
      ctx.register({ id: 'window', area: KANBAN_CARD_AREAS.badges, data: { decorate: card => badges[card.id] || null } })
    }

    ctx.onDispose(
      queryClient.getQueryCache().subscribe(event => {
        const key = event && event.query && event.query.queryKey

        if (event.type === 'updated' && Array.isArray(key) && key[0] === ID && key[1] === 'windows' && event.query.state.data) {
          paint(event.query.state.data)
        }
      })
    )

    queryClient.prefetchQuery({ queryKey: WINDOWS_KEY, queryFn: fetchWindows }).catch(() => {})
  }
}
