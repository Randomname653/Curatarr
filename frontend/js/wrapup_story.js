// ── Wrap-Up story mode: one idea per screen, the identity last ──────────────
// v1 put everything on one scroll: the archetype first (the conclusion before
// the evidence), fifteen tiles of equal weight, the story under them. Here the
// period is told as a paced sequence (docs/WRAPUP_V2_BLUEPRINT.md §4.2):
// a cold-open number, a prediction, the reveal, the rhythm, the curator's
// slides in the editor's order, the roast and the toast, and only then who you
// were — then the send-off. Slides without data are skipped, so a quiet month
// is short. The bento stays behind it as "Explore".
//
// Charts are SVG with attributes and classes only (style-src is 'self').
import { _posterImg, act, countTo, esc, haptic, loc } from './ui.js';
import { brandMark } from './brand.js';
import { t, th } from './i18n.js';

// How long a slide stays: long enough to read it. A fixed 7 s cut the
// curator's longer slides off mid-sentence (2026-10-25). About three words a
// second after a 3 s look, between 6 and 20 s.
const DWELL_BASE_MS = 3000, DWELL_PER_WORD_MS = 330, DWELL_MIN_MS = 6000, DWELL_MAX_MS = 20000;

/** The time a slide's html needs to be read, in ms. */
export function dwellMs(html) {
  const words = String(html || '').replace(/<[^>]*>/g, ' ').split(/\s+/).filter(Boolean).length;
  return Math.min(DWELL_MAX_MS, Math.max(DWELL_MIN_MS, DWELL_BASE_MS + words * DWELL_PER_WORD_MS));
}
const REDUCED = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : {matches: false};
const HOLD_MS = 400;
const st = {slides: [], i: 0, timer: null, paused: false, el: null, data: null, answered: null, downAt: 0};
const _wasHold = () => Date.now() - st.downAt > HOLD_MS && st.downAt > 0;

const _s = v => esc(String(v ?? ''));
const _pct = v => Math.round(100 * (v || 0));
const _fmt = n => (Math.round((n || 0) * 10) / 10).toLocaleString(loc());
// "12.5 hours": the plural by the number, the number as the locale writes it
const _hours = n => t('story.hours', {n: Math.round((n || 0) * 10) / 10, h: _fmt(n)});

/** A period's name in the UI language, from its key (services/wrapup_facts
 *  .resolve_period writes the English ``label`` the curator reads). */
export function periodLabel(p) {
  const k = String(p?.key || '');
  let m;
  if ((m = k.match(/^week:(\d{4})-W(\d{2})$/))) return t('wrapup.period.week', {w: +m[2], y: m[1]});
  if ((m = k.match(/^month:(\d{4})-(\d{2})$/))) {
    return new Date(Date.UTC(+m[1], +m[2] - 1, 1)).toLocaleDateString(loc(), {month: 'long', year: 'numeric', timeZone: 'UTC'});
  }
  if ((m = k.match(/^season:(\d{4})-(winter|spring|summer|fall)$/))) return seasonLabel(k, true);
  if ((m = k.match(/^year:(\d{4})$/))) return t('wrapup.period.year', {y: m[1]});
  if (['30d', '6m', 'all'].includes(k)) return t(`wrapup.period.${k}`);
  return p?.label || '';
}
/** "Fall 2026" (a chip), or "Fall 2026 season" (a heading). */
export function seasonLabel(key, long = false) {
  const [y, s] = String(key).replace('season:', '').split('-');
  const name = ['winter', 'spring', 'summer', 'fall'].includes(s) ? t(`wrapup.season.${s}`) : (s || '');
  return t(long ? 'wrapup.period.season' : 'wrapup.season_chip', {season: name, y});
}

// ── charts ──────────────────────────────────────────────────────────────────
/** GitHub-style grid of the period (the last 365 days of a longer one): a
 *  column per week, a row per weekday, four levels by the period's quartiles. */
export function heatmap(byDay, startIso, endIso) {
  const days = Object.keys(byDay || {});
  if (!days.length) return '';
  const end = new Date(endIso), start0 = new Date(startIso);
  const start = new Date(Math.max(start0.getTime(), end.getTime() - 364 * 86400000));
  const vals = days.map(d => byDay[d]).filter(v => v > 0).sort((a, b) => a - b);
  const q = p => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))] || 0;
  const cuts = [q(0.25), q(0.5), q(0.75)];
  const lvl = v => !v ? 0 : v <= cuts[0] ? 1 : v <= cuts[1] ? 2 : v <= cuts[2] ? 3 : 4;
  const offset = (start.getUTCDay() + 6) % 7;            // Monday = row 0
  const span = Math.round((end - start) / 86400000);
  const cells = [];
  for (let i = 0; i <= span; i++) {
    const iso = new Date(start.getTime() + i * 86400000).toISOString().slice(0, 10);
    const k = i + offset, col = Math.floor(k / 7), row = k % 7, h = byDay[iso] || 0;
    cells.push(`<rect x="${col * 13}" y="${row * 13}" width="11" height="11" rx="2" class="wus-hm l${lvl(h)}"><title>${_s(iso)}${h ? ` · ${_fmt(h)} h` : ''}</title></rect>`);
  }
  const cols = Math.floor((span + offset) / 7) + 1;
  // drawn at most 22px a column: a month is a small block, a year fills the width
  return `<svg class="wus-chart wus-heat" width="${Math.min(560, cols * 22)}" viewBox="0 0 ${cols * 13} ${7 * 13}" role="img" aria-label="${th('story.heat_aria', {n: days.length})}">${cells.join('')}</svg>`;
}

/** Six axes of taste, 0..1 each; at least four must be known. */
export function radarAxes(f) {
  const tp = f.tempo || {}, top = (f.top || [])[0];
  const ax = [
    ['Acclaim', f.highbrow_comfort?.highbrow],
    ['Novelty', f.discovery?.new_share],
    ['Commitment', f.completion?.completion_rate],
    ['Catalogue', f.eras?.split?.catalogue],
    ['Contrarian', f.contrarian ? Math.min(1, (f.contrarian.gap || 0) / 3) : (f.highbrow_comfort ? 0 : undefined)],
    ['Focus', top && tp.hours ? Math.min(1, top.hours / tp.hours * 2) : undefined],
  ];
  return ax.filter(([, v]) => typeof v === 'number' && !Number.isNaN(v));
}

export function radar(f, median = null) {
  const ax = radarAxes(f);
  if (ax.length < 4) return '';
  const c = 110, r = 80, n = ax.length;
  const pt = (i, v) => {
    const a = -Math.PI / 2 + i * 2 * Math.PI / n;
    return [c + Math.cos(a) * r * v, c + Math.sin(a) * r * v];
  };
  const ring = v => ax.map((_, i) => pt(i, v).map(x => x.toFixed(1)).join(',')).join(' ');
  const shape = ax.map(([, v], i) => pt(i, Math.max(0.04, v)).map(x => x.toFixed(1)).join(',')).join(' ');
  // the household median (opt-in, services/household_compare): drawn only
  // when it has every axis the user's own shape has
  const med = median && ax.every(([name]) => typeof median[name] === 'number')
    ? `<polygon points="${ax.map(([name], i) => pt(i, Math.max(0.04, median[name])).map(x => x.toFixed(1)).join(',')).join(' ')}" class="wus-rd-median"><title>${th('story.household_median')}</title></polygon>`
    : '';
  const labels = ax.map(([name, v], i) => {
    const [x, y] = pt(i, 1.22);
    return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" class="wus-rd-lbl" text-anchor="middle">${th(`story.axis.${name}`)} ${_pct(v)}%</text>`;
  }).join('');
  return `<svg class="wus-chart wus-radar" width="300" viewBox="0 0 220 220" role="img" aria-label="${th('story.radar_aria', {list: ax.map(([n, v]) => `${t(`story.axis.${n}`)} ${_pct(v)}%`).join(', ')})}">
    <polygon points="${ring(1)}" class="wus-rd-grid"/><polygon points="${ring(0.5)}" class="wus-rd-grid"/>
    ${med}<polygon points="${shape}" class="wus-rd-shape"/>${labels}</svg>`;
}

/** Hoarder vs pruner: GB in up, GB out down, by month. */
export function balance(v) {
  const months = Object.entries(v?.by_month || {});
  if (!months.length) return '';
  const max = Math.max(1, ...months.map(([, m]) => Math.max(m.in, m.out)));
  const w = 300 / months.length, mid = 60;
  const bars = months.map(([k, m], i) => {
    const hi = m.in / max * 55, ho = m.out / max * 55, x = (i * w + w * 0.15).toFixed(1), bw = (w * 0.7).toFixed(1);
    return `<rect x="${x}" y="${(mid - hi).toFixed(1)}" width="${bw}" height="${hi.toFixed(1)}" class="wus-bal-in"><title>${th('story.gb_in', {k, gb: _fmt(m.in)})}</title></rect>
      <rect x="${x}" y="${mid}" width="${bw}" height="${ho.toFixed(1)}" class="wus-bal-out"><title>${th('story.gb_out', {k, gb: _fmt(m.out)})}</title></rect>`;
  }).join('');
  return `<svg class="wus-chart wus-bal" width="460" viewBox="0 0 300 120" role="img" aria-label="${th('story.balance_aria', {gb_in: _fmt(v.gb_in), gb_out: _fmt(v.gb_out)})}">
    <line x1="0" x2="300" y1="${mid}" y2="${mid}" class="wus-axis"/>${bars}</svg>`;
}

/** The court: debates won and lost as two bars, and the boldest keep. */
// Your decades: one bar per decade, the leading one in amber (SVG, by class).
export function decades(e) {
  const d = Object.entries(e?.decades || {});
  if (d.length < 2) return '';
  const max = Math.max(...d.map(([, v]) => v)) || 1, W = 300, row = 26;
  const bars = d.map(([k, v], i) => {
    const y = i * row, w = Math.max(2, Math.round((W - 110) * v / max));
    return `<text x="0" y="${y + 13}" class="wus-dec-k${v === max ? ' is-lead' : ''}" dominant-baseline="middle">${_s(k)}</text>
      <rect x="60" y="${y + 6}" width="${w}" height="14" rx="4" class="wus-dec-bar${v === max ? ' is-lead' : ''}"><title>${_s(k)} · ${Math.round(100 * v)}%</title></rect>
      <text x="${66 + w}" y="${y + 13}" class="wus-dec-v" dominant-baseline="middle">${Math.round(100 * v)}%</text>`;
  }).join('');
  return `<svg class="wus-dec" viewBox="0 0 ${W} ${d.length * row}" role="img" aria-label="${th('story.decades_aria')}">${bars}</svg>`;
}

export function court(c) {
  if (!c) return '';
  const tot = Math.max(1, (c.won || 0) + (c.lost || 0));
  const bar = (n, cls, label) => `<div class="wus-ct-row"><span>${_s(label)}</span>
    <svg viewBox="0 0 100 8" class="wus-ct-bar" aria-hidden="true"><rect width="100" height="8" rx="4" class="wus-ct-bg"/>
    <rect width="${(100 * n / tot).toFixed(1)}" height="8" rx="4" class="${cls}"/></svg><b>${_s(n)}</b></div>`;
  return `<div class="wus-court">${bar(c.won || 0, 'wus-ct-you', t('story.court_won'))}${bar(c.lost || 0, 'wus-ct-cur', t('story.court_lost'))}
    <div class="wus-ct-meta">${th('story.court_meta', {decided: c.decided ?? '', kept: c.kept ?? '', deleted: c.deleted ?? '', overrides: c.overrides ?? ''})}</div></div>`;
}

// Weekday names as the locale writes them, Monday = 0 (1 Jan 2024 was a Monday).
const _weekday = (d, weekday) => new Date(Date.UTC(2024, 0, 1 + d))
  .toLocaleDateString(loc(), {weekday, timeZone: 'UTC'});

/** The rhythm clock: 24 hour sectors, a ring per weekday (Monday inside),
 *  four levels by the quartiles of the active cells. */
export function clock(rows) {
  const cells = (rows || []).flat().filter(v => v > 0).sort((a, b) => a - b);
  if (cells.length < 10) return '';
  const q = p => cells[Math.min(cells.length - 1, Math.floor(p * cells.length))];
  const cuts = [q(0.25), q(0.5), q(0.75)];
  const lvl = v => !v ? 0 : v <= cuts[0] ? 1 : v <= cuts[1] ? 2 : v <= cuts[2] ? 3 : 4;
  const c = 120, r0 = 30, ring = 10;            // the hour marks sit inside the 240 box
  const pt = (r, a) => [c + r * Math.sin(a), c - r * Math.cos(a)].map(x => x.toFixed(1));
  const arcs = [];
  rows.forEach((row, d) => row.forEach((v, h) => {
    const a0 = h * Math.PI / 12 + 0.012, a1 = (h + 1) * Math.PI / 12 - 0.012;
    const ri = r0 + d * ring + 1, ro = r0 + (d + 1) * ring - 1;
    const [x1, y1] = pt(ro, a0), [x2, y2] = pt(ro, a1), [x3, y3] = pt(ri, a1), [x4, y4] = pt(ri, a0);
    arcs.push(`<path d="M${x1} ${y1}A${ro} ${ro} 0 0 1 ${x2} ${y2}L${x3} ${y3}A${ri} ${ri} 0 0 0 ${x4} ${y4}Z" class="wus-hm l${lvl(v)}"><title>${_s(_weekday(d, 'short'))} ${h}:00${v ? ` · ${_fmt(v)} h` : ''}</title></path>`);
  }));
  const ticks = [0, 6, 12, 18].map(h => {
    const [x, y] = pt(r0 + 7 * ring + 10, h * Math.PI / 12);
    return `<text x="${x}" y="${y}" class="wus-rd-lbl" text-anchor="middle" dominant-baseline="middle">${h}</text>`;
  }).join('');
  return `<svg class="wus-chart wus-clock" width="300" viewBox="0 0 240 240" role="img" aria-label="${th('story.clock_aria')}">${arcs.join('')}${ticks}</svg>`;
}

/** The taste river: the leading nanogenres' hours stacked month by month
 *  around a centre line, each a shade of the accent, named at its widest. */
export function river(byMonth) {
  const months = Object.keys(byMonth || {}).sort();
  const names = [...new Set(months.flatMap(m => Object.keys(byMonth[m])))];
  if (months.length < 3 || names.length < 2) return '';
  const totals = names.map(n => months.reduce((s, m) => s + (byMonth[m][n] || 0), 0));
  const order = names.map((n, i) => [n, totals[i]]).sort((a, b) => b[1] - a[1]).map(([n]) => n);
  const W = 300, H = 140, x = i => (i / (months.length - 1) * W).toFixed(1);
  const col = months.map(m => order.reduce((s, n) => s + (byMonth[m][n] || 0), 0));
  const scale = (H - 10) / Math.max(1, ...col);
  const layers = [], labels = [], placed = [];
  const base = col.map(t => H / 2 - t * scale / 2);
  const CHAR_W = 4.2;                            // the 8px label's width per character
  order.forEach((n, k) => {
    const top = base.slice(), bot = base.map((b, i) => b + (byMonth[months[i]][n] || 0) * scale);
    const up = top.map((y, i) => `${x(i)},${y.toFixed(1)}`).join(' L');
    const down = bot.map((y, i) => `${x(i)},${y.toFixed(1)}`).reverse().join(' L');
    layers.push(`<path d="M${up} L${down}Z" class="wus-rv s${Math.min(k, 4)}"><title>${_s(n)}</title></path>`);
    const widest = bot.map((y, i) => y - top[i]).reduce((b, w, i, a) => (w > a[b] ? i : b), 0);
    if (bot[widest] - top[widest] >= 12) {
      // inside the box, and never over a label already placed
      const w = n.length * CHAR_W, y = (top[widest] + bot[widest]) / 2;
      const left = Math.min(Math.max(+x(widest) - w / 2, 2), W - 2 - w);
      if (!placed.some(p => Math.abs(p.y - y) < 10 && left < p.right && left + w > p.left)) {
        placed.push({left, right: left + w, y});
        labels.push(`<text x="${left.toFixed(1)}" y="${y.toFixed(1)}" class="wus-rv-lbl" dominant-baseline="middle">${_s(n)}</text>`);
      }
    }
    bot.forEach((y, i) => { base[i] = y; });
  });
  const ends = `<text x="0" y="${H + 12}" class="wus-rd-lbl">${_s(months[0])}</text><text x="${W}" y="${H + 12}" class="wus-rd-lbl" text-anchor="end">${_s(months[months.length - 1])}</text>`;
  return `<svg class="wus-chart wus-river" width="460" viewBox="0 0 ${W} ${H + 16}" role="img" aria-label="${th('story.river_aria', {list: order.join(', ')})}">${layers.join('')}${labels.join('')}${ends}</svg>`;
}

/** You and the crowd: one title and its two places among the same titles -
 *  yours by hours, the community's by score (services/wrapup_sections
 *  .consensus "duel"). Replaced a scatter of every scored title, which hid
 *  the one the headline named among thirty anonymous dots (2026-11-08).
 *  The side the slide is about carries the amber number. */
export function duel(d, posters) {
  if (!d || !d.title) return null;
  const yours = d.kind === 'yours';
  const poster = (posters || {})[d.title];
  const side = (lead, label, rank, sub) => `<div class="wus-duel-side${lead ? ' lead' : ''}">
      <span class="wus-duel-lbl">${label}</span><span class="wus-duel-n">#${rank}</span><span class="wus-duel-sub">${sub}</span></div>`;
  return {
    eyebrow: th(yours ? 'story.duel_yours' : 'story.duel_crowd'),
    share: t(yours ? 'story.share_mine' : 'story.share_theirs', {title: d.title}),
    html: `${poster ? _posterImg(poster, 120, d.media_type === 'music' ? 120 : 180, d.media_type === 'music') : ''}
      <h2 class="wus-h">${_s(d.title)}</h2>
      <div class="wus-duel">${side(yours, th('story.for_you'), d.your_rank, _s(_hours(d.hours)))}${side(!yours, th('story.community'), d.crowd_rank, th('story.of_ten', {x: _fmt(d.community)}))}</div>`,
    note: t('story.duel_note', {n: d.of}),
  };
}
// ── the slides ──────────────────────────────────────────────────────────────
function _posters(f, refs, story) {
  // no artwork, no frame: an empty "?" tile says nothing on a story slide
  const items = (refs || []).map(r => story?.refs?.[r]).filter(Boolean)
    .map(it => ({...it, url: it.poster || (f.posters || {})[it.title]})).filter(it => it.url).slice(0, 3);
  if (!items.length) return '';
  return `<div class="wus-posters">${items.map(it => _posterImg(it.url, 96,
    it.media_type === 'music' ? 96 : 144, it.media_type === 'music')).join('')}</div>`;
}

function build(d) {
  const f = d.facts || {}, s = d.story || null, tp = f.tempo || {}, out = [];
  const label = periodLabel(f.period), period = _s(label);
  if (tp.hours) {
    const days = tp.hours / 24;
    out.push({cls: 'open', share: `${_hours(tp.hours)} — ${label}`,
      html: `<div class="wus-eyebrow">${period}</div><div class="wus-big"><span class="wus-n" data-count="${Math.round(tp.hours)}">${Math.round(tp.hours).toLocaleString(loc())}</span><span class="wus-unit">${th('story.hours_unit', {n: Math.round(tp.hours)})}</span></div>
        <p class="wus-text">${days >= 1 ? `${th('story.full_days', {n: days, x: _fmt(days)})} ` : ''}${th('story.plays_on_days', {plays: tp.plays ?? '', days: tp.active_days ?? ''})}</p>`});
  }
  const top = f.top || [];
  if (top.length >= 3) {
    const opts = top.slice(0, 4).map((x, i) => ({x, i})).sort((a, b) => (a.x.title > b.x.title ? 1 : -1));
    out.push({cls: 'quiz', hold: true,
      html: `<div class="wus-eyebrow">${th('story.quiz_eyebrow')}</div><h2 class="wus-h">${th('story.quiz_head')}</h2>
        <div class="wus-quiz">${opts.map(o => `<button type="button" class="wus-opt" ${act('answerWrapupStory', o.i)}>${_s(o.x.title)}</button>`).join('')}</div>
        <p class="wus-text wus-quiz-out" aria-live="polite"></p>`});
  }
  if (top.length) {
    const x = top[0];
    out.push({cls: 'reveal', share: `${x.title}: ${_hours(x.hours)}`,
      html: `<div class="wus-eyebrow">${th('story.number_one')}</div>${(f.posters || {})[x.title] ? _posterImg((f.posters || {})[x.title], 140, x.media_type === 'music' ? 140 : 210, x.media_type === 'music') : ''}
        <h2 class="wus-h">${_s(x.title)}</h2><div class="wus-big"><span class="wus-n" data-count="${Math.round(x.hours)}">${Math.round(x.hours)}</span><span class="wus-unit">${th('story.hours_unit', {n: Math.round(x.hours)})}</span></div>`});
  }
  if (tp.by_day_h && Object.keys(tp.by_day_h).length >= 5) {
    const bd = tp.biggest_day || {};
    out.push({cls: 'rhythm', share: t('story.rhythm_share', {n: tp.active_days, run: tp.longest_streak_days}),
      html: `<div class="wus-eyebrow">${th('story.rhythm')}</div><h2 class="wus-h">${th('story.days_on', {n: tp.active_days})}</h2>
        ${heatmap(tp.by_day_h, f.period.start, f.period.end)}
        <p class="wus-text">${th('story.longest_run', {n: tp.longest_streak_days})}${bd.date ? ` ${th('story.biggest_day', {date: bd.date, hours: _hours(bd.hours)})}` : ''}</p>`});
  }
  const clockSvg = clock(tp.clock_h);
  if (clockSvg) {
    const moods = f.music?.moods?.by_daypart || {};
    const moodLine = ['morning', 'afternoon', 'evening', 'night'].filter(p => moods[p])
      .map(p => `${th(`story.daypart.${p}`)}: ${_s(moods[p].mood)}`).join(' · ');
    const day = _weekday(((tp.peak_weekday ?? 1) + 6) % 7, 'long');
    const head = t('story.clock_head', {h: tp.peak_hour ?? '', day});
    out.push({cls: 'clock', share: head,
      html: `<div class="wus-eyebrow">${th('story.clock')}</div><h2 class="wus-h">${_s(head)}</h2>
        ${clockSvg}${moodLine ? `<p class="wus-text">${th('story.moods')} ${moodLine}.</p>` : ''}`});
  }
  // A slide's share line: the curator's headline, or - told from the cards
  // alone - the claim. Never one that names what the household named itself
  // (a device: ``private``), and never a story's body text, which can.
  const written = !!s?.slides?.length;
  const slides = written ? s.slides
    : (d.insights || []).slice(0, 5).map(c => ({id: c.id, beat: c.slide, headline: '', text: c.claim, item_refs: [],
      private: !!c.private}));
  for (const sl of slides) {
    let visual = '';
    if (sl.beat === 'court') visual = court(f.court);
    else if (sl.beat === 'decades') visual = decades(f.eras);
    else if (sl.beat === 'balance' && f.velocity) visual = balance(f.velocity);
    out.push({cls: `beat beat-${esc(sl.beat || '')}`, share: sl.private ? '' : (sl.headline || (written ? '' : sl.text)),
      html: `${sl.headline ? `<h2 class="wus-h">${_s(sl.headline)}</h2>` : ''}${_posters(f, sl.item_refs, s)}${visual}
        <p class="wus-text">${_s(sl.text)}</p>`});
  }
  const ng = f.nanogenres || {};
  const riverSvg = river(ng.by_month);
  if (riverSvg) {
    const lead = (ng.top || [])[0];
    out.push({cls: 'river', share: lead ? `${lead.name}: ${_hours(lead.hours)}` : t('story.river'),
      html: `<div class="wus-eyebrow">${th('story.river')}</div>${lead ? `<h2 class="wus-h">${th('story.led', {name: lead.name})}</h2>` : ''}
        ${riverSvg}${(ng.new_to_you || []).length ? `<p class="wus-text">${th('story.new_to_you', {list: ng.new_to_you.join(', ')})}</p>` : ''}`});
  }
  const du = duel(f.consensus?.duel, f.posters);
  if (du) {
    const more = (f.consensus.yours_alone || []).filter(x => x !== f.consensus.duel.title);
    const also = f.consensus.duel.kind === 'yours' && more.length ? ` ${th('story.also_yours', {list: more.join(', ')})}` : '';
    out.push({cls: 'consensus', share: du.share,
      html: `<div class="wus-eyebrow">${du.eyebrow}</div>${du.html}<p class="wus-text wus-duel-note">${_s(du.note)}${also}</p>`});
  }
  const rt = s?.roast_and_toast;
  if (rt?.roast && rt?.toast) {
    out.push({cls: 'pair', share: rt.toast,
      html: `<div class="wus-roast"><div class="wus-eyebrow">${th('story.roast')}</div><p class="wus-text">${_s(rt.roast)}</p></div>
        <div class="wus-toast"><div class="wus-eyebrow">${th('story.toast')}</div><p class="wus-text">${_s(rt.toast)}</p></div>`});
  }
  const title = s?.archetype_title;
  if (title) {
    out.push({cls: 'identity', share: `${title}${s.share_line && s.share_line !== title ? ` — ${s.share_line}` : ''}`,
      html: `<div class="wus-eyebrow">${period} · ${th('story.who_you_were')}</div>${radar(f, d.household?.radar_median)}
        <h2 class="wus-arch">${_s(title)}</h2>${s.archetype_subhead ? `<p class="wus-text">${_s(s.archetype_subhead)}</p>` : ''}`});
  }
  // The anime season: started, finished, dropped (wrapup_facts._anime_season).
  const as = f.anime_season;
  if (as && (as.started?.length || as.finished?.length)) {
    const started = t('story.season_started', {n: as.started?.length || 0}), finished = t('story.season_finished', {n: as.finished?.length || 0});
    out.push({cls: 'anime-season', share: `${label}: ${started}, ${finished}`,
      html: `<div class="wus-eyebrow">${period}</div>
        <h2 class="wus-h">${_s(started)} · ${_s(finished)}${as.dropped?.length ? ` · ${th('story.season_dropped', {n: as.dropped.length})}` : ''}</h2>
        ${as.finished?.length ? `<p class="wus-text">${th('story.season_finished_list', {list: as.finished.slice(0, 4).map(x => x.title).join(', ')})}</p>` : ''}
        ${as.dropped?.length ? `<p class="wus-text">${th('story.season_left_list', {list: as.dropped.slice(0, 3).map(x => x.title).join(', ')})}</p>` : ''}`});
  }
  // Milestones reached in this period (services/milestones), one slide.
  const ms = d.milestones?.period || [];
  if (ms.length) {
    const lead = ms[ms.length - 1];
    const cap = x => String(x || '').replace(/^./, c => c.toUpperCase());
    out.push({cls: 'milestone', share: `${cap(lead.phrase)}${lead.title ? `: ${lead.title}` : ''}`,
      html: `<div class="wus-eyebrow">${th('story.milestones', {n: ms.length})}</div>
        <h2 class="wus-h">${_s(cap(lead.phrase))}</h2>${lead.title ? `<p class="wus-text">${_s(lead.title)}</p>` : ''}
        ${ms.length > 1 ? `<ul class="wus-ms">${ms.slice(0, -1).reverse().slice(0, 4).map(x => `<li>${_s(x.phrase)}${x.title ? ` · ${_s(x.title)}` : ''}</li>`).join('')}</ul>` : ''}`});
  }
  const hook = s?.look_ahead_hook;
  const pick = hook?.item_ref && s?.refs?.[hook.item_ref];
  out.push({cls: 'end', hold: true,
    html: `${pick && (pick.poster || (f.posters || {})[pick.title]) ? _posterImg(pick.poster || (f.posters || {})[pick.title], 96, 144, pick.media_type === 'music') : ''}
      ${hook?.challenge ? `<div class="wus-eyebrow">${th('story.next_up')}</div><p class="wus-text">${_s(hook.challenge)}</p>` : ''}
      ${s?.share_line ? `<p class="wus-share">${th('story.quoted', {text: s.share_line})}</p>` : ''}
      <div class="wus-actions"><button type="button" class="btn btn-primary" ${act('shareWrapup')}>${th('story.share')}</button>
        <button type="button" class="btn btn-secondary" ${act('closeWrapupStory')}>${th('story.explore')}</button></div>`});
  return out;
}

// ── playing it ──────────────────────────────────────────────────────────────
function _show() {
  const el = st.el;
  if (!el) return;
  clearTimeout(st.timer);
  const sl = st.slides[st.i];
  el.querySelector('.wus-bars').innerHTML = st.slides.map((_, k) =>
    `<i class="wus-seg${k < st.i ? ' is-done' : k === st.i ? ' is-on' : ''}"></i>`).join('');
  const stage = el.querySelector('.wus-stage');
  stage.className = `wus-stage wus-s-${sl.cls}${REDUCED.matches ? '' : ' is-in'}`;
  stage.innerHTML = sl.html;
  el.querySelector('.wus-share-btn')?.toggleAttribute('hidden', !sl.share);
  stage.querySelectorAll('.wus-n[data-count]').forEach(n => { n.textContent = '0'; countTo(n, +n.dataset.count, 900); });
  if (st.answered != null && sl.cls === 'quiz') _quizResult();
  if (!sl.hold && !st.paused && !REDUCED.matches) st.timer = setTimeout(nextWrapupSlide, dwellMs(sl.html));
}

function _key(e) {
  if (!st.el) return;
  if (e.key === 'Escape') closeWrapupStory();
  else if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); nextWrapupSlide(); }
  else if (e.key === 'ArrowLeft') prevWrapupSlide();
}

function _hold(on) {
  st.paused = on;
  if (on) clearTimeout(st.timer);
  else if (st.el && !st.slides[st.i]?.hold && !REDUCED.matches) st.timer = setTimeout(nextWrapupSlide, dwellMs(st.slides[st.i].html));
}

// ── a slide as an image (UX design record, stage 3.6) ──────────────────────
// Every slide carries a ``share`` line (build() above); "Share this slide"
// draws it on a 1080x1920 canvas IN THE BROWSER - the period, the line, the
// wordmark - and hands the PNG to the system share sheet, or saves it. Nothing
// is uploaded, and the picture carries no server name and no user name: only
// the slide's own line and the period's label go on it.
const SHARE_W = 1080, SHARE_H = 1920;

/** The share line laid out to fit the picture: wrapped by MEASURED width
 *  (``measure(text, px)`` - the canvas's measureText), the font shrunk
 *  until every word fits on a line and every line fits the height, a word
 *  wider than the narrowest line broken with a hyphen rather than cut.
 *  Nothing is dropped. Pure. */
export function shareLayout(share, measure, {maxWidth = SHARE_W - 192, maxHeight = 1240,
  sizes = [92, 80, 70, 62, 56, 48], lead = 1.26} = {}) {
  const words = String(share || '').split(/\s+/).filter(Boolean);
  const wrap = px => {
    const lines = [];
    let cur = '';
    for (const w of words) {
      const next = cur ? `${cur} ${w}` : w;
      if (!cur || measure(next, px) <= maxWidth) cur = next;
      else { lines.push(cur); cur = w; }
    }
    if (cur) lines.push(cur);
    return lines;
  };
  for (const px of sizes) {
    const lines = wrap(px);
    if (lines.every(l => measure(l, px) <= maxWidth) && lines.length * px * lead <= maxHeight) return {size: px, lines};
  }
  // the smallest size, a too-long word broken where it must
  const px = sizes[sizes.length - 1];
  const out = [];
  for (const line of wrap(px)) {
    let rest = line;
    while (measure(rest, px) > maxWidth && rest.length > 1) {
      let n = rest.length - 1;
      while (n > 1 && measure(rest.slice(0, n) + '-', px) > maxWidth) n--;
      out.push(rest.slice(0, n) + '-');
      rest = rest.slice(n);
    }
    out.push(rest);
  }
  return {size: px, lines: out};
}

/** The file name of a slide's image. Pure. */
export function shareFileName(label, i) {
  const slug = String(label || 'wrap-up').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return `curatarr-${slug || 'wrap-up'}-${i + 1}.png`;
}

function _token(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function _slideImage(sl, label) {
  const c = document.createElement('canvas');
  c.width = SHARE_W; c.height = SHARE_H;
  const g = c.getContext('2d');
  const bg = _token('--bg', '#1a1d23'), text = _token('--text', '#c8cdd8'), amber = _token('--amber', '#e5a00d');
  g.fillStyle = bg;
  g.fillRect(0, 0, SHARE_W, SHARE_H);
  const glow = g.createRadialGradient(SHARE_W * 0.7, SHARE_H * 0.25, 40, SHARE_W * 0.7, SHARE_H * 0.25, SHARE_W);
  glow.addColorStop(0, amber + '33');
  glow.addColorStop(1, 'transparent');
  g.fillStyle = glow;
  g.fillRect(0, 0, SHARE_W, SHARE_H);
  const face = px => `700 ${px}px system-ui, sans-serif`;
  const measure = (t, px) => { g.font = face(px); return g.measureText(t).width; };
  const {size, lines} = shareLayout(sl.share, measure);
  g.textBaseline = 'top';
  g.fillStyle = amber;
  g.font = '600 44px system-ui, sans-serif';
  g.fillText(String(label || '').toUpperCase(), 96, 300);
  g.fillStyle = text;
  g.font = face(size);
  lines.forEach((l, k) => g.fillText(l, 96, 420 + k * Math.round(size * 1.26)));
  g.fillStyle = amber;
  g.font = '700 52px system-ui, sans-serif';
  g.fillText('Curatarr', 96, SHARE_H - 200);
  return new Promise(res => c.toBlob(res, 'image/png'));
}

export async function shareWrapupSlide() {
  const sl = st.slides[st.i];
  if (!sl?.share) return;
  const label = periodLabel(st.data?.facts?.period);
  _hold(true);
  try {
    const blob = await _slideImage(sl, label);
    if (!blob) return;
    const file = new File([blob], shareFileName(label, st.i), {type: 'image/png'});
    if (navigator.canShare?.({files: [file]})) {
      await navigator.share({files: [file], text: sl.share});
    } else {
      const a = Object.assign(document.createElement('a'), {href: URL.createObjectURL(blob), download: file.name});
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    }
  } catch (e) {
    if (e?.name !== 'AbortError') console.warn('[wrapup] slide image not shared', e);
  } finally {
    _hold(false);
  }
}

/** Open the story for this wrap-up's data (the page's GET /api/wrapup answer). */
export function playStory(data) {
  if (!data?.facts || data.facts.empty) return;
  closeWrapupStory();
  st.data = data;
  st.slides = build(data);
  st.i = 0;
  st.answered = null;
  const el = document.createElement('div');
  el.className = 'wus';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'true');
  el.setAttribute('aria-label', t('story.dialog'));
  el.tabIndex = -1;
  el.innerHTML = `<div class="wus-bars" aria-hidden="true"></div>
    <button type="button" class="wus-close" aria-label="${th('common.close')}" ${act('closeWrapupStory')}>×</button>
    <button type="button" class="wus-share-btn" aria-label="${th('story.share_slide')}" title="${th('story.share_slide')}" ${act('shareWrapupSlide')}>${th('story.share')}</button>
    <div class="wus-stage" aria-live="polite"></div>
    <button type="button" class="wus-zone wus-prev" aria-label="${th('story.prev')}" ${act('prevWrapupSlide', 'tap')}></button>
    <button type="button" class="wus-zone wus-next" aria-label="${th('common.next')}" ${act('nextWrapupSlide', 'tap')}></button>`;
  // Holding anywhere but a real button pauses; a tap on the sides moves on,
  // unless it was the end of a hold (reading, not skipping).
  el.addEventListener('pointerdown', ev => {
    if (ev.target.closest('button:not(.wus-zone)')) return;
    st.downAt = Date.now(); _hold(true);
  });
  el.addEventListener('pointerup', () => _hold(false));
  // The watermark: a screenshot of a slide says where it came from.
  el.appendChild(brandMark('wus-mark'));
  document.body.appendChild(el);
  document.addEventListener('keydown', _key);
  st.el = el;
  haptic('medium');
  _show();
  el.focus();          // the dialog, not a tap zone: no focus ring on open
}

export function closeWrapupStory() {
  clearTimeout(st.timer);
  document.removeEventListener('keydown', _key);
  st.el?.remove();
  st.el = null;
}
export function nextWrapupSlide(fromTap) {
  if (!st.el) return;
  if (fromTap === 'tap' && _wasHold()) { st.downAt = 0; return; }
  st.downAt = 0;
  if (st.i >= st.slides.length - 1) { clearTimeout(st.timer); return; }
  st.i += 1; haptic('tick'); _show();
}
export function prevWrapupSlide(fromTap) {
  if (!st.el) return;
  if (fromTap === 'tap' && _wasHold()) { st.downAt = 0; return; }
  st.downAt = 0;
  if (st.i === 0) return;
  st.i -= 1; haptic('tick'); _show();
}

function _quizResult() {
  const top = st.data?.facts?.top || [];
  const out = st.el?.querySelector('.wus-quiz-out');
  if (!out || !top.length) return;
  const right = st.answered === 0;
  out.textContent = t(right ? 'story.quiz_right' : 'story.quiz_wrong', {title: top[0].title});
  st.el.querySelectorAll('.wus-opt').forEach(b => b.disabled = true);
}
export function answerWrapupStory(i) {
  st.answered = +i;
  haptic(st.answered === 0 ? 'medium' : 'light');
  _quizResult();
  clearTimeout(st.timer);
  st.timer = setTimeout(nextWrapupSlide, 1800);
}
