/**
 * change-windows: the Hermes Desktop half.
 *
 * One question on every surface: is the door open now, and until when?
 *
 * - Page `/change-windows` with a sidebar row:
 *     the door      one large sentence with a lock glyph (closed / open with time left / next);
 *     the runway    today and tomorrow as an SVG timeline (a vertical ladder when narrow): approved
 *                   windows solid, proposals hatched, overlaps as a warning band, night shaded, a
 *                   "now" needle. Focus or hover reads a bar out; a click jumps to its card;
 *     window cards  name, scope, local time and relative time, proposer, linked Kanban cards, a
 *                   decision stamp, and each overlap as a button that opens a conflict panel;
 *     the ledger    decisions and matched calls (metadata only) as one quiet log.
 *   Approve / Decline / Close now, each behind a confirm dialog and an idempotency key.
 * - Status-bar chip and Kanban card badges, in the door's words.
 * - Palette command that opens the page.
 *
 * Plain ESM, loaded uncompiled: jsx() calls, no JSX. Imports only @hermes/plugin-sdk, react and
 * react/jsx-runtime. Data comes from the plugin's own backend (dashboard/plugin_api.py) by
 * polling; no socket. Layout is inline styles on the app's theme variables, because an uncompiled
 * plugin cannot add utility classes to the app's stylesheet. Colour never carries meaning alone:
 * every state also has a word, a glyph or a pattern.
 */

import {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  CopyButton,
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
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'change-windows'
const PATH = '/change-windows'
const BOARD_PATH = '/kanban'
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
  door: {
    label: 'Door',
    closed: 'Closed: production writes are blocked',
    warn: 'Warn mode: production writes are recorded, not blocked',
    off: 'Guard off: production writes are not checked',
    open: (name, d) => `Open: ${name}, ${d} left`,
    openSub: (scope, until) => `Matched writes in scope ${scope} are allowed until ${until}. Other scopes stay blocked.`,
    more: n => (n === 1 ? '1 more window is open.' : `${n} more windows are open.`),
    next: (at, name) => `Next: ${at} ${name} (approved)`,
    nextIn: d => `in ${d}`,
    none: 'No approved window ahead.',
    waiting: n => (n === 1 ? '1 proposal waits for your decision.' : `${n} proposals wait for your decision.`)
  },
  runway: {
    label: 'Runway: change windows today and tomorrow',
    today: day => (day ? `Today · ${day}` : 'Today'),
    tomorrow: day => (day ? `Tomorrow · ${day}` : 'Tomorrow'),
    now: at => `now ${at}`,
    night: 'night',
    overlap: length => `overlap ${length}`,
    overlapLabel: (a, b, span, length) => `Overlap: ${a} and ${b}, ${span}, ${length}`,
    bar: (name, state, span, rel) => [name, state, span, rel].filter(Boolean).join(', '),
    hint: 'Focus or hover a bar to read it. Select it to jump to its card.',
    readout: parts => parts.filter(Boolean).join(' · '),
    legend: {
      approved: 'approved',
      pending: 'proposed',
      open: 'open now',
      past: 'ended',
      overlap: 'overlap',
      night: 'night',
      now: 'now'
    }
  },
  group: {
    open: 'Open now',
    pending: 'Waiting for your decision',
    approved: 'Approved',
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
  exclusive: 'exclusive',
  shared: 'shared (other windows may overlap)',
  scope: scope => `scope ${scope}`,
  proposedBy: who => `Proposed by ${who}`,
  copyId: 'Copy the window id',
  card: {
    open: id => `Open the Kanban board (card ${id})`,
    detail: (title, status, assignee) => [title, status, assignee].filter(Boolean).join(' · ')
  },
  via: { telegram: 'Telegram', miniapp: 'the Mini App', desktop: 'Hermes Desktop' },
  stamp: {
    approved: (channel, at) => (channel ? `Approved in ${channel}, ${at}` : `Approved, ${at}`),
    declined: (channel, at) => (channel ? `Declined in ${channel}, ${at}` : `Declined, ${at}`),
    closed: at => (at ? `Closed ${at}` : 'Closed'),
    cancelled: at => (at ? `Withdrawn ${at}` : 'Withdrawn'),
    pending: 'Awaiting your decision',
    expired: 'Expired without a decision'
  },
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
  overlap: {
    chip: (name, span, length) => `Overlaps '${name}' ${span}, ${length}`,
    chipId: id => `Overlaps ${id}`,
    title: name => `This window and '${name}' overlap`,
    other: 'Other window',
    span: 'Shared span',
    spanValue: (span, length) => `${span}, ${length}`,
    rule: 'Rule',
    means: 'What it means',
    wayOut: 'Ways out',
    otherValue: parts => parts.filter(Boolean).join(' · '),
    show: name => `Show '${name}'`,
    dismiss: 'Close explanation'
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
  ledger: {
    title: 'Ledger',
    hint: 'Decisions and matched calls, newest first. Last 20 of each · metadata only',
    empty: 'Nothing recorded yet.',
    decision: (verdict, channel) => (channel ? `${verdict} in ${channel}` : verdict),
    closedLater: 'closed later',
    by: who => `by ${who}`,
    row: parts => parts.filter(Boolean).join(' · ')
  },
  chip: {
    closed: 'closed',
    warn: 'warn only',
    off: 'guard off',
    open: d => `open, ${d}`,
    more: n => ` +${n}`,
    next: at => `next ${at}`,
    tip: sentence => `Change windows. ${sentence}`
  },
  badge: {
    open: d => `window open, ${d}`,
    approved: at => `next ${at}`,
    waiting: 'check-in needed',
    pending: at => `pending ${at}`,
    pendingNow: 'pending decision',
    ended: 'window ended',
    closed: 'window closed',
    declined: 'window declined',
    cancelled: 'window withdrawn',
    expired: 'proposal expired'
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
const GROUPS = ['open', 'pending', 'approved', 'finished']
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

/** Fallback for a backend that sends no formatted moment: the app's own locale and zone. */
const clock = ts => new Date(ts * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })

// ── days and clock times (the plugin's time zone, as the backend sends it) ──

/** Today and tomorrow, midnight to midnight. An older backend sends only `start` (today's midnight). */
function daysOf(data) {
  if (data && Array.isArray(data.days) && data.days.length) {
    return data.days
  }

  const start = Number(data && data.start)

  return start ? [0, 1].map(i => ({ start: start + i * 86400, end: start + (i + 1) * 86400, label: '' })) : []
}

const two = n => String(n).padStart(2, '0')

/** `14:05` for a moment inside `day`. */
function hm(day, ts) {
  const minutes = Math.floor((((ts - day.start) / (day.end - day.start)) * 1440))
  const clamped = Math.max(0, Math.min(1440, minutes))

  return `${two(Math.floor(clamped / 60) % 24)}:${two(clamped % 60)}`
}

/** A backend moment (`Tue 10 Nov 14:00`) without the day when the day is today. */
function short(when, data) {
  const today = daysOf(data)[0]
  const prefix = today && today.label ? `${today.label} ` : ''

  return prefix && String(when).startsWith(prefix) ? String(when).slice(prefix.length) : String(when || '')
}

/** When a window starts, as the door says it: `14:00` today, `Wed 11 Nov 09:00` later. */
const startOf = (w, data) => (w.starts ? short(w.starts, data) : clock(w.start_ts))

// ── styles (theme variables only) ───────────────────────────────────────────

const C = {
  primary: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  stroke: 'var(--ui-stroke-secondary)',
  accent: 'var(--ui-accent)',
  green: 'var(--ui-green)',
  yellow: 'var(--ui-yellow)',
  red: 'var(--ui-red)',
  hatch: 'url(#cw-hatch)',
  hatchWarn: 'url(#cw-hatch-warn)',
  none: 'none',
  clear: 'transparent'
}

const MONO = 'var(--dt-font-mono, ui-monospace, monospace)'

const S = {
  page: { height: '100%', overflowY: 'auto', color: C.primary },
  inner: { maxWidth: '72rem', margin: '0 auto', padding: '1.25rem 1rem 3rem', display: 'grid', gap: '1.5rem' },
  h1: { fontSize: '0.8125rem', fontWeight: 600, margin: 0, color: C.primary },
  h2: { fontSize: '0.875rem', fontWeight: 600, margin: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' },
  hint: { fontSize: '0.8125rem', color: C.primary, margin: 0 },
  notice: { fontSize: '0.8125rem', color: C.primary, border: `1px solid ${C.stroke}`, borderRadius: '8px', padding: '0.625rem 0.75rem' },
  section: { display: 'grid', gap: '0.625rem' },
  hero: { border: `1px solid ${C.stroke}`, borderRadius: '12px', padding: '1rem', display: 'grid', gap: '1rem', minWidth: 0 },
  door: { display: 'flex', alignItems: 'flex-start', gap: '0.875rem', minWidth: 0 },
  doorGlyph: {
    flex: '0 0 auto',
    width: '3rem',
    height: '3rem',
    borderRadius: '12px',
    display: 'grid',
    placeItems: 'center'
  },
  doorTone: {
    open: { color: C.green, border: `2px solid ${C.green}` },
    closed: { color: C.secondary, border: `2px solid ${C.secondary}` },
    warn: { color: C.primary, border: `2px dashed ${C.primary}` }
  },
  doorText: { display: 'grid', gap: '0.25rem', minWidth: 0 },
  doorTitle: { fontSize: 'clamp(1.125rem, 0.75rem + 2vw, 1.5rem)', lineHeight: 1.2, fontWeight: 650, letterSpacing: '-0.01em', margin: 0, overflowWrap: 'anywhere' },
  doorSub: { fontSize: '0.875rem', color: C.primary, margin: 0, overflowWrap: 'anywhere' },
  runway: { display: 'grid', gap: '0.375rem', minWidth: 0 },
  svg: { display: 'block', width: '100%', height: 'auto', overflow: 'visible' },
  readout: { fontSize: '0.75rem', color: C.primary, minHeight: '1.125rem', margin: 0, overflowWrap: 'anywhere' },
  legend: { display: 'flex', flexWrap: 'wrap', gap: '0.25rem 0.875rem', fontSize: '0.6875rem', color: C.primary, margin: 0, padding: 0, listStyle: 'none' },
  legendItem: { display: 'inline-flex', alignItems: 'center', gap: '0.3125rem' },
  cards: { display: 'grid', gap: '0.75rem', gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 24rem), 1fr))', alignItems: 'start' },
  card: { border: `1px solid ${C.stroke}`, borderRadius: '10px', padding: '0.875rem', display: 'grid', gap: '0.5rem', minWidth: 0, outline: 'none' },
  cardLit: { border: `1px solid ${C.primary}`, boxShadow: `0 0 0 1px ${C.primary}` },
  cardWide: { gridColumn: '1 / -1' },
  row: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.375rem', minWidth: 0 },
  head: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '0.5rem', minWidth: 0 },
  name: { fontSize: '0.9375rem', fontWeight: 600, overflowWrap: 'anywhere', flex: '1 1 8rem', minWidth: 0 },
  meta: { fontSize: '0.8125rem', color: C.primary, overflowWrap: 'anywhere' },
  small: { fontSize: '0.75rem', color: C.primary, overflowWrap: 'anywhere' },
  id: { fontFamily: MONO, fontSize: '0.6875rem', color: C.primary, userSelect: 'all' },
  reason: { fontSize: '0.8125rem', color: C.primary, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' },
  foot: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: '0.625rem', marginTop: '0.125rem' },
  actions: { display: 'flex', flexWrap: 'wrap', gap: '0.5rem' },
  stamp: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '0.375rem',
    padding: '0.25rem 0.5rem',
    border: `3px double ${C.secondary}`,
    borderRadius: '4px',
    color: C.primary,
    fontFamily: MONO,
    fontSize: '0.6875rem',
    lineHeight: 1.3,
    letterSpacing: '0.05em',
    textTransform: 'uppercase',
    transform: 'rotate(-1.5deg)',
    overflowWrap: 'anywhere'
  },
  stampIdle: { border: `1px dashed ${C.secondary}`, transform: 'none' },
  link: {
    display: 'inline-flex',
    alignItems: 'baseline',
    gap: '0.375rem',
    maxWidth: '100%',
    padding: '0.1875rem 0.4375rem',
    border: `1px solid ${C.secondary}`,
    borderRadius: '4px',
    background: C.clear,
    color: C.primary,
    font: 'inherit',
    fontSize: '0.75rem',
    lineHeight: 1.35,
    textAlign: 'left',
    cursor: 'pointer',
    overflowWrap: 'anywhere'
  },
  overlapChip: { borderStyle: 'dashed' },
  panel: { border: `1px solid ${C.secondary}`, borderLeft: `4px solid ${C.yellow}`, borderRadius: '8px', padding: '0.75rem', display: 'grid', gap: '0.625rem', minWidth: 0 },
  panelTitle: { fontSize: '0.875rem', fontWeight: 600, margin: 0, display: 'flex', alignItems: 'center', gap: '0.375rem', overflowWrap: 'anywhere' },
  facts: { display: 'grid', gridTemplateColumns: 'minmax(5.5rem, max-content) 1fr', gap: '0.3125rem 0.75rem', margin: 0, fontSize: '0.8125rem' },
  term: { color: C.primary, margin: 0 },
  fact: { margin: 0, color: C.primary, overflowWrap: 'anywhere', minWidth: 0 },
  ledger: { listStyle: 'none', margin: 0, padding: '0.375rem 0 0', display: 'grid', borderTop: `1px solid ${C.stroke}` },
  entry: { display: 'flex', alignItems: 'baseline', gap: '0.625rem', padding: '0.25rem 0', fontSize: '0.75rem', minWidth: 0 },
  moment: { flex: '0 0 auto', width: '21ch', fontFamily: MONO, color: C.primary, whiteSpace: 'nowrap' },
  glyph: { flex: '0 0 auto', width: '1rem', textAlign: 'center' },
  glyphTone: { blocked: { color: C.red }, warned: { color: C.yellow }, allowed: { color: C.green }, plain: { color: C.primary } },
  line: { flex: '1 1 auto', minWidth: 0, color: C.primary, overflowWrap: 'anywhere' },
  chip: { display: 'inline-flex', alignItems: 'center', gap: '0.25rem', height: '100%', padding: '0 0.375rem', fontSize: '0.6875rem', color: C.primary, background: C.clear, border: 0, cursor: 'pointer', whiteSpace: 'nowrap' }
}

/** jsx() for one child, jsxs() for several: the calls an uncompiled plugin writes by hand. */
const el = (type, props, ...children) =>
  children.length > 1 ? jsxs(type, { ...props, children }) : jsx(type, { ...props, children: children[0] })

const BADGE_VARIANT = { open: 'success', pending: 'warn', approved: 'default', waiting: 'warn' }

function StateBadge({ p }) {
  const t = usePluginI18n(ID)

  return jsx(Badge, { variant: BADGE_VARIANT[p] || 'muted', children: t(`state.${p}`) })
}

// ── the door ────────────────────────────────────────────────────────────────

/** The open window that ends first (and how many more are open), else the next approved one. */
function headline(data) {
  const now = nowOf(data)
  const rows = (data && data.windows) || []
  const open = rows.filter(w => phase(w, now) === 'open').sort((a, b) => a.end_ts - b.end_ts)

  if (open.length) {
    return { kind: 'open', w: open[0], more: open.length - 1, seconds: open[0].end_ts - now }
  }

  const next = rows.filter(w => phase(w, now) === 'approved').sort((a, b) => a.start_ts - b.start_ts)[0]

  return next ? { kind: 'next', w: next, more: 0, seconds: next.start_ts - now } : { kind: 'closed', w: null, more: 0, seconds: 0 }
}

const guardMode = data => (data && (data.mode === 'warn' || data.mode === 'off') ? data.mode : 'enforce')

/** The door as words: one sentence, then what follows from it. The chip and its tip reuse this. */
function doorOf(data, t) {
  const now = nowOf(data)
  const head = headline(data)
  const mode = guardMode(data)
  const waiting = ((data && data.windows) || []).filter(w => phase(w, now) === 'pending').length
  const lines = []
  let title = t(mode === 'enforce' ? 'door.closed' : `door.${mode}`)
  let tone = mode === 'enforce' ? 'closed' : 'warn'
  let icon = mode === 'enforce' ? 'lock' : 'warning'

  if (head.kind === 'open') {
    const today = daysOf(data)[0]
    const until = !today ? clock(head.w.end_ts) : head.w.end_ts <= today.end ? hm(today, head.w.end_ts) : head.w.span

    if (mode === 'enforce') {
      title = t('door.open', head.w.name, duration(t, head.seconds))
      tone = 'open'
      icon = 'unlock'
    }

    lines.push(t('door.openSub', head.w.scope, until))

    if (head.more) {
      lines.push(t('door.more', head.more))
    }
  } else if (head.kind === 'next') {
    lines.push(`${t('door.next', startOf(head.w, data), head.w.name)}, ${t('door.nextIn', duration(t, head.seconds))}.`)
  } else {
    lines.push(t('door.none'))
  }

  if (waiting) {
    lines.push(t('door.waiting', waiting))
  }

  return { head, mode, title, tone, icon, lines }
}

function Door({ data }) {
  const t = usePluginI18n(ID)
  const door = doorOf(data, t)

  return el(
    'div',
    { style: S.door, role: 'status', 'aria-label': t('door.label'), 'data-door': door.head.kind, 'data-mode': door.mode },
    jsx('div', { style: { ...S.doorGlyph, ...S.doorTone[door.tone] }, children: jsx(Codicon, { name: door.icon, size: 24 }) }),
    el(
      'div',
      { style: S.doorText },
      jsx('p', { style: S.doorTitle, children: door.title }),
      jsx('p', { style: S.doorSub, children: door.lines.join(' ') })
    )
  )
}

// ── the runway ──────────────────────────────────────────────────────────────

const NIGHT = [[0, 6], [22, 24]] // hours of the quiet band
const DRAWN = { open: 'open', approved: 'approved', waiting: 'approved', pending: 'pending', ended: 'past', closed: 'past' }
const BAR_PAINT = {
  open: { fill: C.green, stroke: C.primary, strokeWidth: 2 },
  approved: { fill: C.secondary, stroke: C.secondary, strokeWidth: 1 },
  pending: { fill: C.hatch, stroke: C.secondary, strokeWidth: 1.5, strokeDasharray: '4 3' },
  past: { fill: C.stroke, stroke: C.secondary, strokeWidth: 1 }
}
// Opening a window is one short fill of its bar. The app turns transitions off for reduced motion.
const BAR_MOTION = { transition: 'fill 200ms ease-out, stroke 200ms ease-out' }
const cut = (text, max) => (text.length > max ? `${text.slice(0, Math.max(1, max - 1)).trimEnd()}…` : text)

/** The windows that have a bar inside `day`, clipped to it, earliest first. */
function barsOf(data, now, day) {
  const bars = []

  for (const w of (data && data.windows) || []) {
    const p = phase(w, now)
    const kind = DRAWN[p]
    const end = p === 'closed' && w.closed_at ? Math.min(w.end_ts, w.closed_at) : w.end_ts
    const from = Math.max(w.start_ts, day.start)
    const to = Math.min(end, day.end)

    if (kind && to > from) {
      bars.push({ w, p, kind, from, to })
    }
  }

  return bars.sort((a, b) => a.from - b.from || (a.w.id < b.w.id ? -1 : 1))
}

/** Each overlapping pair once, clipped to `day`. */
function bandsOf(data, day) {
  const bands = []
  const seen = {}

  for (const w of (data && data.windows) || []) {
    for (const c of w.conflicts || []) {
      const key = [w.id, c.id].sort().join(':')
      const from = Math.max(c.start_ts, day.start)
      const to = Math.min(c.end_ts, day.end)

      if (!seen[key] && to > from) {
        seen[key] = true
        bands.push({ key, ids: [w.id, c.id], names: [w.name, c.name], from, to, span: c.overlap, length: c.length })
      }
    }
  }

  return bands
}

/** Greedy lanes: each item takes the first lane that is free at its start. Returns the lane count. */
function pack(items, extent) {
  const ends = []

  for (const item of items) {
    const [from, to] = extent(item)
    let lane = ends.findIndex(end => end <= from)

    if (lane < 0) {
      lane = ends.length
      ends.push(to)
    } else {
      ends[lane] = to
    }

    item.lane = lane
  }

  return Math.max(1, ends.length)
}

const text = (props, children) => jsx('text', { ...props, children })
const svgText = (size, tone, extra) => ({ fontSize: size, fill: C[tone], fontFamily: 'inherit', ...extra })

function Defs() {
  const line = tone => jsx('line', { x1: 0, y1: 0, x2: 0, y2: 6, style: { stroke: C[tone], strokeWidth: 2 } })

  return el(
    'defs',
    {},
    jsx('pattern', { id: 'cw-hatch', patternUnits: 'userSpaceOnUse', width: 6, height: 6, patternTransform: 'rotate(45)', children: line('secondary') }),
    jsx('pattern', { id: 'cw-hatch-warn', patternUnits: 'userSpaceOnUse', width: 7, height: 6, patternTransform: 'rotate(-45)', children: line('yellow') })
  )
}

/** One bar: a focusable button with its own focus ring (drawn, so it shows in every theme). */
function Bar({ bar, box, label, labelAt, view }) {
  const t = usePluginI18n(ID)
  const { w, p, kind } = bar
  const lit = view.active === w.id || Boolean(view.mark && view.mark.ids.includes(w.id))
  const dim = Boolean(view.mark) && !view.mark.ids.includes(w.id)
  const rel = relative(t, w, p, view.now)
  const pick = () => view.pick(w.id)

  return el(
    'g',
    {
      role: 'button',
      tabIndex: 0,
      'aria-label': t('runway.bar', w.name, t(`state.${p}`), w.span, rel),
      'data-bar': w.id,
      'data-kind': kind,
      'data-lit': lit ? 'yes' : 'no',
      style: { cursor: 'pointer', outline: 'none', opacity: dim ? 0.35 : 1 },
      onClick: pick,
      onKeyDown: event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          pick()
        }
      },
      onFocus: () => view.hover(w.id),
      onBlur: () => view.hover(null),
      onMouseEnter: () => view.hover(w.id),
      onMouseLeave: () => view.hover(null)
    },
    lit
      ? jsx('rect', { x: box.x - 3, y: box.y - 3, width: box.width + 6, height: box.height + 6, rx: 5, style: { fill: C.none, stroke: C.primary, strokeWidth: 2 } })
      : null,
    jsx('rect', { ...box, rx: 3, style: { ...BAR_PAINT[kind], ...BAR_MOTION } }),
    label
      ? text({ ...labelAt, style: svgText(11, 'primary', { fontWeight: kind === 'open' ? 700 : 500 }) }, label)
      : null
  )
}

const DAY_HEAD = 18 // the day label
const TICKS = 16 // hour labels
const LANE = 22
const STRIP = 16 // under the lanes: overlap and night labels
const CHAR = 6.9 // width of one 11px label character (a monospace theme is the widest), for lane packing

function WideDay({ day, index, top, width, view, data }) {
  const t = usePluginI18n(ID)
  const x = ts => ((ts - day.start) / (day.end - day.start)) * width
  const bars = barsOf(data, view.now, day)

  for (const bar of bars) {
    const name = cut(bar.w.name, 30)
    const x0 = x(bar.from)
    const barWidth = Math.max(5, x(bar.to) - x0)
    const room = name.length * CHAR + 10

    bar.name = name
    bar.box = { x: x0, width: barWidth }
    bar.flip = x0 + barWidth + room > width && x0 - room >= 0
    bar.extent = bar.flip ? [x0 - room, x0 + barWidth + 4] : [x0, x0 + barWidth + room]
  }

  const lanes = pack(bars, bar => bar.extent)
  const trackTop = top + DAY_HEAD + TICKS
  const trackHeight = lanes * LANE + 6 + STRIP
  const today = view.now >= day.start && view.now < day.end
  const needle = today ? x(view.now) : -100
  const parts = []

  parts.push(text({ key: 'day', x: 0, y: top + 12, style: svgText(12, 'primary', { fontWeight: 600 }) }, t(index ? 'runway.tomorrow' : 'runway.today', day.label)))
  parts.push(jsx('rect', { key: 'track', x: 0, y: trackTop, width, height: trackHeight, rx: 6, style: { fill: C.none, stroke: C.stroke } }))

  for (const [from, to] of NIGHT) {
    parts.push(jsx('rect', { key: `night${from}`, 'data-band': 'night', x: (from / 24) * width, y: trackTop, width: ((to - from) / 24) * width, height: trackHeight, style: { fill: C.primary, fillOpacity: 0.07 } }))
  }

  parts.push(text({ key: 'nightLabel', x: 5, y: trackTop + trackHeight - 5, style: svgText(10, 'primary') }, t('runway.night')))

  const step = width / 24 >= 22 ? 3 : 6

  for (let hour = 0; hour <= 24; hour += 1) {
    const at = (hour / 24) * width

    parts.push(jsx('line', { key: `tick${hour}`, x1: at, x2: at, y1: trackTop - (hour % step ? 3 : 5), y2: trackTop, style: { stroke: C.secondary } }))

    if (hour % step === 0 && Math.abs(at - needle) > 44) {
      parts.push(text({ key: `hour${hour}`, x: at, y: trackTop - 7, textAnchor: hour === 0 ? 'start' : hour === 24 ? 'end' : 'middle', style: svgText(10, 'primary', { fontFamily: MONO }) }, two(hour)))
    }
  }

  for (const band of bandsOf(data, day)) {
    const x0 = x(band.from)
    const bandWidth = Math.max(4, x(band.to) - x0)
    const strong = Boolean(view.mark && band.ids.every(id => view.mark.ids.includes(id)))
    const right = x0 + 96 > width
    // The band covers the lanes of its two windows, so it does not run over other labels.
    const own = bars.filter(bar => band.ids.includes(bar.w.id)).map(bar => bar.lane)
    const firstLane = own.length ? Math.min(...own) : 0
    const lastLane = own.length ? Math.max(...own) : lanes - 1
    const bandTop = trackTop + 2 + firstLane * LANE
    const bandHeight = (lastLane - firstLane + 1) * LANE - 2

    parts.push(
      el(
        'g',
        { key: `band${band.key}`, role: 'img', 'aria-label': t('runway.overlapLabel', band.names[0], band.names[1], band.span, band.length), 'data-band': 'overlap', 'data-strong': strong ? 'yes' : 'no' },
        jsx('rect', { x: x0, y: bandTop, width: bandWidth, height: bandHeight, style: { fill: C.hatchWarn, fillOpacity: strong ? 1 : 0.7, stroke: C.secondary, strokeWidth: strong ? 2 : 1, strokeDasharray: strong ? undefined : '3 3' } }),
        jsx('line', { x1: x0, x2: x0, y1: bandTop + bandHeight, y2: trackTop + trackHeight - 14, style: { stroke: C.secondary, strokeDasharray: '2 2' } }),
        text({ x: right ? x0 + bandWidth : x0, y: trackTop + trackHeight - 5, textAnchor: right ? 'end' : 'start', style: svgText(10, 'primary', { fontWeight: 600 }) }, `⚠ ${t('runway.overlap', band.length)}`)
      )
    )
  }

  for (const bar of bars) {
    const y = trackTop + 5 + bar.lane * LANE

    parts.push(
      jsx(Bar, {
        bar,
        view,
        box: bar.kind === 'open' ? { ...bar.box, y: y - 2, height: 18 } : { ...bar.box, y, height: 14 },
        label: bar.name,
        labelAt: { x: bar.flip ? bar.box.x - 5 : bar.box.x + bar.box.width + 5, y: y + 11, textAnchor: bar.flip ? 'end' : 'start' }
      }, `bar${bar.w.id}`)
    )
  }

  if (today) {
    const labelX = Math.max(30, Math.min(width - 30, needle))

    parts.push(
      el(
        'g',
        { key: 'needle', 'data-needle': hm(day, view.now), 'aria-hidden': true },
        jsx('line', { x1: needle, x2: needle, y1: trackTop - 4, y2: trackTop + trackHeight, style: { stroke: C.primary, strokeWidth: 1.5 } }),
        jsx('path', { d: `M ${needle - 4} ${trackTop - 5} L ${needle + 4} ${trackTop - 5} L ${needle} ${trackTop + 1} Z`, style: { fill: C.accent, stroke: C.primary, strokeWidth: 1 } }),
        text({ x: labelX, y: trackTop - 8, textAnchor: 'middle', style: svgText(10, 'primary', { fontWeight: 700, fontFamily: MONO }) }, t('runway.now', hm(day, view.now)))
      )
    )
  }

  return { height: DAY_HEAD + TICKS + trackHeight, node: jsx('g', { 'data-day': index, children: parts }, `day${index}`) }
}

const HOUR = 22 // ladder: one hour
const TIME_COL = 44

function LadderDay({ day, index, top, width, view, data }) {
  const t = usePluginI18n(ID)
  const bars = barsOf(data, view.now, day)
  const today = view.now >= day.start && view.now < day.end
  const hourOf = ts => ((ts - day.start) / (day.end - day.start)) * 24
  const marks = bars.map(bar => hourOf(bar.from)).concat(today ? [hourOf(view.now)] : [])
  const first = Math.max(0, Math.floor(Math.min(...marks, 24)) - 1)
  const last = today ? 24 : Math.min(24, Math.ceil(Math.max(...bars.map(bar => hourOf(bar.to)), first + 2)) + 1)
  const bodyTop = top + DAY_HEAD + 6
  const y = ts => bodyTop + (Math.max(first, Math.min(last, hourOf(ts))) - first) * HOUR
  const height = (last - first) * HOUR
  const lanes = pack(bars, bar => [bar.from, bar.to])
  const labelX = TIME_COL + 8 + lanes * 16
  const bands = bandsOf(data, day)
  const notes = bands.map(band => y(band.from) + 11) // where each overlap note sits, right-aligned
  const roomAt = at => Math.max(6, Math.floor((width - labelX - (notes.some(note => Math.abs(note - at) < 13) ? 104 : 4)) / CHAR))
  const parts = []
  let lastLabel = -100

  parts.push(text({ key: 'day', x: 0, y: top + 12, style: svgText(12, 'primary', { fontWeight: 600 }) }, t(index ? 'runway.tomorrow' : 'runway.today', day.label)))

  for (const [from, to] of NIGHT) {
    const a = Math.max(first, from)
    const b = Math.min(last, to)

    if (b > a) {
      parts.push(jsx('rect', { key: `night${from}`, 'data-band': 'night', x: TIME_COL, y: bodyTop + (a - first) * HOUR, width: width - TIME_COL, height: (b - a) * HOUR, style: { fill: C.primary, fillOpacity: 0.07 } }))
    }
  }

  for (let hour = first; hour <= last; hour += 1) {
    const at = bodyTop + (hour - first) * HOUR

    parts.push(jsx('line', { key: `rule${hour}`, x1: TIME_COL, x2: width, y1: at, y2: at, style: { stroke: C.stroke } }))
    parts.push(text({ key: `hour${hour}`, x: TIME_COL - 6, y: at + 3.5, textAnchor: 'end', style: svgText(10, 'primary', { fontFamily: MONO }) }, `${two(hour)}:00`))
  }

  for (const band of bands) {
    const y0 = y(band.from)
    const strong = Boolean(view.mark && band.ids.every(id => view.mark.ids.includes(id)))

    parts.push(
      el(
        'g',
        { key: `band${band.key}`, role: 'img', 'aria-label': t('runway.overlapLabel', band.names[0], band.names[1], band.span, band.length), 'data-band': 'overlap', 'data-strong': strong ? 'yes' : 'no' },
        jsx('rect', { x: TIME_COL + 2, y: y0, width: labelX - TIME_COL - 5, height: Math.max(4, y(band.to) - y0), style: { fill: C.hatchWarn, fillOpacity: strong ? 1 : 0.7, stroke: C.secondary, strokeWidth: strong ? 2 : 1, strokeDasharray: strong ? undefined : '3 3' } }),
        text({ x: width - 4, y: y0 + 11, textAnchor: 'end', style: svgText(10, 'primary', { fontWeight: 600 }) }, `⚠ ${t('runway.overlap', band.length)}`)
      )
    )
  }

  for (const bar of bars) {
    const y0 = y(bar.from)
    let labelY = Math.max(y0 + 10, lastLabel + 13)

    // Keep the label clear of the needle, which would strike it through.
    if (today && Math.abs(labelY - 4 - y(view.now)) < 8) {
      labelY = y(view.now) + 13
    }

    lastLabel = labelY
    parts.push(
      jsx(Bar, {
        bar,
        view,
        box: { x: TIME_COL + (bar.kind === 'open' ? 4 : 6) + bar.lane * 16, y: y0, width: bar.kind === 'open' ? 14 : 10, height: Math.max(6, y(bar.to) - y0) },
        label: `${hm(day, bar.from)} ${cut(bar.w.name, Math.max(4, roomAt(labelY) - 6))}`,
        labelAt: { x: labelX, y: labelY, textAnchor: 'start' }
      }, `bar${bar.w.id}`)
    )
  }

  if (today) {
    const at = y(view.now)

    parts.push(
      el(
        'g',
        { key: 'needle', 'data-needle': hm(day, view.now), 'aria-hidden': true },
        jsx('line', { x1: TIME_COL - 4, x2: width, y1: at, y2: at, style: { stroke: C.primary, strokeWidth: 1.5 } }),
        jsx('path', { d: `M ${TIME_COL - 5} ${at - 4} L ${TIME_COL - 5} ${at + 4} L ${TIME_COL + 1} ${at} Z`, style: { fill: C.accent, stroke: C.primary, strokeWidth: 1 } }),
        text({ x: width - 4, y: at - 4, textAnchor: 'end', style: svgText(10, 'primary', { fontWeight: 700, fontFamily: MONO }) }, t('runway.now', hm(day, view.now)))
      )
    )
  }

  return { height: DAY_HEAD + 6 + height + 6, node: jsx('g', { 'data-day': index, children: parts }, `day${index}`) }
}

function Legend() {
  const t = usePluginI18n(ID)
  const swatch = (key, shape) =>
    el('li', { key, style: S.legendItem }, jsx('svg', { width: 16, height: 10, viewBox: '0 0 16 10', 'aria-hidden': true, children: shape }), t(`runway.legend.${key}`))
  const box = style => jsx('rect', { x: 1, y: 1, width: 14, height: 8, rx: 2, style })

  return el(
    'ul',
    { style: S.legend },
    swatch('open', jsx('rect', { x: 1, y: 0.5, width: 14, height: 9, rx: 2, style: { ...BAR_PAINT.open, strokeWidth: 1.5 } })),
    swatch('approved', box(BAR_PAINT.approved)),
    swatch('pending', box(BAR_PAINT.pending)),
    swatch('past', box(BAR_PAINT.past)),
    swatch('overlap', box({ fill: C.hatchWarn, stroke: C.secondary, strokeDasharray: '3 3' })),
    swatch('night', box({ fill: C.primary, fillOpacity: 0.07, stroke: C.stroke })),
    swatch('now', jsx('line', { x1: 8, x2: 8, y1: 0, y2: 10, style: { stroke: C.primary, strokeWidth: 1.5 } }))
  )
}

/** Wide: today and tomorrow as two 24-hour strips. Narrow: a vertical agenda ladder. */
function Runway({ data, view, ladder, width }) {
  const t = usePluginI18n(ID)
  const days = daysOf(data)

  if (!days.length) {
    return null
  }

  const span = Math.max(ladder ? 280 : 480, width || (ladder ? 358 : 960))
  const Day = ladder ? LadderDay : WideDay
  const nodes = []
  let top = 0

  days.forEach((day, index) => {
    // The ladder shows tomorrow only when something is planned there.
    if (ladder && index && !barsOf(data, view.now, day).length) {
      return
    }

    const drawn = Day({ day, index, top, width: span, view, data })

    nodes.push(drawn.node)
    top += drawn.height + 14
  })

  const shown = view.active && ((data && data.windows) || []).find(w => w.id === view.active)
  const p = shown ? phase(shown, view.now) : ''
  const readout = shown
    ? t('runway.readout', [shown.name, t(`state.${p}`), shown.span, relative(t, shown, p, view.now), t('scope', shown.scope)])
    : t('runway.hint')

  return el(
    'div',
    { style: S.runway },
    el(
      'svg',
      { viewBox: `0 0 ${span} ${Math.max(1, top - 14)}`, style: S.svg, role: 'group', 'aria-label': t('runway.label'), 'data-runway': ladder ? 'ladder' : 'wide' },
      jsx(Defs, {}),
      ...nodes
    ),
    jsx('p', { style: S.readout, 'data-readout': shown ? shown.id : '', children: readout }),
    jsx(Legend, {})
  )
}

// ── window cards ────────────────────────────────────────────────────────────

const $wide = atom({ width: 0, height: 0, narrow: false }) // hosts without host.state.viewport

/** The decision as a stamp: who decided where and when, in the plugin's time zone. */
function Stamp({ w, p, data }) {
  const t = usePluginI18n(ID)
  const channel = w.decided_via && t(`via.${w.decided_via}`) !== `via.${w.decided_via}` ? t(`via.${w.decided_via}`) : ''
  const at = w.decided_when ? short(w.decided_when, data) : w.decided_at ? clock(w.decided_at) : ''
  const closed = w.closed_when ? short(w.closed_when, data) : ''
  const lines = []
  let icon = 'pass'
  let idle = false

  if (p === 'pending' || p === 'expired') {
    lines.push(t(`stamp.${p}`))
    icon = p === 'pending' ? 'watch' : 'circle-slash'
    idle = true
  } else if (p === 'cancelled') {
    lines.push(t('stamp.cancelled', closed))
    icon = 'discard'
    idle = true
  } else if (p === 'declined') {
    lines.push(t('stamp.declined', channel, at))
    icon = 'close'
  } else {
    lines.push(t('stamp.approved', channel, at))

    if (p === 'closed') {
      lines.push(t('stamp.closed', closed))
    }
  }

  return el(
    'div',
    { style: idle ? { ...S.stamp, ...S.stampIdle } : S.stamp, 'data-stamp': p },
    jsx(Codicon, { name: icon, size: 12 }),
    jsx('span', { children: lines.join(' · ') })
  )
}

function LinkedCards({ w }) {
  const t = usePluginI18n(ID)
  const details = {}

  for (const info of w.card_details || []) {
    details[info.task_id] = info
  }

  const chips = (w.cards || [])
    .map(card => card && card.task_id)
    .filter(Boolean)
    .map(id => {
      const info = details[id]

      return jsx(Tip, {
        label: t('card.open', id),
        children: el(
          'button',
          { type: 'button', style: S.link, 'data-card': id, onClick: () => host.navigate(BOARD_PATH) },
          jsx(Codicon, { name: 'project', size: 12 }),
          info ? jsx('span', { children: t('card.detail', info.title, info.status, info.assignee) }) : null,
          jsx('span', { style: S.id, children: id })
        )
      }, id)
    })

  return chips.length ? jsx('div', { style: S.row, children: chips }) : null
}

/** Each overlap as a sentence on a button: the other window by name, the shared span. */
function OverlapChips({ w, view }) {
  const t = usePluginI18n(ID)
  const conflicts = w.conflicts || (w.overlaps || []).map(id => ({ id }))

  if (!conflicts.length) {
    return null
  }

  return jsx('div', {
    style: S.row,
    children: conflicts.map(c =>
      el(
        'button',
        {
          key: c.id,
          type: 'button',
          style: { ...S.link, ...S.overlapChip },
          'data-overlap': c.id,
          'aria-expanded': Boolean(view.conflict && view.conflict.on === w.id && view.conflict.other === c.id),
          onClick: () => view.explain(w, c)
        },
        jsx(Codicon, { name: 'warning', size: 12 }),
        jsx('span', { children: c.name ? t('overlap.chip', c.name, c.overlap, c.length) : t('overlap.chipId', c.id) })
      )
    )
  })
}

/** Why two windows conflict, what follows, and the ways out. Shown on the card it was opened to. */
function ConflictPanel({ w, c, view }) {
  const t = usePluginI18n(ID)
  const row = (label, value) => [jsx('dt', { style: S.term, children: label }, `${label}t`), jsx('dd', { style: S.fact, children: value }, `${label}d`)]

  return el(
    'section',
    { style: S.panel, role: 'region', 'aria-label': t('overlap.title', c.name), 'data-conflict': `${w.id}:${c.id}` },
    el('h3', { style: S.panelTitle }, jsx(Codicon, { name: 'warning', size: 14 }), jsx('span', { children: t('overlap.title', c.name) })),
    jsx('dl', {
      style: S.facts,
      children: [
        ...row(
          t('overlap.other'),
          el(
            'span',
            {},
            t('overlap.otherValue', [c.name, t(`state.${c.state}`), c.span, t('scope', c.scope), c.proposer ? t('proposedBy', c.proposer) : '']),
            ' ',
            jsx('span', { style: S.id, children: c.id })
          )
        ),
        ...row(t('overlap.span'), t('overlap.spanValue', c.overlap, c.length)),
        ...row(t('overlap.rule'), c.why),
        ...row(t('overlap.means'), c.consequence),
        ...row(t('overlap.wayOut'), c.way_out)
      ]
    }),
    el(
      'div',
      { style: S.actions },
      jsx(Button, { size: 'sm', variant: 'outline', onClick: () => view.explain(w, c), children: t('overlap.show', c.name) }),
      jsx(Button, { size: 'sm', variant: 'text', onClick: () => view.explain(null), children: t('overlap.dismiss') })
    )
  )
}

function Actions({ w, p, approval, ask }) {
  const t = usePluginI18n(ID)
  const buttons = []

  if (p === 'pending' && approval) {
    buttons.push(
      jsx(Button, { size: 'touch', onClick: () => ask(w, 'approve'), children: t('action.approve') }, 'approve'),
      jsx(Button, { size: 'touch', variant: 'outline', onClick: () => ask(w, 'decline'), children: t('action.decline') }, 'decline')
    )
  }

  if (p === 'approved' || p === 'waiting' || p === 'open') {
    buttons.push(jsx(Button, { size: 'touch', variant: 'outline', onClick: () => ask(w, 'close'), children: t('action.close') }, 'close'))
  }

  return buttons.length ? jsx('div', { style: S.actions, children: buttons }) : null
}

function WindowCard({ w, data, view, approval, ask }) {
  const t = usePluginI18n(ID)
  const p = phase(w, view.now)
  const rel = relative(t, w, p, view.now)
  const open = view.conflict && view.conflict.on === w.id ? (w.conflicts || []).find(c => c.id === view.conflict.other) : null
  const lit = view.spot === w.id || Boolean(view.mark && view.mark.ids.includes(w.id))

  return el(
    'article',
    {
      ref: node => view.keep(w.id, node),
      tabIndex: -1,
      'aria-label': w.name,
      'data-window': w.id,
      'data-lit': lit ? 'yes' : 'no',
      style: { ...S.card, ...(lit ? S.cardLit : null), ...(open ? S.cardWide : null) }
    },
    el('div', { style: S.head }, jsx(StateBadge, { p }), jsx('span', { style: S.name, children: w.name }), jsx(Badge, { variant: 'outline', children: t('scope', w.scope) })),
    jsx('div', { style: S.meta, children: rel ? `${w.span} · ${rel}` : w.span }),
    w.reason ? jsx('div', { style: S.reason, children: w.reason }) : null,
    el(
      'div',
      { style: S.row },
      jsx('span', { style: S.small, children: [w.proposer ? t('proposedBy', w.proposer) : '', t(w.exclusive ? 'exclusive' : 'shared')].filter(Boolean).join(' · ') }),
      jsx('span', { style: S.id, children: w.id }),
      jsx(CopyButton, { appearance: 'icon', buttonSize: 'icon-xs', text: w.id, label: t('copyId') })
    ),
    jsx(LinkedCards, { w }),
    jsx(OverlapChips, { w, view }),
    open ? jsx(ConflictPanel, { w, c: open, view }) : null,
    el('div', { style: S.foot }, jsx(Stamp, { w, p, data }), jsx(Actions, { w, p, approval, ask }))
  )
}

function Group({ name, rows, data, view, approval, ask }) {
  const t = usePluginI18n(ID)

  return el(
    'section',
    { style: S.section, 'data-group': name },
    el('h2', { style: S.h2 }, t(`group.${name}`), jsx(Badge, { variant: 'muted', children: String(rows.length) })),
    jsx('div', { style: S.cards, children: rows.map(w => jsx(WindowCard, { w, data, view, approval, ask }, w.id)) })
  )
}

// ── the ledger ──────────────────────────────────────────────────────────────

const LEDGER_GLYPH = { blocked: 'circle-slash', warned: 'warning', allowed: 'check', approved: 'pass', declined: 'close' }

/** Decisions and matched calls in one log, newest first. Calls are metadata only. */
function Ledger() {
  const t = usePluginI18n(ID)
  const { data } = useQuery({ queryKey: AUDIT_KEY, queryFn: fetchAudit, refetchInterval: POLL_MS })
  const entries = []

  for (const d of (data && data.decisions) || []) {
    const channel = d.decided_via && t(`via.${d.decided_via}`) !== `via.${d.decided_via}` ? t(`via.${d.decided_via}`) : ''
    // The row is a decision. A window closed after its approval still reads "approved in …",
    // because the channel is where it was approved, not where it was closed.
    const verdict = d.state === 'declined' ? 'declined' : 'approved'

    entries.push({
      key: `d:${d.window_id}:${d.decided_at}`,
      ts: d.decided_at,
      when: d.when || clock(d.decided_at),
      verdict,
      tone: 'plain',
      line: t('ledger.row', [t('ledger.decision', t(`state.${verdict}`), channel), d.name, t('ledger.by', d.decided_by), d.state === 'closed' ? t('ledger.closedLater') : '']),
      id: d.window_id
    })
  }

  for (const row of (data && data.audit) || []) {
    entries.push({
      key: `c:${row.id}`,
      ts: row.ts,
      when: row.when || clock(row.ts),
      verdict: row.verdict,
      tone: S.glyphTone[row.verdict] ? row.verdict : 'plain',
      line: t('ledger.row', [row.verdict, row.tool, parseList(row.scopes).join(', '), parseList(row.rule_ids).join(', ')]),
      id: row.window_id || ''
    })
  }

  entries.sort((a, b) => b.ts - a.ts)

  const items = entries.map(entry =>
    el(
      'li',
      { key: entry.key, style: S.entry, 'data-verdict': entry.verdict },
      jsx('span', { style: S.moment, children: entry.when }),
      jsx('span', { style: { ...S.glyph, ...S.glyphTone[entry.tone] }, children: jsx(Codicon, { name: LEDGER_GLYPH[entry.verdict] || 'circle-outline', size: 12 }) }),
      el('span', { style: S.line }, entry.line, entry.id ? ' ' : null, entry.id ? jsx('span', { style: S.id, children: entry.id }) : null)
    )
  )

  return el(
    'section',
    { style: S.section, 'data-ledger': String(items.length) },
    jsx('h2', { style: S.h2, children: t('ledger.title') }),
    jsx('p', { style: S.hint, children: t('ledger.hint') }),
    items.length ? jsx('ol', { style: S.ledger, children: items }) : jsx('p', { style: S.hint, children: t('ledger.empty') })
  )
}

// ── the page ────────────────────────────────────────────────────────────────

function ChangeWindowsPage() {
  const t = usePluginI18n(ID)
  const viewport = useValue((host.state && host.state.viewport) || $wide)
  const query = useWindows()
  // Every piece of page state lives here; the parts below are plain functions of it.
  const [pending, setPending] = useState(null) // { w, action, key } while a confirm dialog is open
  const [active, setActive] = useState(null) // the bar under the pointer or the keyboard focus
  const [conflict, setConflict] = useState(null) // { on, other }: the explained overlap, on card `on`
  const [spot, setSpot] = useState(null) // the card a runway bar jumped to
  const [width, setWidth] = useState(0) // the runway's own width, so the timeline is drawn at 1:1
  const frame = useRef(null)
  const cards = useRef({})

  useEffect(() => {
    const node = frame.current

    if (!node || typeof ResizeObserver === 'undefined') {
      return undefined
    }

    const observer = new ResizeObserver(entries => setWidth(Math.round(entries[0].contentRect.width)))

    observer.observe(node)

    return () => observer.disconnect()
  }, [])

  const data = query.data
  const now = nowOf(data)
  const approval = Boolean(data && data.approval === true)
  const ladder = Boolean(viewport && viewport.narrow) || (width > 0 && width < 560)

  // One key per opened dialog: a retry inside the dialog repeats the same request.
  const ask = (w, action) => setPending({ w, action, key: newKey() })

  const reveal = id => {
    const node = cards.current[id]

    if (node && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ block: 'center' })
      node.focus({ preventScroll: true })
    }
  }

  const view = {
    now,
    active,
    conflict,
    spot,
    mark: conflict ? { ids: [conflict.on, conflict.other] } : null,
    hover: setActive,
    keep: (id, node) => {
      cards.current[id] = node
    },
    pick: id => {
      setSpot(id)
      reveal(id)
    },
    /** Explain the overlap of `w` with `c` on the OTHER window's card (so the click leads to it). */
    explain: (w, c) => {
      if (!w) {
        setConflict(null)

        return
      }

      const known = ((data && data.windows) || []).some(row => row.id === c.id && (row.conflicts || []).some(x => x.id === w.id))
      const next = known ? { on: c.id, other: w.id } : { on: w.id, other: c.id }

      setConflict(next)
      setSpot(next.on)
      reveal(next.on)
    }
  }

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
      jsx(Group, { name, rows: byGroup[name], data, view, approval, ask }, name)
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
      el(
        'header',
        { style: S.hero },
        jsx('h1', { style: S.h1, children: t('title') }),
        data ? jsx(Door, { data }) : jsx('p', { style: S.hint, children: t('subtitle') }),
        jsx('div', { ref: frame, style: S.runway, children: data ? jsx(Runway, { data, view, ladder, width }) : null })
      ),
      data && !approval ? jsx('div', { style: S.notice, role: 'status', children: t('approvalOff') }) : null,
      data && approval ? jsx('p', { style: S.hint, children: t('trust') }) : null,
      data && query.isError ? jsx('div', { style: S.notice, role: 'status', children: t('stale') }) : null,
      content,
      data ? jsx(Ledger, {}) : null,
      dialog
    )
  })
}

// ── status-bar chip ─────────────────────────────────────────────────────────

/** The door in two or three words: `closed`, `open, 37 min`, `next 14:00`. */
function StatusChip() {
  const t = usePluginI18n(ID)
  const { data } = useWindows()

  if (!data) {
    return null
  }

  const door = doorOf(data, t)
  const { head, mode } = door
  let label = t(mode === 'enforce' ? 'chip.closed' : `chip.${mode}`)

  if (mode === 'enforce' && head.kind === 'open') {
    label = t('chip.open', duration(t, head.seconds)) + (head.more ? t('chip.more', head.more) : '')
  } else if (mode === 'enforce' && head.kind === 'next') {
    label = t('chip.next', startOf(head.w, data))
  }

  return jsx(Tip, {
    label: t('chip.tip', `${door.title}. ${door.lines.join(' ')}`),
    children: el(
      'button',
      { type: 'button', style: S.chip, onClick: () => host.navigate(PATH) },
      jsx(Codicon, { name: door.icon, size: 12 }),
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
      label = t('badge.approved', startOf(w, data))
    } else if (p === 'pending') {
      label = w.start_ts > now ? t('badge.pending', startOf(w, data)) : t('badge.pendingNow')
    }

    badges[id] = { label, tone: BADGE_TONE[p] || 'neutral', tooltip: `${w.name} · ${w.span} · ${w.id}` }
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
