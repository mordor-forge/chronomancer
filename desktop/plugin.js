/**
 * chronomancer: the Hermes Desktop half.
 *
 * One question on every surface: what runs next, and where does it clash?
 *
 * - Page `/chronomancer` with a sidebar row:
 *     the astrolabe  the plugin's clock, the headline (what runs next) and the horizon switch
 *                    (day, 2 days, week);
 *     the loom       a timeline with one lane per profile. A slot is a bar: outlined while planned,
 *                    dashed once released, shimmering while its card runs, faded when done. A clash
 *                    is hatched on both bars. Night is a starred band; "now" is a glowing needle.
 *                    Drag a bar to move its slot, or drag a card from the tray onto the loom;
 *     the scroll     the selected card: its state in one sentence, its clashes, its labels, and the
 *                    moves (cast now, move, pin, unplan);
 *     the tray       the open cards that have no slot;
 *     the chronicle  who changed what in the plan: you, the orchestrator, or the timer.
 * - Status-bar chip and palette command.
 *
 * Plain ESM, loaded uncompiled: jsx() calls, no JSX. Imports only @hermes/plugin-sdk, react and
 * react/jsx-runtime. Data comes from the plugin's own backend (dashboard/plugin_api.py) by polling.
 * Every sentence about the plan (the headline, a card's state, a clash, a log entry, the result of
 * a move) is written by the backend; this file lays them out and owns only labels and buttons.
 * Colour comes from the app's theme variables and never carries meaning alone: every state also
 * has a word, a glyph or a pattern. Motion stops under "reduce motion".
 */

import * as SDK from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const {
  atom,
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  ErrorState,
  host,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  Tip,
  usePluginI18n,
  useQuery,
  useValue
} = SDK

const ID = 'chronomancer'
const PATH = '/chronomancer'
const BOARD_PATH = '/kanban'
const POLL_MS = 5000
const SNAP_S = 900
const HORIZONS = ['day', '2d', 'week']
const LANE_W = 132 // px: the column with the profile names
const HEAD_H = 46 // px: the day and hour labels above the lanes
const ROW_H = 30
const ROW_GAP = 6
const LANE_PAD = 9
const MIN_BAR = 10
const INSIDE = 150 // px: a body at least this wide carries its title inside
const TAG = { day: 260, '2d': 200, week: 130 } // px: the most room a title beside a body may take

const EN = {
  title: 'Chronomancer',
  subtitle: 'One plan for the Kanban cards of every profile.',
  open: 'Open Chronomancer',
  loading: 'Reading the plan…',
  loadFailed: 'The plan is unavailable',
  loadFailedHint:
    'The plugin backend did not answer. Enable the chronomancer plugin for this profile and restart the gateway.',
  stale: 'Could not refresh. Showing the last plan received.',
  retry: 'Retry',
  failed: 'The move failed.',
  boardDown: 'The Kanban board cannot be read',
  horizon: { label: 'Horizon', day: 'Day', '2d': '2 days', week: 'Week' },
  now: at => `now ${at}`,
  noProfile: 'No profile',
  loom: {
    title: 'Timeline',
    label: 'Timeline of slots, one lane per profile',
    empty: 'Nothing is planned in this horizon.',
    hint: 'Drag a slot to move it. Drag a card from Unplanned onto the timeline to plan it.',
    moveTo: at => `Move to ${at}`,
    planAt: at => `Plan at ${at}`,
    bar: parts => parts.filter(Boolean).join(', ')
  },
  tray: {
    title: 'Unplanned',
    empty: 'Every open card has a slot.',
    hint: 'Cards without a slot. Drag one onto the timeline.'
  },
  scroll: {
    title: 'Selected card',
    none: 'Select a slot or a card to see it here.',
    start: 'Start',
    estimate: 'Estimate (minutes)',
    labels: 'Labels',
    noLabels: 'No labels',
    addLabel: 'Add a label, for example repo:web',
    clashes: 'Clashes',
    board: 'Open the board',
    card: id => `card ${id}`
  },
  action: {
    now: 'Cast now',
    save: 'Move',
    plan: 'Plan',
    pin: 'Pin',
    unpin: 'Unpin',
    clear: 'Unplan',
    earlier: '15 min earlier',
    later: '15 min later',
    nextFree: at => `Next free start: ${at}`,
    add: 'Add',
    remove: name => `Remove the label ${name}`,
    close: 'Close'
  },
  confirm: {
    nowTitle: 'Cast now?',
    nowBody: title => `"${title}" is released at once and its slot moves to now.`,
    nowAction: 'Cast now'
  },
  chronicle: { title: 'Changes', empty: 'No changes yet.' },
  filter: {
    label: 'Filters',
    clashes: 'Clashes only',
    oneAtATime: 'one card at a time',
    clear: 'Clear filters'
  },
  phase: {
    planned: 'planned',
    due: 'due',
    waiting: 'released',
    running: 'running',
    done: 'done',
    missed: 'missed',
    failed: 'not released',
    gone: 'gone',
    unplanned: 'unplanned'
  },
  mark: { pinned: 'pinned by you', clash: 'clash', busy: 'busy', over: 'over its estimate' },
  chipTip: tip => `Chronomancer. ${tip}`
}

// Set in register(): the plugin's REST door and its storage.
let rest = null
let storage = null

// ── data ────────────────────────────────────────────────────────────────────

const planKey = horizon => [ID, 'plan', horizon]

async function fetchPlan(horizon) {
  const data = await rest(`/plan?horizon=${encodeURIComponent(horizon)}`)

  // Times are compared on the server's clock: keep the offset to this machine's clock.
  return { ...data, skew: (Number(data.now) || 0) - Date.now() / 1000 }
}

const nowOf = data => Math.floor(Date.now() / 1000 + ((data && data.skew) || 0))

function usePlan(horizon) {
  return useQuery({
    queryKey: planKey(horizon),
    queryFn: () => fetchPlan(horizon),
    refetchInterval: POLL_MS,
    placeholderData: previous => previous
  })
}

/** The backend's sentence from a failed request ("409: {"detail": "..."}"), else the fallback. */
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

const card = id => `/cards/${encodeURIComponent(id)}`

const api = {
  slot: (id, body) => rest(`${card(id)}/slot`, { method: 'POST', body }),
  now: id => rest(`${card(id)}/now`, { method: 'POST' }),
  clear: id => rest(`${card(id)}/slot`, { method: 'DELETE' }),
  pin: (id, pinned) => rest(`${card(id)}/pin`, { method: 'POST', body: { pinned } }),
  tag: (id, add, remove) => rest(`${card(id)}/labels`, { method: 'POST', body: { add, remove } })
}

/** Run one move, say what the backend said, and read the plan again. True when it went through. */
async function move(call, fallback) {
  let ok = true

  try {
    const out = await call()
    const change = out && out.change

    if (change && change.message) {
      host.notify({ kind: change.clashes && change.clashes.length ? 'warning' : 'success', message: change.message })
    }
  } catch (error) {
    ok = false
    host.notify({ kind: 'error', message: errorText(error, fallback) })
  }

  await queryClient.invalidateQueries({ queryKey: [ID] })

  return ok
}

// ── time and geometry ───────────────────────────────────────────────────────

const pad = n => String(n).padStart(2, '0')
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** ts → "Wed 14:15" in the plugin's time zone (the zone of every time the backend writes). */
function clockOf(data) {
  let format = null

  try {
    if (data.tz) {
      format = new Intl.DateTimeFormat('en-GB', { timeZone: data.tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })
    }
  } catch {
    format = null
  }

  return ts => {
    if (format) {
      return format.format(new Date(ts * 1000)).replace(',', '')
    }

    const local = new Date((ts + (Number(data.utc_offset_s) || 0)) * 1000)

    return `${DAYS[local.getUTCDay()]} ${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}`
  }
}

const snap = ts => Math.round(ts / SNAP_S) * SNAP_S
const soonest = now => Math.ceil((now + 60) / SNAP_S) * SNAP_S // the first quarter hour that is still ahead

function hash(text) {
  let h = 2166136261

  for (let i = 0; i < text.length; i += 1) {
    h = Math.imul(h ^ text.charCodeAt(i), 16777619)
  }

  return h >>> 0
}

const HUES = ['--ui-purple', '--ui-cyan', '--ui-orange', '--ui-blue', '--ui-green', '--ui-yellow']

/** A profile's colour: the app's own colour for it, else one of the theme's hues. */
function laneColor(profile, index) {
  const own = typeof SDK.profileColor === 'function' ? SDK.profileColor(profile) : null

  return own || `var(${HUES[index % HUES.length]}, var(--ui-accent))`
}

/** Bars of one lane, packed into as few rows as fit without overlap. */
function pack(bars) {
  const ends = []

  for (const bar of bars.sort((a, b) => a.x - b.x || a.item.task_id.localeCompare(b.item.task_id))) {
    let row = ends.findIndex(end => end + 3 <= bar.x)

    if (row < 0) {
      row = ends.length
      ends.push(0)
    }

    ends[row] = bar.x + bar.span
    bar.row = row
  }

  return Math.max(1, ends.length)
}

/** Where everything sits for a plot of `width` pixels. */
function layoutOf(data, width, hidden, moved) {
  const { start, end } = data.horizon
  const plot = Math.max(240, width - LANE_W)
  const scale = plot / Math.max(1, end - start)
  const x = ts => (ts - start) * scale
  const room = TAG[data.horizon.key] || TAG['2d']
  const lanes = []
  let top = HEAD_H

  data.lanes.forEach((lane, index) => {
    if (hidden.profiles.has(lane.profile)) {
      return
    }

    const bars = data.items
      .filter(item => item.assignee === lane.profile)
      .map(item => {
        const shift = moved && moved.id === item.task_id && moved.start !== null ? moved.start - item.start : 0
        const from = Math.max(0, x(item.start + shift))
        const w = Math.max(MIN_BAR, Math.min(plot, x(item.end + shift)) - from)
        // A short body gets its title beside it, as far as the plot has room.
        const tag = w >= INSIDE ? 0 : Math.max(0, Math.min(room, 22 + item.short.length * 6.4, plot - from - w - 6))

        return { item, shift, x: from, w, tag, span: w + (tag ? 6 + tag : 0), row: 0 }
      })
      .filter(bar => bar.x < plot)
    const rows = pack(bars)
    const height = LANE_PAD * 2 + rows * ROW_H + (rows - 1) * ROW_GAP

    lanes.push({ ...lane, index, color: laneColor(lane.profile, index), bars, top, height })
    top += height
  })

  return { plot, scale, x, lanes, height: Math.max(top, HEAD_H + 60), start, end }
}

function matches(item, filters) {
  if (filters.clashes && !item.clash) {
    return false
  }

  return !filters.labels.size || item.labels.some(name => filters.labels.has(name))
}

// ── look ────────────────────────────────────────────────────────────────────

const mix = (color, percent) => `color-mix(in srgb, ${color} ${percent}%, transparent)`

const CSS = `
.chm { --chm-display: 'Cinzel', 'Cormorant Garamond', 'Palatino Linotype', Palatino, 'Book Antiqua', Georgia, serif;
  --chm-mono: var(--dt-font-mono, ui-monospace, monospace);
  --chm-ink: var(--ui-text-primary); --chm-soft: var(--ui-text-secondary); --chm-line: var(--ui-stroke-secondary);
  --chm-glow: ${mix('var(--ui-accent)', 55)}; --chm-veil: ${mix('var(--ui-accent)', 8)};
  height: 100%; overflow-y: auto; color: var(--chm-ink); }
.chm-inner { max-width: 92rem; margin: 0 auto; padding: 1.25rem 1.25rem 3rem; display: grid; gap: 1.125rem; }
.chm h2 { font-family: var(--chm-display); font-size: 0.8125rem; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase;
  margin: 0; display: flex; align-items: center; gap: 0.5rem; color: var(--chm-ink); }
.chm h2::after { content: ''; flex: 1 1 auto; height: 1px; background: linear-gradient(90deg, var(--chm-line), transparent); }
.chm-p { font-size: 0.8125rem; margin: 0; color: var(--chm-ink); overflow-wrap: anywhere; }
.chm-quiet { font-size: 0.75rem; margin: 0; color: var(--chm-ink); opacity: 0.86; overflow-wrap: anywhere; }
.chm-head { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 1.25rem; padding: 0.875rem 1rem; border-radius: 14px;
  border: 1px solid var(--chm-line); background: radial-gradient(120% 160% at 0% 0%, var(--chm-veil), transparent 62%); }
.chm-astro { flex: 0 0 auto; color: var(--ui-accent); filter: drop-shadow(0 0 7px var(--chm-glow)); }
.chm-spin { transform-origin: center; animation: chm-spin 90s linear infinite; }
.chm-titles { flex: 1 1 18rem; min-width: 0; display: grid; gap: 0.25rem; }
.chm-title { font-family: var(--chm-display); font-size: 0.75rem; font-weight: 600; letter-spacing: 0.22em; text-transform: uppercase; margin: 0; }
.chm-headline { font-size: clamp(1.0625rem, 0.8rem + 1vw, 1.375rem); line-height: 1.25; font-weight: 650; margin: 0; overflow-wrap: anywhere; }
.chm-facts { display: flex; flex-wrap: wrap; gap: 0.375rem; margin: 0; padding: 0; list-style: none; }
.chm-fact { display: inline-flex; align-items: center; gap: 0.3125rem; font-size: 0.75rem; padding: 0.125rem 0.5rem; border-radius: 999px;
  border: 1px solid var(--chm-line); }
.chm-fact[data-kind='clash'] { border-color: var(--ui-red); }
.chm-fact[data-kind='attention'] { border-color: var(--ui-yellow); border-style: dashed; }
.chm-side { display: grid; gap: 0.375rem; justify-items: end; }
.chm-seg { display: inline-flex; padding: 2px; gap: 2px; border-radius: 9px; border: 1px solid var(--chm-line); }
.chm-seg button, .chm-chip, .chm-plain { font: inherit; color: var(--chm-ink); background: transparent; border: 0; cursor: pointer; }
.chm-seg button { font-size: 0.75rem; padding: 0.25rem 0.75rem; border-radius: 7px; }
.chm-seg button[aria-pressed='true'] { background: ${mix('var(--ui-accent)', 22)}; box-shadow: inset 0 0 0 1px var(--ui-accent); font-weight: 600; }
.chm-seg button:focus-visible, .chm-chip:focus-visible, .chm-plain:focus-visible, .chm-row:focus-visible { outline: 2px solid var(--chm-ink); outline-offset: 2px; }
.chm-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem; }
.chm-chip { display: inline-flex; align-items: center; gap: 0.3125rem; font-size: 0.75rem; padding: 0.1875rem 0.5625rem; border-radius: 999px;
  border: 1px solid var(--chm-line); max-width: 100%; }
.chm-chip[aria-pressed='true'] { border-color: var(--lane, var(--ui-accent)); background: ${mix('var(--lane, var(--ui-accent))', 18)}; }
.chm-chip[data-off='1'] { opacity: 0.5; text-decoration: line-through; }
.chm-notice { font-size: 0.8125rem; border: 1px solid var(--chm-line); border-left: 4px solid var(--ui-yellow); border-radius: 8px; padding: 0.5rem 0.75rem; margin: 0; }
.chm-section { display: grid; gap: 0.5rem; min-width: 0; }
.chm-loom { position: relative; border: 1px solid var(--chm-line); border-radius: 14px; overflow: hidden; user-select: none;
  background: linear-gradient(180deg, var(--chm-veil), transparent 38%); }
.chm-lane { position: absolute; left: 0; right: 0; border-top: 1px solid var(--chm-line); pointer-events: none; }
.chm-lane-name { position: absolute; left: 0; top: 0; bottom: 0; width: ${LANE_W}px; box-sizing: border-box; display: flex; align-items: center; gap: 0.4375rem;
  padding: 0 0.625rem; font-size: 0.75rem; font-weight: 600; border-right: 1px solid var(--chm-line);
  background: linear-gradient(90deg, ${mix('var(--lane)', 14)}, transparent); }
.chm-lane-name span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.chm-sigil { flex: 0 0 auto; color: var(--lane, var(--ui-accent)); }
.chm-tick { position: absolute; top: 24px; bottom: 0; width: 1px; background: var(--chm-line); opacity: 0.45; pointer-events: none; }
.chm-tick[data-day='1'] { top: 0; opacity: 1; }
.chm-tick-label { position: absolute; top: 26px; font-size: 0.6875rem; font-family: var(--chm-mono); padding-left: 4px; white-space: nowrap; pointer-events: none; }
.chm-day-label { position: absolute; top: 5px; font-family: var(--chm-display); font-size: 0.6875rem; letter-spacing: 0.12em; text-transform: uppercase;
  padding-left: 6px; white-space: nowrap; overflow: hidden; pointer-events: none; }
.chm-day-label[data-today='1'] { color: var(--ui-accent); font-weight: 700; }
.chm-night { position: absolute; bottom: 0; background-color: ${mix('var(--chm-ink)', 5)};
  background-image: radial-gradient(circle at 20% 30%, ${mix('var(--chm-ink)', 45)} 0.6px, transparent 1.2px),
    radial-gradient(circle at 70% 65%, ${mix('var(--chm-ink)', 32)} 0.5px, transparent 1.1px),
    radial-gradient(circle at 45% 85%, ${mix('var(--ui-accent)', 55)} 0.6px, transparent 1.3px);
  background-size: 37px 29px, 53px 41px, 71px 47px; pointer-events: none; }
.chm-past { position: absolute; bottom: 0; left: ${LANE_W}px; background: ${mix('var(--chm-ink)', 4)}; pointer-events: none; }
.chm-now { position: absolute; top: 14px; bottom: 0; width: 2px; margin-left: -1px; background: var(--ui-accent);
  box-shadow: 0 0 10px 1px var(--chm-glow); pointer-events: none; z-index: 4; animation: chm-pulse 2.6s ease-in-out infinite; }
.chm-now-label { position: absolute; top: 0; transform: translateX(-50%); font-size: 0.625rem; font-family: var(--chm-mono); padding: 1px 6px;
  border-radius: 999px; color: var(--chm-ink); border: 1px solid var(--ui-accent); background: var(--ui-bg-editor, var(--ui-bg-primary, transparent));
  white-space: nowrap; z-index: 5; pointer-events: none; }
.chm-bar { position: absolute; height: ${ROW_H}px; box-sizing: border-box; display: flex; align-items: center; gap: 6px; padding: 0; border: 0;
  border-radius: 9px; background: transparent; color: var(--chm-ink); font: inherit; font-size: 0.75rem; line-height: 1; text-align: left;
  white-space: nowrap; cursor: pointer; touch-action: none; transition: opacity 0.15s ease; animation: chm-rise 0.25s ease both; }
.chm-bar[data-move='1'] { cursor: grab; }
.chm-bar:focus-visible { outline: 2px solid var(--chm-ink); outline-offset: 2px; }
.chm-body { position: relative; flex: 0 0 auto; height: 100%; box-sizing: border-box; border-radius: 9px; border: 1px solid var(--lane);
  background: ${mix('var(--lane)', 15)}; display: flex; align-items: center; gap: 0.3125rem; padding: 0 0.4375rem; overflow: hidden;
  transition: box-shadow 0.15s ease; }
.chm-body[data-thin='1'] { padding: 0; justify-content: center; }
.chm-body > * { position: relative; z-index: 1; flex: 0 0 auto; }
.chm-body > .chm-bar-title { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.chm-tag { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 0.25rem; }
.chm-tag > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.chm-bar:hover > .chm-body { box-shadow: 0 0 0 1px var(--lane), 0 0 14px ${mix('var(--lane)', 45)}; }
.chm-bar[data-phase='waiting'] > .chm-body { border-style: dashed; }
.chm-bar[data-phase='due'] > .chm-body { animation: chm-pulse 1.4s ease-in-out infinite; }
.chm-bar[data-phase='running'] > .chm-body { border-width: 2px; background-image: linear-gradient(100deg, ${mix('var(--lane)', 26)} 0%, ${mix('var(--lane)', 58)} 50%, ${mix('var(--lane)', 26)} 100%);
  background-size: 220% 100%; animation: chm-sheen 3.4s linear infinite; }
.chm-bar[data-phase='done'] { opacity: 0.6; }
.chm-bar[data-phase='done'] > .chm-body { background: transparent; }
.chm-bar[data-phase='missed'] > .chm-body, .chm-bar[data-phase='failed'] > .chm-body, .chm-bar[data-phase='gone'] > .chm-body { border: 1px dashed var(--ui-red); background: ${mix('var(--ui-red)', 8)}; }
.chm-bar[data-clash='1'] > .chm-body { box-shadow: 0 0 0 1px var(--ui-red), 0 0 12px ${mix('var(--ui-red)', 50)}; }
.chm-bar[data-partner='1'] > .chm-body { box-shadow: 0 0 0 2px var(--ui-red); }
.chm-bar[data-sel='1'] { z-index: 3; font-weight: 600; }
.chm-bar[data-sel='1'] > .chm-body { box-shadow: 0 0 0 2px var(--chm-ink), 0 0 18px var(--chm-glow); }
.chm-bar[data-dim='1'] { opacity: 0.25; }
.chm-bar[data-drag='1'] { cursor: grabbing; z-index: 6; transition: none; }
.chm-bar[data-drag='1'] > .chm-body { box-shadow: 0 6px 22px var(--chm-glow), 0 0 0 2px var(--ui-accent); }
.chm-body > .chm-hatch { position: absolute; top: 0; bottom: 0; z-index: 0; pointer-events: none;
  background: repeating-linear-gradient(135deg, ${mix('var(--ui-red)', 60)} 0 4px, transparent 4px 9px); }
.chm-body > .chm-over { position: absolute; top: 0; bottom: 0; z-index: 0; pointer-events: none; border-left: 2px dotted var(--chm-ink);
  background: repeating-linear-gradient(90deg, ${mix('var(--ui-yellow)', 50)} 0 3px, transparent 3px 7px); }
.chm-bar > .chm-spark { position: absolute; top: 50%; width: 5px; height: 5px; margin: -2.5px; border-radius: 50%; background: var(--ui-accent);
  box-shadow: 0 0 8px 2px var(--chm-glow); pointer-events: none; animation: chm-spark 0.9s ease-out forwards; }
.chm-guide { position: absolute; top: 14px; bottom: 0; width: 0; border-left: 2px dashed var(--ui-accent); pointer-events: none; z-index: 7; }
.chm-guide-label { position: absolute; top: 0; left: 6px; font-size: 0.6875rem; font-family: var(--chm-mono); white-space: nowrap; padding: 2px 7px;
  border-radius: 6px; border: 1px solid var(--ui-accent); background: var(--ui-bg-editor, var(--ui-bg-primary, transparent)); color: var(--chm-ink); }
.chm-empty { position: absolute; left: ${LANE_W}px; right: 0; margin: 0; text-align: center; font-size: 0.8125rem; pointer-events: none; }
.chm-two { display: grid; gap: 1.125rem; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); align-items: start; }
.chm-card { border: 1px solid var(--chm-line); border-radius: 12px; padding: 0.875rem; display: grid; gap: 0.625rem; min-width: 0; }
.chm-card[data-lit='1'] { border-color: var(--lane, var(--ui-accent)); box-shadow: 0 0 0 1px var(--lane, var(--ui-accent)), 0 0 22px ${mix('var(--lane, var(--ui-accent))', 22)}; }
.chm-name { font-size: 1rem; font-weight: 650; margin: 0; overflow-wrap: anywhere; }
.chm-wrap { display: flex; flex-wrap: wrap; align-items: center; gap: 0.375rem; min-width: 0; }
.chm-form { display: flex; flex-wrap: wrap; align-items: end; gap: 0.5rem; margin: 0; }
.chm-field { display: grid; gap: 0.1875rem; font-size: 0.6875rem; }
.chm-input { font: inherit; font-size: 0.8125rem; color: var(--chm-ink); background: var(--ui-bg-input, transparent); border: 1px solid var(--chm-soft);
  border-radius: 7px; padding: 0.3125rem 0.5rem; min-width: 0; color-scheme: light dark; }
.chm-input:focus-visible { outline: 2px solid var(--chm-ink); outline-offset: 1px; }
.chm-clash { border-left: 3px solid var(--ui-red); padding: 0.125rem 0 0.125rem 0.5rem; font-size: 0.8125rem; }
.chm-clash[data-kind='busy'] { border-left-color: var(--ui-yellow); border-left-style: dashed; }
.chm-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.25rem; }
.chm-row { display: flex; align-items: center; gap: 0.5rem; width: 100%; box-sizing: border-box; min-width: 0; padding: 0.375rem 0.5rem; border-radius: 9px; border: 1px solid var(--chm-line);
  background: transparent; color: var(--chm-ink); font: inherit; font-size: 0.8125rem; text-align: left; cursor: pointer; touch-action: none; }
.chm-row:hover, .chm-row[data-sel='1'] { border-color: var(--lane, var(--ui-accent)); background: ${mix('var(--lane, var(--ui-accent))', 10)}; }
.chm-row[data-dim='1'] { opacity: 0.3; }
.chm-row-title { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chm-entry { display: flex; align-items: baseline; gap: 0.5rem; font-size: 0.75rem; padding: 0.1875rem 0; min-width: 0; }
.chm-entry time { flex: 0 0 auto; font-family: var(--chm-mono); white-space: nowrap; }
.chm-entry span { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
.chm-entry[data-owner='1'] span { font-weight: 600; }
.chm-agenda { display: grid; gap: 0.375rem; }
.chm-agenda h3 { font-family: var(--chm-display); font-size: 0.6875rem; letter-spacing: 0.12em; text-transform: uppercase; margin: 0.375rem 0 0; }
.chm-id { font-family: var(--chm-mono); font-size: 0.6875rem; user-select: all; }
@keyframes chm-spin { to { transform: rotate(360deg); } }
@keyframes chm-pulse { 0%, 100% { opacity: 0.62; } 50% { opacity: 1; } }
@keyframes chm-sheen { from { background-position: 110% 0; } to { background-position: -110% 0; } }
@keyframes chm-rise { from { opacity: 0; transform: translateY(3px); } }
@keyframes chm-spark { to { transform: translate(var(--dx), var(--dy)) scale(0.2); opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .chm *, .chm *::before, .chm *::after { animation: none !important; transition: none !important; } }
`

/** jsx() for one child, jsxs() for several: the calls an uncompiled plugin writes by hand. */
const el = (type, props, ...children) =>
  children.length > 1 ? jsxs(type, { ...props, children }) : jsx(type, { ...props, children: children[0] })

const icon = (name, size = 12) => jsx(Codicon, { name, size })

const PHASE_ICON = {
  planned: 'clock',
  due: 'watch',
  waiting: 'debug-step-over',
  running: 'play',
  done: 'check',
  missed: 'warning',
  failed: 'warning',
  gone: 'question',
  unplanned: 'circle-outline'
}

const FACT_ICON = { running: 'play', clash: 'zap', busy: 'person', attention: 'warning', planned: 'clock', tray: 'inbox' }

// ── the astrolabe and the sigils ────────────────────────────────────────────

function star(points, outer, inner, turn) {
  const path = []

  for (let i = 0; i < points * 2; i += 1) {
    const radius = i % 2 ? inner : outer
    const angle = (Math.PI * i) / points + turn

    path.push(`${i ? 'L' : 'M'}${(Math.sin(angle) * radius).toFixed(2)} ${(-Math.cos(angle) * radius).toFixed(2)}`)
  }

  return `${path.join(' ')} Z`
}

/** The page's clock: a small astrolabe whose hands show the plugin's time. */
function Astrolabe({ data, size = 58 }) {
  const local = new Date((nowOf(data) + (Number(data.utc_offset_s) || 0)) * 1000)
  const minutes = local.getUTCMinutes()
  const hours = (local.getUTCHours() % 12) + minutes / 60
  const line = { stroke: 'currentColor', strokeLinecap: 'round', fill: 'none' }
  const marks = []

  for (let i = 0; i < 12; i += 1) {
    marks.push(jsx('line', { ...line, x1: 0, y1: -27, x2: 0, y2: i % 3 ? -24.5 : -22.5, strokeWidth: i % 3 ? 0.8 : 1.4, transform: `rotate(${i * 30})` }, i))
  }

  return el(
    'svg',
    { className: 'chm-astro', width: size, height: size, viewBox: '-30 -30 60 60', 'aria-hidden': true },
    jsx('circle', { ...line, r: 28.5, strokeWidth: 1 }),
    jsx('g', { children: marks }),
    el(
      'g',
      { className: 'chm-spin' },
      jsx('circle', { ...line, r: 18.5, strokeWidth: 0.7, strokeDasharray: '1.5 3.2' }),
      jsx('path', { ...line, d: star(7, 18.5, 8.5, 0), strokeWidth: 0.6, opacity: 0.75 })
    ),
    jsx('line', { ...line, x1: 0, y1: 2.5, x2: 0, y2: -12, strokeWidth: 2, transform: `rotate(${hours * 30})` }),
    jsx('line', { ...line, x1: 0, y1: 3, x2: 0, y2: -20, strokeWidth: 1.2, transform: `rotate(${minutes * 6})` }),
    jsx('path', { d: star(4, 4.2, 1.6, 0), fill: 'currentColor' })
  )
}

/** One profile's mark: a star whose points and turn come from the profile's name. */
function Sigil({ name, size = 16 }) {
  const seed = hash(name || '-')
  const points = 3 + (seed % 5)

  return el(
    'svg',
    { className: 'chm-sigil', width: size, height: size, viewBox: '-10 -10 20 20', 'aria-hidden': true },
    jsx('circle', { r: 8.6, fill: 'none', stroke: 'currentColor', strokeWidth: 1.1 }),
    jsx('path', { d: star(points, 7, 3, ((seed >> 4) % 6) / 6), fill: 'currentColor', opacity: 0.9 })
  )
}

// ── the header ──────────────────────────────────────────────────────────────

function Header({ data, horizon, setHorizon, t }) {
  const facts = data.summary.facts.map(fact =>
    el('li', { className: 'chm-fact', 'data-kind': fact.kind, key: fact.kind }, icon(FACT_ICON[fact.kind] || 'circle-outline'), fact.text)
  )

  return el(
    'header',
    { className: 'chm-head' },
    jsx(Astrolabe, { data }),
    el(
      'div',
      { className: 'chm-titles' },
      jsx('h1', { className: 'chm-title', children: t('title') }),
      jsx('p', { className: 'chm-headline', children: data.summary.headline }),
      facts.length ? jsx('ul', { className: 'chm-facts', children: facts }) : null
    ),
    el(
      'div',
      { className: 'chm-side' },
      jsx('div', {
        className: 'chm-seg',
        role: 'group',
        'aria-label': t('horizon.label'),
        children: HORIZONS.map(key =>
          jsx('button', { type: 'button', 'aria-pressed': horizon === key, onClick: () => setHorizon(key), children: t(`horizon.${key}`) }, key)
        )
      }),
      jsx('p', { className: 'chm-quiet', children: `${data.now_text} ${data.zone} · ${data.horizon.label}` })
    )
  )
}

function Filters({ data, lanes, filters, setFilters, t }) {
  const toggle = (kind, value) => {
    const next = new Set(filters[kind])

    if (next.has(value)) {
      next.delete(value)
    } else {
      next.add(value)
    }

    setFilters({ ...filters, [kind]: next })
  }

  const chips = [
    ...lanes.map(lane =>
      el(
        'button',
        {
          type: 'button',
          className: 'chm-chip',
          style: { '--lane': lane.color },
          'aria-pressed': !filters.profiles.has(lane.profile),
          'data-off': filters.profiles.has(lane.profile) ? '1' : '0',
          onClick: () => toggle('profiles', lane.profile),
          key: `p:${lane.profile}`
        },
        jsx(Sigil, { name: lane.profile, size: 13 }),
        lane.profile ? lane.label : t('noProfile')
      )
    ),
    ...data.labels.map(label =>
      el(
        'button',
        {
          type: 'button',
          className: 'chm-chip',
          'aria-pressed': filters.labels.has(label.name),
          title: label.exclusive ? `${label.description || label.name}: ${t('filter.oneAtATime')}` : label.description || label.name,
          onClick: () => toggle('labels', label.name),
          key: `l:${label.name}`
        },
        icon(label.exclusive ? 'lock' : 'tag', 11),
        label.name
      )
    )
  ]
  const active = filters.profiles.size || filters.labels.size || filters.clashes

  return el(
    'div',
    { className: 'chm-filters', role: 'group', 'aria-label': t('filter.label') },
    ...chips,
    data.counts.clashes
      ? el(
          'button',
          { type: 'button', className: 'chm-chip', 'aria-pressed': filters.clashes, onClick: () => setFilters({ ...filters, clashes: !filters.clashes }) },
          icon('zap', 11),
          t('filter.clashes')
        )
      : null,
    active
      ? jsx('button', {
          type: 'button',
          className: 'chm-plain',
          style: { fontSize: '0.75rem', textDecoration: 'underline' },
          onClick: () => setFilters({ profiles: new Set(), labels: new Set(), clashes: false }),
          children: t('filter.clear')
        })
      : null
  )
}

// ── the loom ────────────────────────────────────────────────────────────────

function barLabel(item, t) {
  return t('loom.bar', [
    item.title,
    t(`phase.${item.phase}`),
    item.when,
    item.pinned ? t('mark.pinned') : '',
    item.clash ? t('mark.clash') : item.busy ? t('mark.busy') : '',
    item.overrun_s ? t('mark.over') : ''
  ])
}

function Sparks({ seed, at }) {
  const sparks = []

  for (let i = 0; i < 12; i += 1) {
    const angle = (Math.PI * 2 * i) / 12 + (seed % 7) / 7
    const reach = 26 + ((seed >> (i % 8)) & 15)

    sparks.push(
      jsx('i', { className: 'chm-spark', style: { left: at, '--dx': `${(Math.cos(angle) * reach).toFixed(0)}px`, '--dy': `${(Math.sin(angle) * reach).toFixed(0)}px`, animationDelay: `${(i % 4) * 0.04}s` } }, i)
    )
  }

  return sparks
}

function Bar({ bar, lane, view, t }) {
  const { item } = bar
  const { layout, selected, drag, burst, filters } = view
  const top = lane.top + LANE_PAD + bar.row * (ROW_H + ROW_GAP)
  const dragging = Boolean(drag && drag.active && drag.id === item.task_id)
  const partner = Boolean(selected && selected !== item.task_id && item.clashes.some(c => c.kind === 'clash' && c.with === selected))
  const inside = !bar.tag && bar.w >= INSIDE
  const marks = []

  if (!bar.shift) {
    for (const clash of item.clashes) {
      if (clash.kind === 'clash') {
        const from = Math.max(0, layout.x(Math.max(clash.start, item.start)) - bar.x)
        const width = Math.min(bar.w, layout.x(Math.min(clash.end, item.end)) - bar.x) - from

        if (width > 1) {
          marks.push(jsx('i', { className: 'chm-hatch', style: { left: from, width } }, `c:${clash.with}`))
        }
      }
    }

    if (item.overrun_s) {
      marks.push(jsx('i', { className: 'chm-over', style: { left: Math.max(0, layout.x(item.end - item.overrun_s) - bar.x), right: 0 } }, 'over'))
    }
  }

  const flags = [item.pinned ? icon('pinned', 11) : null, item.clash ? icon('zap', 11) : null]

  return el(
    'button',
    {
      type: 'button',
      className: 'chm-bar',
      style: { left: LANE_W + bar.x, top, width: bar.span, '--lane': lane.color },
      title: `${item.title}\n${item.when}\n${item.line}`,
      'aria-label': barLabel(item, t),
      'aria-pressed': selected === item.task_id,
      'data-phase': item.phase,
      'data-clash': item.clash ? '1' : '0',
      'data-partner': partner ? '1' : '0',
      'data-sel': selected === item.task_id ? '1' : '0',
      'data-dim': matches(item, filters) ? '0' : '1',
      'data-move': item.can.move ? '1' : '0',
      'data-drag': dragging ? '1' : '0',
      onPointerDown: event => view.grab(event, item, 'move'),
      onClick: () => view.select(item.task_id),
      onKeyDown: event => {
        if (item.can.move && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
          event.preventDefault()
          void view.nudge(item, event.key === 'ArrowLeft' ? -SNAP_S : SNAP_S)
        }
      }
    },
    el(
      'span',
      { className: 'chm-body', style: { width: bar.w }, 'data-thin': bar.w < 30 ? '1' : '0' },
      ...marks,
      bar.w >= 18 ? icon(PHASE_ICON[item.phase] || 'circle-outline') : null,
      inside ? jsx('span', { className: 'chm-bar-title', children: item.short }) : null,
      ...(inside ? flags : [])
    ),
    bar.tag ? el('span', { className: 'chm-tag' }, ...flags, jsx('span', { children: item.short })) : null,
    burst && burst.id === item.task_id ? jsx(Sparks, { seed: burst.seed, at: bar.w / 2 }, burst.seed) : null
  )
}

function Loom({ data, view, t }) {
  const { layout, drag, clock, now } = view
  const within = ts => ts >= layout.start && ts <= layout.end
  const parts = []

  // the sky: nights, the past, day and hour lines
  for (const night of data.horizon.nights) {
    parts.push(jsx('div', { className: 'chm-night', style: { left: LANE_W + layout.x(night.start), width: layout.x(night.end) - layout.x(night.start), top: HEAD_H } }, `n:${night.start}`))
  }

  if (now > layout.start) {
    parts.push(jsx('div', { className: 'chm-past', style: { width: Math.min(layout.plot, layout.x(now)), top: HEAD_H } }, 'past'))
  }

  for (const day of data.horizon.days) {
    parts.push(
      jsx('div', {
        className: 'chm-day-label',
        'data-today': day.today ? '1' : '0',
        style: { left: LANE_W + layout.x(day.start), maxWidth: Math.max(0, layout.x(day.end) - layout.x(day.start) - 8) },
        children: day.label
      }, `d:${day.start}`)
    )
  }

  const gap = layout.plot / Math.max(1, data.horizon.ticks.length)
  const every = Math.max(1, Math.ceil(44 / Math.max(1, gap)))

  data.horizon.ticks.forEach((tick, index) => {
    const left = LANE_W + layout.x(tick.ts)

    parts.push(jsx('div', { className: 'chm-tick', 'data-day': tick.day ? '1' : '0', style: { left } }, `t:${tick.ts}`))

    if (!tick.day && index % every === 0 && left < LANE_W + layout.plot - 30) {
      parts.push(jsx('div', { className: 'chm-tick-label', style: { left }, children: tick.label }, `tl:${tick.ts}`))
    }
  })

  // the lanes and their bars
  for (const lane of layout.lanes) {
    parts.push(
      jsx('div', {
        className: 'chm-lane',
        style: { top: lane.top, height: lane.height, '--lane': lane.color },
        children: el('div', { className: 'chm-lane-name', title: lane.label }, jsx(Sigil, { name: lane.profile }), jsx('span', { children: lane.profile ? lane.label : t('noProfile') }))
      }, `lane:${lane.profile}`)
    )

    for (const bar of lane.bars) {
      parts.push(jsx(Bar, { bar, lane, view, t }, `b:${bar.item.task_id}`))
    }
  }

  if (!data.items.length) {
    parts.push(jsx('p', { className: 'chm-empty', style: { top: HEAD_H + 20 }, children: t('loom.empty') }, 'empty'))
  }

  // the needle, and the guide while something is dragged
  if (within(now)) {
    const left = LANE_W + layout.x(now)

    parts.push(jsx('div', { className: 'chm-now', style: { left } }, 'now'))
    parts.push(jsx('div', { className: 'chm-now-label', style: { left }, children: t('now', clock(now).split(' ')[1]) }, 'now-label'))
  }

  if (drag && drag.active && drag.start !== null && within(drag.start)) {
    parts.push(
      jsx('div', {
        className: 'chm-guide',
        style: { left: LANE_W + layout.x(drag.start) },
        children: jsx('span', { className: 'chm-guide-label', children: t(drag.kind === 'move' ? 'loom.moveTo' : 'loom.planAt', clock(drag.start)) })
      }, 'guide')
    )
  }

  return jsx('div', {
    className: 'chm-loom',
    ref: view.frame,
    role: 'group',
    'aria-label': t('loom.label'),
    style: { height: layout.height },
    onClick: event => {
      if (event.target === event.currentTarget) {
        view.select(null)
      }
    },
    children: parts
  })
}

/** The narrow view: the same slots as a list, day by day. No dragging; the scroll has the moves. */
function Agenda({ data, view, t }) {
  const colors = Object.fromEntries(view.layout.lanes.map(lane => [lane.profile, lane.color]))
  const rows = []

  for (const day of data.horizon.days) {
    const items = data.items
      .filter(item => !view.filters.profiles.has(item.assignee))
      .filter(item => {
        const at = Math.max(item.start, data.horizon.start)

        return at >= day.start && at < day.end
      })
      .sort((a, b) => a.start - b.start)

    if (!items.length) {
      continue
    }

    rows.push(jsx('h3', { children: day.label }, `h:${day.start}`))

    for (const item of items) {
      rows.push(
        el(
          'button',
          {
            type: 'button',
            className: 'chm-row',
            style: { '--lane': colors[item.assignee] },
            'data-sel': view.selected === item.task_id ? '1' : '0',
            'data-dim': matches(item, view.filters) ? '0' : '1',
            'aria-label': barLabel(item, t),
            onClick: () => view.select(item.task_id),
            key: item.task_id
          },
          jsx(Sigil, { name: item.assignee }),
          jsx('span', { className: 'chm-id', children: view.clock(item.start).split(' ')[1] }),
          jsx('span', { className: 'chm-row-title', children: item.short }),
          item.clash ? icon('zap') : null,
          item.pinned ? icon('pinned') : null,
          jsx(Badge, { variant: 'outline', children: t(`phase.${item.phase}`) })
        )
      )
    }
  }

  return jsx('div', { className: 'chm-agenda', children: rows.length ? rows : jsx('p', { className: 'chm-p', children: t('loom.empty') }) })
}

// ── the scroll: one selected card ───────────────────────────────────────────

const BADGE_VARIANT = { running: 'success', done: 'muted', missed: 'warn', failed: 'warn', gone: 'warn', due: 'warn', planned: 'default', waiting: 'default' }

function Scroll({ item, data, color, view, t }) {
  const [start, setStart] = useState(item.start_local || data.suggest_local)
  const [minutes, setMinutes] = useState(String(item.estimate_min || data.default_estimate_min))
  const [label, setLabel] = useState('')
  const planned = item.planned_start !== null && item.planned_start !== undefined

  useEffect(() => {
    setStart(item.start_local || data.suggest_local)
    setMinutes(String(item.estimate_min || data.default_estimate_min))
  }, [item.task_id, item.start_local, item.estimate_min])

  const save = event => {
    event.preventDefault()

    const estimate = Math.round(Number(minutes))

    void view.run(() => api.slot(item.task_id, { start, ...(estimate > 0 ? { estimate_minutes: estimate } : {}) }), item.task_id)
  }

  const addLabel = event => {
    event.preventDefault()

    const name = label.trim()

    if (name) {
      setLabel('')
      void view.run(() => api.tag(item.task_id, [name], []))
    }
  }

  const known = new Set(item.labels)
  const exclusive = new Set(data.labels.filter(l => l.exclusive).map(l => l.name))
  const derived = new Set(data.labels.filter(l => l.derived).map(l => l.name))

  return el(
    'article',
    { className: 'chm-card', 'data-lit': '1', style: { '--lane': color } },
    el(
      'div',
      { className: 'chm-wrap' },
      jsx(Sigil, { name: item.assignee, size: 18 }),
      jsx('span', { className: 'chm-quiet', style: { fontWeight: 600 }, children: item.assignee || t('noProfile') }),
      jsx(Badge, { variant: BADGE_VARIANT[item.phase] || 'outline', children: t(`phase.${item.phase}`) }),
      item.pinned ? jsx(Badge, { variant: 'outline', children: t('mark.pinned') }) : null,
      jsx('span', { style: { flex: '1 1 auto' } }),
      jsx('button', { type: 'button', className: 'chm-plain', 'aria-label': t('action.close'), onClick: () => view.select(null), children: icon('close', 14) })
    ),
    jsx('h3', { className: 'chm-name', children: item.title }),
    item.when ? jsx('p', { className: 'chm-p', style: { fontFamily: 'var(--chm-mono)' }, children: `${item.when}${item.estimate ? ` · ${item.estimate}` : ''}` }) : null,
    jsx('p', { className: 'chm-p', children: item.line }),
    item.set_by_text ? jsx('p', { className: 'chm-quiet', children: item.set_by_text }) : null,
    item.clashes.length
      ? jsx('div', {
          className: 'chm-list',
          role: 'list',
          'aria-label': t('scroll.clashes'),
          children: item.clashes.map(clash =>
            el(
              'div',
              { className: 'chm-clash', 'data-kind': clash.kind, role: 'listitem', key: clash.with },
              clash.text,
              ' ',
              jsx('button', { type: 'button', className: 'chm-plain', style: { textDecoration: 'underline', fontSize: '0.75rem' }, onClick: () => view.select(clash.with), children: t('scroll.card', clash.with) })
            )
          )
        })
      : null,
    item.next_free_text && item.can.move
      ? jsx('div', {
          children: jsx(Button, { size: 'sm', variant: 'outline', onClick: () => void view.commit(item, item.next_free), children: t('action.nextFree', item.next_free_text) })
        })
      : null,
    item.can.move
      ? el(
          'form',
          { className: 'chm-form', onSubmit: save },
          el('label', { className: 'chm-field' }, t('scroll.start'), jsx('input', { className: 'chm-input', type: 'datetime-local', value: start, required: true, onChange: event => setStart(event.target.value) })),
          el('label', { className: 'chm-field' }, t('scroll.estimate'), jsx('input', { className: 'chm-input', type: 'number', min: 1, max: 20160, style: { width: '6.5rem' }, value: minutes, onChange: event => setMinutes(event.target.value) })),
          jsx(Button, { size: 'sm', type: 'submit', children: t(planned ? 'action.save' : 'action.plan') })
        )
      : null,
    el(
      'div',
      { className: 'chm-wrap' },
      item.can.now ? jsx(Button, { size: 'sm', onClick: () => view.ask(item), children: el('span', { className: 'chm-wrap' }, icon('wand'), t('action.now')) }) : null,
      item.can.move && planned ? jsx(Button, { size: 'sm', variant: 'outline', onClick: () => void view.nudge(item, -SNAP_S), children: t('action.earlier') }) : null,
      item.can.move && planned ? jsx(Button, { size: 'sm', variant: 'outline', onClick: () => void view.nudge(item, SNAP_S), children: t('action.later') }) : null,
      item.can.pin ? jsx(Button, { size: 'sm', variant: 'outline', onClick: () => void view.run(() => api.pin(item.task_id, !item.pinned)), children: t(item.pinned ? 'action.unpin' : 'action.pin') }) : null,
      item.can.clear ? jsx(Button, { size: 'sm', variant: 'outline', onClick: () => void view.run(() => api.clear(item.task_id)), children: t('action.clear') }) : null
    ),
    jsx('div', {
      className: 'chm-wrap',
      role: 'group',
      'aria-label': t('scroll.labels'),
      children: item.labels.length
        ? item.labels.map(name =>
            el(
              'span',
              { className: 'chm-chip', style: { cursor: 'default' }, key: name },
              icon(exclusive.has(name) ? 'lock' : 'tag', 11),
              name,
              derived.has(name)
                ? null
                : jsx('button', { type: 'button', className: 'chm-plain', 'aria-label': t('action.remove', name), onClick: () => void view.run(() => api.tag(item.task_id, [], [name])), children: icon('close', 10) })
            )
          )
        : jsx('span', { className: 'chm-quiet', children: t('scroll.noLabels') })
    }),
    el(
      'form',
      { className: 'chm-form', onSubmit: addLabel },
      jsx('input', { className: 'chm-input', style: { flex: '1 1 10rem' }, value: label, list: 'chm-labels', placeholder: t('scroll.addLabel'), 'aria-label': t('scroll.addLabel'), onChange: event => setLabel(event.target.value) }),
      jsx('datalist', { id: 'chm-labels', children: data.labels.filter(l => !l.derived && !known.has(l.name)).map(l => jsx('option', { value: l.name }, l.name)) }),
      jsx(Button, { size: 'sm', variant: 'outline', type: 'submit', children: t('action.add') })
    ),
    el(
      'div',
      { className: 'chm-wrap' },
      jsx('span', { className: 'chm-id', children: item.task_id }),
      jsx('button', { type: 'button', className: 'chm-plain', style: { fontSize: '0.75rem', textDecoration: 'underline' }, onClick: () => host.navigate(BOARD_PATH), children: t('scroll.board') })
    )
  )
}

// ── the tray and the chronicle ──────────────────────────────────────────────

function Tray({ data, view, t }) {
  const colors = Object.fromEntries(view.layout.lanes.map(lane => [lane.profile, lane.color]))
  const rows = data.tray.filter(item => !view.filters.profiles.has(item.assignee))

  return el(
    'section',
    { className: 'chm-section' },
    el('h2', {}, icon('inbox', 14), t('tray.title'), jsx(Badge, { variant: 'muted', children: String(rows.length) })),
    rows.length
      ? jsx('ul', {
          className: 'chm-list',
          children: rows.map(item =>
            jsx('li', {
              children: el(
                'button',
                {
                  type: 'button',
                  className: 'chm-row',
                  style: { '--lane': colors[item.assignee], cursor: item.can.move ? 'grab' : 'pointer' },
                  title: `${item.title}\n${item.line}`,
                  'data-sel': view.selected === item.task_id ? '1' : '0',
                  'data-dim': matches(item, { ...view.filters, clashes: false }) ? '0' : '1',
                  onPointerDown: event => view.grab(event, item, 'plan'),
                  onClick: () => view.select(item.task_id)
                },
                jsx(Sigil, { name: item.assignee }),
                jsx('span', { className: 'chm-row-title', children: item.short }),
                jsx(Badge, { variant: 'outline', children: item.status })
              )
            }, item.task_id)
          )
        })
      : jsx('p', { className: 'chm-p', children: t('tray.empty') }),
    rows.length ? jsx('p', { className: 'chm-quiet', children: t('tray.hint') }) : null
  )
}

const ACTOR_ICON = { owner: 'account', timer: 'watch' }

function Chronicle({ data, t }) {
  return el(
    'section',
    { className: 'chm-section' },
    el('h2', {}, icon('history', 14), t('chronicle.title')),
    data.log.length
      ? jsx('div', {
          role: 'log',
          children: data.log.map(entry =>
            el(
              'div',
              { className: 'chm-entry', 'data-owner': entry.by_owner ? '1' : '0', key: entry.id },
              jsx('time', { children: entry.when }),
              icon(ACTOR_ICON[entry.actor] || 'sparkle'),
              jsx('span', { children: entry.text })
            )
          )
        })
      : jsx('p', { className: 'chm-p', children: t('chronicle.empty') })
  )
}

// ── the page ────────────────────────────────────────────────────────────────

const $wide = atom({ narrow: false })
const NO_FILTERS = { profiles: new Set(), labels: new Set(), clashes: false }

function ChronomancerPage() {
  const t = usePluginI18n(ID)
  const viewport = useValue((host.state && host.state.viewport) || $wide)
  const [horizon, setHorizonState] = useState('2d')
  const [filters, setFilters] = useState(NO_FILTERS)
  const [selected, setSelected] = useState(null)
  const [width, setWidth] = useState(0)
  const [drag, setDrag] = useState(null) // { id, kind: 'move' | 'plan', active, start } while a pointer is down
  const [moved, setMoved] = useState(null) // { id, start }: where a bar sits until the backend confirms the move
  const [pending, setPending] = useState(null) // the card whose "cast now" dialog is open
  const [burst, setBurst] = useState(null) // { id, seed }: sparks on a bar after a move
  const frame = useRef(null)
  const live = useRef({})
  const query = usePlan(horizon)
  const data = query.data

  useEffect(() => {
    let alive = true

    Promise.resolve(storage && storage.get('horizon'))
      .then(value => {
        if (alive && HORIZONS.includes(value)) {
          setHorizonState(value)
        }
      })
      .catch(() => {})

    return () => {
      alive = false
    }
  }, [])

  useEffect(() => {
    const node = frame.current

    if (!node || typeof ResizeObserver === 'undefined') {
      return undefined
    }

    const observer = new ResizeObserver(entries => setWidth(Math.round(entries[0].contentRect.width)))

    observer.observe(node)

    return () => observer.disconnect()
  }, [Boolean(data)])

  // A confirmed plan replaces the optimistic position.
  useEffect(() => setMoved(null), [query.dataUpdatedAt])

  const setHorizon = key => {
    setHorizonState(key)

    try {
      void Promise.resolve(storage && storage.set('horizon', key)).catch(() => {})
    } catch {
      // storage is a convenience
    }
  }

  const now = nowOf(data)
  const shifted = moved || (drag && drag.active && drag.kind === 'move' ? drag : null)
  const layout = useMemo(() => (data ? layoutOf(data, width || 960, filters, shifted) : null), [data, width, filters, shifted])
  const clock = useMemo(() => (data ? clockOf(data) : null), [data && data.tz, data && data.utc_offset_s])

  live.current = { layout, now }

  const run = async (call, burstId) => {
    const ok = await move(call, t('failed'))

    if (ok && burstId) {
      setBurst({ id: burstId, seed: (Date.now() & 0xffff) | 1 })
      setTimeout(() => setBurst(null), 1100)
    }

    return ok
  }

  const commit = (item, start) => {
    setMoved({ id: item.task_id, start })

    return run(() => api.slot(item.task_id, { start }), item.task_id)
  }

  const nudge = (item, delta) => {
    const from = item.planned_start !== null && item.planned_start !== undefined ? item.planned_start : soonest(now)

    return commit(item, Math.max(soonest(now), snap(from + delta)))
  }

  /** A pointer went down on a bar or on a tray card: follow it until it comes up. */
  const grab = (event, item, kind) => {
    if (!item.can.move || event.button !== 0 || !live.current.layout || (viewport && viewport.narrow)) {
      return
    }

    const originX = event.clientX
    const origin = item.start
    let active = false
    let start = null

    const tsAt = clientX => {
      const { layout: l, now: at } = live.current
      const box = frame.current && frame.current.getBoundingClientRect()

      if (!box || !l) {
        return null
      }

      if (kind === 'move') {
        return Math.max(soonest(at), snap(origin + (clientX - originX) / l.scale))
      }

      const offset = clientX - box.left - LANE_W

      return offset < 0 || offset > l.plot ? null : Math.max(soonest(at), snap(l.start + offset / l.scale))
    }

    const onMove = e => {
      if (!active && Math.abs(e.clientX - originX) < 5) {
        return
      }

      active = true
      start = tsAt(e.clientX)
      setDrag({ id: item.task_id, kind, active: true, start })
    }

    const onUp = e => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      setDrag(null)

      if (!active || e.type === 'pointercancel') {
        return
      }

      // The click that follows a drag must not select or deselect anything.
      const swallow = click => click.stopPropagation()

      window.addEventListener('click', swallow, { capture: true, once: true })
      setTimeout(() => window.removeEventListener('click', swallow, { capture: true }), 80)

      if (start !== null && start !== origin) {
        void commit(item, start)
      }
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
  }

  let content

  if (!data || !layout) {
    content = query.isError
      ? jsx(ErrorState, {
          title: t('loadFailed'),
          description: errorText(query.error, t('loadFailedHint')),
          children: jsx(Button, { size: 'touch', variant: 'outline', onClick: () => void query.refetch(), children: t('retry') })
        })
      : jsx('p', { className: 'chm-p', children: t('loading') })
  } else {
    const ladder = Boolean(viewport && viewport.narrow) || (width > 0 && width < 620)
    const chosen = [...data.items, ...data.tray].find(item => item.task_id === selected) || null
    const allLanes = data.lanes.map((lane, index) => ({ ...lane, color: laneColor(lane.profile, index) }))
    const colorOf = profile => (allLanes.find(lane => lane.profile === profile) || {}).color
    const view = { layout, now, clock, selected, drag, burst, filters, frame, grab, nudge, run, commit, select: setSelected, ask: setPending }

    content = [
      jsx(Header, { data, horizon, setHorizon, t }, 'head'),
      query.isError ? jsx('p', { className: 'chm-notice', role: 'status', children: t('stale') }, 'stale') : null,
      data.board_error ? jsx('p', { className: 'chm-notice', role: 'status', children: `${t('boardDown')}: ${data.board_error}` }, 'board') : null,
      jsx(Filters, { data, lanes: allLanes, filters, setFilters, t }, 'filters'),
      el(
        'section',
        { className: 'chm-section', key: 'loom' },
        el('h2', {}, icon('wand', 14), t('loom.title')),
        ladder ? jsx('div', { ref: frame, children: jsx(Agenda, { data, view, t }) }) : jsx(Loom, { data, view, t }),
        data.later.text ? jsx('p', { className: 'chm-quiet', children: data.later.text }) : null,
        ladder ? null : jsx('p', { className: 'chm-quiet', children: t('loom.hint') })
      ),
      el(
        'div',
        { className: 'chm-two', key: 'two' },
        el(
          'section',
          { className: 'chm-section' },
          el('h2', {}, icon('book', 14), t('scroll.title')),
          chosen ? jsx(Scroll, { item: chosen, data, color: colorOf(chosen.assignee), view, t }, chosen.task_id) : jsx('p', { className: 'chm-p', children: t('scroll.none') })
        ),
        jsx(Tray, { data, view, t })
      ),
      jsx(Chronicle, { data, t }, 'chronicle')
    ]
  }

  const dialog = pending
    ? jsx(ConfirmDialog, {
        open: true,
        onClose: () => setPending(null),
        onConfirm: async () => {
          const item = pending

          setPending(null)
          await run(() => api.now(item.task_id), item.task_id)
        },
        title: t('confirm.nowTitle'),
        description: t('confirm.nowBody', pending.short),
        confirmLabel: t('confirm.nowAction')
      })
    : null

  return el('div', { className: 'chm' }, jsx('style', { children: CSS }), jsx('div', { className: 'chm-inner', children: content }), dialog)
}

// ── status-bar chip ─────────────────────────────────────────────────────────

// The chip lives outside the page, where the page's stylesheet is not mounted.
const STATUS_CHIP = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '0.25rem',
  height: '100%',
  padding: '0 0.375rem',
  font: 'inherit',
  fontSize: '0.6875rem',
  color: 'var(--ui-text-primary)',
  background: 'transparent',
  border: 0,
  cursor: 'pointer',
  whiteSpace: 'nowrap'
}

function StatusChip() {
  const t = usePluginI18n(ID)
  const { data } = usePlan('2d')

  if (!data) {
    return null
  }

  return jsx(Tip, {
    label: t('chipTip', data.summary.tip),
    children: el(
      'button',
      { type: 'button', style: STATUS_CHIP, onClick: () => host.navigate(PATH) },
      icon(data.counts.clashes ? 'zap' : 'wand'),
      jsx('span', { children: data.summary.chip })
    )
  })
}

export default {
  id: ID,
  name: 'Chronomancer',
  register(ctx) {
    rest = ctx.rest
    storage = ctx.storage || null
    ctx.i18n.register({ en: EN })

    const t = ctx.i18n.t

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, title: t('title'), data: { path: PATH }, render: () => jsx(ChronomancerPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: t('title'), codicon: 'wand' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 140, render: () => jsx(StatusChip, {}) },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: `${ID}.open`, label: t('open'), keywords: ['plan', 'slot', 'kanban', 'timeline', 'schedule', 'clash'], run: () => host.navigate(PATH) }
      }
    ])

    queryClient.prefetchQuery({ queryKey: planKey('2d'), queryFn: () => fetchPlan('2d') }).catch(() => {})
  }
}
