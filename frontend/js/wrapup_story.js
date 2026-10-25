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
    const t = new Date(start.getTime() + i * 86400000), iso = t.toISOString().slice(0, 10);
    const k = i + offset, col = Math.floor(k / 7), row = k % 7, h = byDay[iso] || 0;
    cells.push(`<rect x="${col * 13}" y="${row * 13}" width="11" height="11" rx="2" class="wus-hm l${lvl(h)}"><title>${_s(iso)}${h ? ` · ${_fmt(h)} h` : ''}</title></rect>`);
  }
  const cols = Math.floor((span + offset) / 7) + 1;
  // drawn at most 22px a column: a month is a small block, a year fills the width
  return `<svg class="wus-chart wus-heat" width="${Math.min(560, cols * 22)}" viewBox="0 0 ${cols * 13} ${7 * 13}" role="img" aria-label="${days.length} active days">${cells.join('')}</svg>`;
}

/** Six axes of taste, 0..1 each; at least four must be known. */
export function radarAxes(f) {
  const t = f.tempo || {}, top = (f.top || [])[0];
  const ax = [
    ['Acclaim', f.highbrow_comfort?.highbrow],
    ['Novelty', f.discovery?.new_share],
    ['Commitment', f.completion?.completion_rate],
    ['Catalogue', f.eras?.split?.catalogue],
    ['Contrarian', f.contrarian ? Math.min(1, (f.contrarian.gap || 0) / 3) : (f.highbrow_comfort ? 0 : undefined)],
    ['Focus', top && t.hours ? Math.min(1, top.hours / t.hours * 2) : undefined],
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
    ? `<polygon points="${ax.map(([name], i) => pt(i, Math.max(0.04, median[name])).map(x => x.toFixed(1)).join(',')).join(' ')}" class="wus-rd-median"><title>Household median</title></polygon>`
    : '';
  const labels = ax.map(([name, v], i) => {
    const [x, y] = pt(i, 1.22);
    return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" class="wus-rd-lbl" text-anchor="middle">${_s(name)} ${_pct(v)}%</text>`;
  }).join('');
  return `<svg class="wus-chart wus-radar" width="300" viewBox="0 0 220 220" role="img" aria-label="Taste: ${ax.map(([n, v]) => `${n} ${_pct(v)}%`).join(', ')}">
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
    return `<rect x="${x}" y="${(mid - hi).toFixed(1)}" width="${bw}" height="${hi.toFixed(1)}" class="wus-bal-in"><title>${_s(k)}: ${_fmt(m.in)} GB in</title></rect>
      <rect x="${x}" y="${mid}" width="${bw}" height="${ho.toFixed(1)}" class="wus-bal-out"><title>${_s(k)}: ${_fmt(m.out)} GB out</title></rect>`;
  }).join('');
  return `<svg class="wus-chart wus-bal" width="460" viewBox="0 0 300 120" role="img" aria-label="${_fmt(v.gb_in)} GB in, ${_fmt(v.gb_out)} GB out">
    <line x1="0" x2="300" y1="${mid}" y2="${mid}" class="wus-axis"/>${bars}</svg>`;
}

/** The court: debates won and lost as two bars, and the boldest keep. */
export function court(c) {
  if (!c) return '';
  const tot = Math.max(1, (c.won || 0) + (c.lost || 0));
  const bar = (n, cls, label) => `<div class="wus-ct-row"><span>${_s(label)}</span>
    <svg viewBox="0 0 100 8" class="wus-ct-bar" aria-hidden="true"><rect width="100" height="8" rx="4" class="wus-ct-bg"/>
    <rect width="${(100 * n / tot).toFixed(1)}" height="8" rx="4" class="${cls}"/></svg><b>${_s(n)}</b></div>`;
  return `<div class="wus-court">${bar(c.won || 0, 'wus-ct-you', 'You talked it out of')}${bar(c.lost || 0, 'wus-ct-cur', 'It talked you into')}
    <div class="wus-ct-meta">${_s(c.decided)} decided · ${_s(c.kept)} kept · ${_s(c.deleted)} deleted · ${_s(c.overrides)} over its objection</div></div>`;
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
  const f = d.facts || {}, s = d.story || null, t = f.tempo || {}, out = [];
  const period = _s(f.period?.label || '');
  if (t.hours) {
    const days = t.hours / 24;
    out.push({cls: 'open', share: `${_fmt(t.hours)} hours — ${f.period?.label || ''}`,
      html: `<div class="wus-eyebrow">${period}</div><div class="wus-big"><span class="wus-n" data-count="${Math.round(t.hours)}">${Math.round(t.hours).toLocaleString(loc())}</span><span class="wus-unit">hours</span></div>
        <p class="wus-text">${days >= 1 ? `That is ${_fmt(days)} full days. ` : ''}${_s(t.plays)} plays on ${_s(t.active_days)} days.</p>`});
  }
  const top = f.top || [];
  if (top.length >= 3) {
    const opts = top.slice(0, 4).map((x, i) => ({x, i})).sort((a, b) => (a.x.title > b.x.title ? 1 : -1));
    out.push({cls: 'quiz', hold: true,
      html: `<div class="wus-eyebrow">Before the reveal</div><h2 class="wus-h">Which one got the most of your time?</h2>
        <div class="wus-quiz">${opts.map(o => `<button type="button" class="wus-opt" ${act('answerWrapupStory', o.i)}>${_s(o.x.title)}</button>`).join('')}</div>
        <p class="wus-text wus-quiz-out" aria-live="polite"></p>`});
  }
  if (top.length) {
    const x = top[0];
    out.push({cls: 'reveal', share: `${x.title}: ${_fmt(x.hours)} hours`,
      html: `<div class="wus-eyebrow">Your number one</div>${(f.posters || {})[x.title] ? _posterImg((f.posters || {})[x.title], 140, x.media_type === 'music' ? 140 : 210, x.media_type === 'music') : ''}
        <h2 class="wus-h">${_s(x.title)}</h2><div class="wus-big"><span class="wus-n" data-count="${Math.round(x.hours)}">${Math.round(x.hours)}</span><span class="wus-unit">hours</span></div>`});
  }
  if (t.by_day_h && Object.keys(t.by_day_h).length >= 5) {
    const bd = t.biggest_day || {};
    out.push({cls: 'rhythm', share: `${t.active_days} active days, longest run ${t.longest_streak_days}`,
      html: `<div class="wus-eyebrow">The rhythm</div><h2 class="wus-h">${_s(t.active_days)} days with something on</h2>
        ${heatmap(t.by_day_h, f.period.start, f.period.end)}
        <p class="wus-text">Longest run: ${_s(t.longest_streak_days)} days in a row.${bd.date ? ` Biggest day: ${_s(bd.date)}, ${_fmt(bd.hours)} hours.` : ''}</p>`});
  }
  const slides = s?.slides?.length ? s.slides
    : (d.insights || []).slice(0, 5).map(c => ({id: c.id, beat: c.slide, headline: '', text: c.claim, item_refs: []}));
  for (const sl of slides) {
    let visual = '';
    if (sl.beat === 'court') visual = court(f.court);
    else if (sl.beat === 'balance' && f.velocity) visual = balance(f.velocity);
    out.push({cls: `beat beat-${esc(sl.beat || '')}`, share: sl.headline || sl.text,
      html: `${sl.headline ? `<h2 class="wus-h">${_s(sl.headline)}</h2>` : ''}${_posters(f, sl.item_refs, s)}${visual}
        <p class="wus-text">${_s(sl.text)}</p>`});
  }
  const rt = s?.roast_and_toast;
  if (rt?.roast && rt?.toast) {
    out.push({cls: 'pair', share: rt.toast,
      html: `<div class="wus-roast"><div class="wus-eyebrow">Roast</div><p class="wus-text">${_s(rt.roast)}</p></div>
        <div class="wus-toast"><div class="wus-eyebrow">Toast</div><p class="wus-text">${_s(rt.toast)}</p></div>`});
  }
  const title = s?.archetype_title;
  if (title) {
    out.push({cls: 'identity', share: `${title}${s.share_line && s.share_line !== title ? ` — ${s.share_line}` : ''}`,
      html: `<div class="wus-eyebrow">${period} · who you were</div>${radar(f, d.household?.radar_median)}
        <h2 class="wus-arch">${_s(title)}</h2>${s.archetype_subhead ? `<p class="wus-text">${_s(s.archetype_subhead)}</p>` : ''}`});
  }
  const hook = s?.look_ahead_hook;
  const pick = hook?.item_ref && s?.refs?.[hook.item_ref];
  out.push({cls: 'end', hold: true,
    html: `${pick && (pick.poster || (f.posters || {})[pick.title]) ? _posterImg(pick.poster || (f.posters || {})[pick.title], 96, 144, pick.media_type === 'music') : ''}
      ${hook?.challenge ? `<div class="wus-eyebrow">Next up</div><p class="wus-text">${_s(hook.challenge)}</p>` : ''}
      ${s?.share_line ? `<p class="wus-share">“${_s(s.share_line)}”</p>` : ''}
      <div class="wus-actions"><button type="button" class="btn btn-primary" ${act('shareWrapup')}>Share</button>
        <button type="button" class="btn btn-secondary" ${act('closeWrapupStory')}>Explore the details</button></div>`});
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
  el.setAttribute('aria-label', 'Your wrap-up');
  el.tabIndex = -1;
  el.innerHTML = `<div class="wus-bars" aria-hidden="true"></div>
    <button type="button" class="wus-close" aria-label="Close" ${act('closeWrapupStory')}>×</button>
    <div class="wus-stage" aria-live="polite"></div>
    <button type="button" class="wus-zone wus-prev" aria-label="Previous" ${act('prevWrapupSlide', 'tap')}></button>
    <button type="button" class="wus-zone wus-next" aria-label="Next" ${act('nextWrapupSlide', 'tap')}></button>`;
  // Holding anywhere but a real button pauses; a tap on the sides moves on,
  // unless it was the end of a hold (reading, not skipping).
  el.addEventListener('pointerdown', ev => {
    if (ev.target.closest('button:not(.wus-zone)')) return;
    st.downAt = Date.now(); _hold(true);
  });
  el.addEventListener('pointerup', () => _hold(false));
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
  out.textContent = right ? `Right — ${top[0].title}.` : `Not quite: it was ${top[0].title}.`;
  st.el.querySelectorAll('.wus-opt').forEach(b => b.disabled = true);
}
export function answerWrapupStory(i) {
  st.answered = +i;
  haptic(st.answered === 0 ? 'medium' : 'light');
  _quizResult();
  clearTimeout(st.timer);
  st.timer = setTimeout(nextWrapupSlide, 1800);
}
