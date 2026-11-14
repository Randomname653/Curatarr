// ── Wrap-Up: one person's period in review (replaced the Curation Report) ──
// Facts arrive instantly (computed server-side, cached); the curator's story
// is written in a background task the ticker follows. Tiles render only when
// the server sent their section — an empty tile is a lie in a recap. Every
// number on the page comes from the facts; the story only adds words.
import { api } from './api.js';
import { serverLabel, state } from './state.js';
import { _busyStreaming, enterThread, sendMessage } from './chat.js';
import { _repBar as _bar, renderLedger } from './report.js';
import { skeletonHtml, EL, _errHtml, _errMsg, _posterImg, act, actOn, btnBusy, btnDone, countTo, esc, escAttr, haptic, loc, toast } from './ui.js';
import { periodLabel, playStory, seasonLabel } from './wrapup_story.js';
import { t, th } from './i18n.js';
import { brandMarkHtml } from './brand.js';

// A day as the reader writes it: the peak line printed the raw ISO date
// ("2026-03-14") in the showpiece view (UI audit 2026-11-14).
function _day(iso) {
  if (!iso) return '';
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(loc(), {day: 'numeric', month: 'long', year: 'numeric'});
}

const WU_KEY = 'curatarr_wrapup';
const SEEN_KEY = 'curatarr_wrapup_seen';
// The words are in the dictionaries (wrapup.*); these are the keys.
const PERIODS = ['week', 'month', '30d', '6m', 'year', 'all'];
const SCOPES = ['all', 'movie', 'show', 'anime', 'music'];
const PHASES = 6;                       // wrapup.phase.0 … 5, the ticker's steps
const TRAITS = ['night_owl', 'matinee', 'completionist', 'sampler', 'scout', 'loyalist', 'archivist', 'day_one',
  'binger', 'highbrow', 'comfort_seeker', 'contrarian', 'purger', 'defender'];
const _scopeLabel = s => (SCOPES.includes(s) ? t(`wrapup.scope.${s}`) : s || '');
const _traitLabel = k => (TRAITS.includes(k) ? t(`wrapup.trait.${k}`) : k);
const _traitTitle = k => (TRAITS.includes(k) ? t(`wrapup.trait_title.${k}`) : '');
// A word the facts carry as an identifier (an outcome, a trend), in the UI
// language where the dictionary has it, else as it came.
const _word = (prefix, v) => { const k = `${prefix}.${v}`, w = t(k); return w === k ? String(v ?? '') : w; };
// Sunday = 0, as the facts count (1 Jan 2023 was a Sunday)
const _weekdays = d => new Date(Date.UTC(2023, 0, 1 + d)).toLocaleDateString(loc(), {weekday: 'long', timeZone: 'UTC'});

const wu = {scope: 'all', period: '30d', year: null, data: null, poll: null, snapshot: false,
            layout: 'card', quizOpen: false, tiles: []};
try { Object.assign(wu, JSON.parse(localStorage.getItem(WU_KEY) || '{}')); } catch (_) { /* private mode: defaults */ }
function _save() {
  try { localStorage.setItem(WU_KEY, JSON.stringify({scope: wu.scope, period: wu.period, year: wu.year, season: wu.season})); } catch (_) { /* private mode */ }
}
// The ISO week / the month of a wall-clock date - the browser's clock is the
// member's (it is what reports their zone); the server resolves the window
// on the stored zone (services/wrapup_facts.resolve_period).
function _isoWeekKey(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y = t.getUTCFullYear();
  const w = Math.ceil(((t - Date.UTC(y, 0, 1)) / 86400000 + 1) / 7);
  return `week:${y}-W${String(w).padStart(2, '0')}`;
}
const _monthKey = d => `month:${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
const _periodKey = () => wu.period === 'year' ? `year:${wu.year || new Date().getFullYear()}`
  : wu.period === 'season' ? (wu.season || '30d')
  : wu.period === 'week' ? (wu.weekKey || _isoWeekKey(new Date()))
  : wu.period === 'month' ? (wu.monthKey || _monthKey(new Date()))
  : wu.period;
const _seasonLabel = k => seasonLabel(k);
const _pct = v => Math.round(100 * (v || 0));
const _s = v => esc(String(v ?? ''));
const _isMusic = mt => mt === 'music';
const _poster = (f, title) => (f.posters || {})[title] || null;

export async function loadWrapup() {
  const el = document.getElementById('wrapup-content');
  if (!el) return;
  if (!wu.data) el.innerHTML = skeletonHtml(t('wrapup.loading'), {kind: 'tiles'});
  try {
    wu.data = await api(`/api/wrapup?scope=${encodeURIComponent(wu.scope)}&period=${encodeURIComponent(_periodKey())}`);
    if (!(wu.data.scopes || []).includes(wu.scope) && wu.scope !== 'all') {
      wu.scope = (wu.data.scopes || ['all'])[0];
      _save();
      return loadWrapup();
    }
    // A curator-written (v2) story opens as the story the first time it is
    // seen; its own prediction replaces the hero quiz.
    const story = _storyDue();
    wu.quizOpen = !story && _quizDue();
    _render();
    if (story) { _markSeen(); playStory(wu.data); }
    if (wu.data.generating) _follow(wu.data.generating);
  } catch (e) {
    el.innerHTML = _errHtml(e, act('loadWrapup'));
  }
}

export function setWrapupPeriod(p) {
  if (p === wu.period && !wu.weekKey && !wu.monthKey) return;
  wu.period = p; wu.weekKey = null; wu.monthKey = null;   // the chips mean "this" week / month
  _save(); haptic('tick'); loadWrapup();
}
// A card names a concrete week or month ("Your week", "Your month is ready"):
// open the Wrap-Up on exactly that one.
export function openWrapupPeriod(key) {
  const k = String(key || '');
  if (k.startsWith('week:')) { wu.period = 'week'; wu.weekKey = k; wu.monthKey = null; }
  else if (k.startsWith('month:')) { wu.period = 'month'; wu.monthKey = k; wu.weekKey = null; }
  else if (k.startsWith('year:')) { wu.period = 'year'; wu.year = Number(k.slice(5)) || null; }
  else return;
  wu.data = null;
  location.hash = '#wrapup';
  loadWrapup();
}
// The anime season (services/wrapup_facts: Winter, Spring, Summer, Fall):
// a wrap-up of the member's anime in that season.
export function setWrapupSeason(key) {
  if (wu.period === 'season' && key === wu.season) return;
  wu.period = 'season'; wu.season = key;
  if ((wu.data?.scopes || []).includes('anime')) wu.scope = 'anime';
  _save(); haptic('tick'); loadWrapup();
}
export function setWrapupYear(y) {
  if (wu.period === 'year' && +y === +wu.year) return;
  wu.period = 'year'; wu.year = +y; _save(); haptic('tick'); loadWrapup();
}
export function setWrapupScope(s) {
  if (s === wu.scope) return;
  wu.scope = s; _save(); haptic('tick'); loadWrapup();
}
export function toggleWrapupSnapshot() { wu.snapshot = !wu.snapshot; haptic('light'); _render(); }
export function setWrapupLayout(l) { wu.layout = l === 'lineup' ? 'lineup' : 'card'; haptic('tick'); _render(); }

function _render(reveal = false) {
  const el = document.getElementById('wrapup-content');
  if (!el || !wu.data) return;
  const d = wu.data, f = d.facts || {};
  wu.tiles = [];
  if (f.empty || !f.eligible) {
    const plays = f.tempo?.plays || 0;
    el.innerHTML = _controls(d) + `<div class="wu-empty card"><h2 class="wu-empty-title">${th('wrapup.empty_title')}</h2>
      <p>${plays ? th('wrapup.plays_so_far', {n: plays}) : th('wrapup.nothing_played')}
      ${th('wrapup.empty_hint')}</p></div>`;
    return;
  }
  el.innerHTML = wu.snapshot
    ? _controls(d) + _snapshot(f, d.story)
    : _controls(d) + _hero(f, d) + `<h2 class="wu-explore">${th('wrapup.explore')}</h2>` + _bento(f) + _story(f, d) + _household(d) + _milestones(d) + _ledgerSlot(f);
  if (reveal) el.querySelector('.wu-hero')?.classList.add('is-reveal');
  _armCounters(el);
  if (f.curatarr?.ledger && !wu.snapshot) {
    const slot = document.getElementById('wu-ledger');
    if (slot) renderLedger(slot);
  }
}

// ── controls ────────────────────────────────────────────────────────────────
function _controls(d) {
  const seasons = d.seasons || [];
  const per = PERIODS.map(k => `<button type="button" class="wu-chip${k === wu.period ? ' is-on' : ''}" aria-pressed="${k === wu.period}" ${act('setWrapupPeriod', k)}>${th(`wrapup.chip.${k}`)}</button>`).join('')
    + (seasons.length ? `<button type="button" class="wu-chip${wu.period === 'season' ? ' is-on' : ''}" aria-pressed="${wu.period === 'season'}" ${act('setWrapupSeason', wu.season && seasons.includes(wu.season) ? wu.season : seasons[0])}>${th('wrapup.anime_season')}</button>` : '');
  const seasonChips = wu.period === 'season' && seasons.length > 1
    ? `<div class="wu-chips" role="group" aria-label="${th('wrapup.anime_season')}">${seasons.map(k => {
        const on = k === wu.season;
        return `<button type="button" class="wu-chip wu-chip-sm${on ? ' is-on' : ''}" aria-pressed="${on}" ${act('setWrapupSeason', k)}>${esc(_seasonLabel(k))}</button>`;
      }).join('')}</div>` : '';
  const years = wu.period === 'year' && (d.years || []).length > 1
    ? `<div class="wu-chips" role="group" aria-label="${th('wrapup.year')}">${d.years.map(y => {
        const on = +y === +(wu.year || new Date().getFullYear());
        return `<button type="button" class="wu-chip wu-chip-sm${on ? ' is-on' : ''}" aria-pressed="${on}" ${act('setWrapupYear', y)}>${_s(y)}</button>`;
      }).join('')}</div>` : '';
  const scopes = (d.scopes || ['all']).map(s => `<button type="button" class="wu-tab${s === wu.scope ? ' is-on' : ''}" aria-pressed="${s === wu.scope}" ${act('setWrapupScope', s)}>${esc(_scopeLabel(s))}</button>`).join('');
  return `<div class="wu-controls">
    <div class="wu-chips" role="group" aria-label="${th('wrapup.period_group')}">${per}</div>${years}${seasonChips}
    <div class="wu-tabs" role="group" aria-label="${th('wrapup.media_group')}">${scopes}</div>
    <button type="button" class="btn btn-secondary btn-sm wu-snap-btn" ${act('toggleWrapupSnapshot')}>${th(wu.snapshot ? 'wrapup.full_view' : 'wrapup.snapshot')}</button></div>`;
}

// ── hero persona ────────────────────────────────────────────────────────────
function _archetype(f, s) {
  return s?.archetype_title || _traitTitle(f.traits?.[0]?.key) || t('wrapup.in_review');
}

function _hero(f, d) {
  const s = d.story, top = (f.top || [])[0];
  const art = top ? `<div class="wu-hero-art">${_posterImg(_poster(f, top.title), 200, _isMusic(top.media_type) ? 200 : 300, _isMusic(top.media_type))}</div>` : '';
  if (wu.quizOpen) return `<section class="card wu-hero" aria-labelledby="wu-quiz-q">${art}<div class="wu-hero-text">${_quiz(f)}</div></section>`;
  const mainTrait = s?.archetype_trait || f.traits?.[0]?.key;
  const badges = (f.traits || []).filter(x => x.key !== mainTrait).slice(0, 3)
    .map(x => `<span class="wu-badge" title="${esc(x.evidence)}">${esc(_traitLabel(x.key))}</span>`).join('');
  const sub = s?.archetype_subhead || (f.traits?.[0]?.evidence ? `${f.traits[0].evidence}.` : '');
  const gen = d.generating ? _ticker(0) : _genButton(d);
  return `<section class="card wu-hero" aria-labelledby="wu-arch">${art}
    <div class="wu-hero-text">
      <div class="wu-eyebrow">${esc(periodLabel(f.period))} · ${esc(SCOPES.includes(f.scope) ? _scopeLabel(f.scope) : '')}</div>
      <h2 id="wu-arch" class="wu-arch">${esc(_archetype(f, s))}</h2>
      ${sub ? `<p class="wu-sub">${esc(sub)}</p>` : ''}
      ${badges ? `<div class="wu-badges" aria-label="${th('wrapup.also_earned')}">${badges}</div>` : ''}
      ${s?.share_line && s.share_line !== s.archetype_title ? `<p class="wu-share-line">${th('story.quoted', {text: s.share_line})}</p>` : ''}
      <div class="wu-hero-actions" id="wu-gen">${gen}</div>
    </div></section>`;
}

function _genButton(d) {
  const s = d.story;
  const label = th(!s ? 'wrapup.write' : d.story_stale ? 'wrapup.rewrite_moved' : 'wrapup.rewrite');
  const ask = s ? `<button type="button" class="btn btn-secondary btn-sm" ${act('askWrapup')}>${th('wrapup.ask')}</button>` : '';
  const pl = `<button type="button" class="btn btn-ghost btn-sm" ${act('pushWrapupPlaylist', EL)}>${th('wrapup.make_playlist', {server: serverLabel()})}</button>`;
  const play = `<button type="button" class="btn btn-primary btn-sm" ${act('playWrapupStory')}>${th('wrapup.play')}</button>`;
  return `${play}<button type="button" class="btn ${s ? 'btn-secondary' : 'btn-primary'} btn-sm" ${act('generateWrapup', EL)}>${label}</button>${ask}${pl}`;
}

// ── the quiz before the reveal (Deezer, Spotify) ────────────────────────────
function _seenKey() { return `${wu.scope}|${_periodKey()}|${wu.data?.story_at || ''}`; }
function _quizDue() {
  const d = wu.data, top = d?.facts?.top || [];
  if (!d?.story || top.length < 4) return false;
  try { return !(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')).includes(_seenKey()); } catch (_) { return false; }
}
function _storyDue() {
  if (wu.data?.story?.version !== 2) return false;
  try { return !(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]')).includes(_seenKey()); } catch (_) { return false; }
}
// The paced story (wrapup_story.js); the page below is its Explore view.
export function playWrapupStory() {
  if (!wu.data) return;
  _markSeen();
  playStory(wu.data);
}
function _markSeen() {
  try {
    const seen = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]').filter(k => k !== _seenKey());
    seen.push(_seenKey());
    localStorage.setItem(SEEN_KEY, JSON.stringify(seen.slice(-40)));
  } catch (_) { /* private mode: the quiz shows again next time */ }
}
function _quiz(f) {
  const opts = (f.top || []).slice(0, 4).map((x, i) => ({...x, i})).sort((a, b) => a.title.localeCompare(b.title));
  return `<div class="wu-eyebrow">${th('story.quiz_eyebrow')}</div>
    <h2 id="wu-quiz-q" class="wu-quiz-q">${th('story.quiz_head')}</h2>
    <div class="wu-quiz">${opts.map(o => `<button type="button" class="wu-quiz-opt" ${act('answerWrapupQuiz', o.i)}>
      ${_posterImg(_poster(f, o.title), 72, _isMusic(o.media_type) ? 72 : 108, _isMusic(o.media_type))}<span>${esc(o.title)}</span></button>`).join('')}</div>
    <button type="button" class="btn btn-ghost btn-sm" ${act('answerWrapupQuiz', -1)}>${th('wrapup.just_show')}</button>`;
}
export function answerWrapupQuiz(i) {
  const top = wu.data?.facts?.top || [];
  if (i >= 0) {
    if (i === 0) { haptic('medium'); toast(t('wrapup.quiz_right', {title: top[0].title}), 'success'); }
    else { haptic('light'); toast(t('wrapup.quiz_was', {title: top[0].title}), 'info'); }
  }
  wu.quizOpen = false;
  _markSeen();
  _render(true);
}

// ── bento ───────────────────────────────────────────────────────────────────
function _tile(cls, label, body, share) {
  const i = wu.tiles.push(share || label) - 1;
  return `<article class="wu-tile ${cls}"><div class="wu-tile-head"><div class="wu-tile-lbl">${esc(label)}</div>
    <button type="button" class="wu-tile-share" aria-label="${th('wrapup.share_tile', {label})}" ${act('shareWrapupTile', i)}>
      <svg aria-hidden="true" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg></button></div>${body}</article>`;
}
const _num = (n, unit = '') => `<span class="wu-num" data-count="${Math.round(n || 0)}">${Math.round(n || 0).toLocaleString(loc())}</span>${unit ? `<span class="wu-unit">${esc(unit)}</span>` : ''}`;

function _posterTile(f, x, extra) {
  const m = _isMusic(x.media_type);
  return `<div class="wu-poster-tile"><div class="glow-interactive${m ? ' is-music' : ''}">${_posterImg(_poster(f, x.title) || x.poster, 84, m ? 84 : 126, m)}</div>
    <div class="wu-poster-body"><div class="wu-tile-title">${esc(x.title)}</div>${extra}</div></div>`;
}

function _calendar(dates, f) {
  if (!dates?.length) return '';
  const start = new Date(f.period.start), end = new Date(f.period.end);
  const span = Math.round((end - start) / 86400000);
  if (span > 200 || span < 7) return '';
  const set = new Set(dates);
  const cells = [];
  for (let i = 0; i <= span; i++) {
    const d = new Date(start.getTime() + i * 86400000).toISOString().slice(0, 10);
    cells.push(`<i class="wu-cal-day${set.has(d) ? ' is-on' : ''}" title="${esc(d)}"></i>`);
  }
  return `<div class="wu-cal" role="img" aria-label="${th('wrapup.cal_aria', {n: dates.length, title: f.top[0].title})}">${cells.join('')}</div>`;
}

function _bento(f) {
  const tp = f.tempo || {}, tiles = [];
  if (tp.hours != null) {
    const max = Math.max(1, ...(tp.by_hour_h || [0]));
    const clock = (tp.by_hour_h || []).map((h, i) => `<span class="wu-hr${i === tp.peak_hour ? ' is-peak' : ''}" title="${i}:00 · ${_s(h)} h">${_bar('wu-hr-fill', 100 * h / max)}</span>`).join('');
    const delta = tp.prev_hours ? Math.round(100 * (tp.hours - tp.prev_hours) / tp.prev_hours) : null;
    const approx = tp.hours_estimated_share > 0.25 ? '≈' : '';
    const day = tp.peak_weekday != null ? _weekdays(tp.peak_weekday) : '';
    tiles.push(_tile('wu-span-hours', t('wrapup.tile.hours'),
      `<div class="wu-big">${approx}${_num(tp.hours, 'h')}</div>
       <div class="wu-meta">${_num(tp.active_days)} ${th('wrapup.active_days', {n: tp.active_days || 0})} · ${th('wrapup.best_streak', {n: tp.longest_streak_days || 0})}${delta != null ? ` · <b class="${delta >= 0 ? 't-amber' : ''}">${delta >= 0 ? '+' : ''}${delta}%</b> ${th('wrapup.vs_before')}` : ''}</div>
       <div class="wu-clock" role="img" aria-label="${th('wrapup.clock_aria', {h: tp.peak_hour ?? ''})}">${clock}</div>
       <div class="wu-clock-axis" aria-hidden="true"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
       <div class="wu-meta">${th('wrapup.peak_line', {h: tp.peak_hour ?? '', day, date: _day(tp.biggest_day?.date), hours: tp.biggest_day?.hours != null ? Number(tp.biggest_day.hours).toLocaleString(loc()) : ''})}</div>`,
      `${approx}${t('wrapup.hours_share', {n: tp.active_days || 0, hours: tp.hours, days: tp.active_days ?? '', h: tp.peak_hour ?? ''})}`));
  }
  const hc = f.highbrow_comfort;
  if (hc) tiles.push(_tile('wu-span-split', t('wrapup.tile.split'),
    `<div class="wu-split">${_bar('wu-split-hb', _pct(hc.highbrow))}${_bar('wu-split-cf', _pct(hc.comfort))}<i class="wu-split-ot"></i></div>
     <div class="wu-split-legend"><span><b>${_pct(hc.highbrow)}%</b> ${th('wrapup.acclaimed')}${hc.top_highbrow ? ` · ${esc(hc.top_highbrow)}` : ''}</span>
     <span><b>${_pct(hc.comfort)}%</b> ${th('wrapup.comfort')}${hc.top_comfort ? ` · ${esc(hc.top_comfort)}` : ''}</span><span>${_pct(hc.other)}% ${th('wrapup.other')}</span></div>`,
    t('wrapup.split_share', {hb: _pct(hc.highbrow), cf: _pct(hc.comfort)})));
  const c = f.contrarian;
  if (c?.title) tiles.push(_tile('wu-span-poster', t('wrapup.tile.contrarian'), _posterTile(f, c,
    `<div class="wu-vs"><span><b>${_s(c.you)}</b>${th(c.basis === 'rewatched' ? 'wrapup.you_rewatched' : 'wrapup.you')}</span><span><b>${_s(c.community)}</b>${th('wrapup.everyone')}</span></div>`),
    t('wrapup.contrarian_share', {title: c.title, you: c.you, community: c.community})));
  const dm = f.dismissal;
  if (dm?.title) tiles.push(_tile('wu-span-poster', t('wrapup.tile.dismissal'), _posterTile(f, dm,
    `<div class="wu-meta">${esc(_word('wrapup.how', dm.how))} · ${th('wrapup.everyone_else', {score: dm.community ?? ''})}</div>`),
    t('wrapup.dismissal_share', {title: dm.title, score: dm.community ?? ''})));
  const b = f.binge?.in_48h || f.binge?.deepest_sitting;
  if (b) tiles.push(_tile('wu-span-sm', t(f.binge?.in_48h ? 'wrapup.tile.binge' : 'wrapup.tile.sitting'),
    `<div class="wu-big">${_num(b.episodes, t('wrapup.eps'))}</div><div class="wu-meta">${esc(b.series)}</div>`,
    t(f.binge?.in_48h ? 'wrapup.binge_share' : 'wrapup.sitting_share', {n: b.episodes || 0, series: b.series})));
  const e = f.eras;
  if (e) tiles.push(_tile('wu-span-sm', t('wrapup.tile.age'),
    `<div class="wu-big wu-year">${_s(e.viewing_age_year)}</div>
     <div class="wu-meta">${esc(e.oldest.title)} (${_s(e.oldest.year)}) → ${esc(e.newest.title)} (${_s(e.newest.year)})</div>
     <div class="wu-decades">${Object.entries(e.decades).map(([k, v]) => `<span title="${esc(k)} · ${_pct(v)}%">${_bar('wu-dec-fill', _pct(v))}<em>${esc(k.slice(2))}</em></span>`).join('')}</div>`,
    t('wrapup.age_share', {year: e.viewing_age_year})));
  const as = f.anime_season;
  if (as && (as.started?.length || as.finished?.length || as.dropped?.length)) {
    const list = (xs, fmt) => xs.length ? `<ul class="wu-as-list">${xs.slice(0, 6).map(x => `<li>${fmt(x)}</li>`).join('')}</ul>` : `<p class="wu-meta">${th('wrapup.none')}</p>`;
    const ep = x => th('wrapup.ep', {n: x.episodes || 0, x: x.of ? t('wrapup.ep_of', {n: x.episodes ?? '', of: x.of}) : (x.episodes ?? '')});
    tiles.push(_tile('wu-span-split', t('wrapup.tile.season'),
      `<div class="wu-as">
        <div><div class="wu-as-h">${th('wrapup.as_started')} <b>${_s(as.started.length)}</b></div>${list(as.started, x => `${esc(x.title)} <span class="t3">· ${ep({...x, of: null})}</span>`)}</div>
        <div><div class="wu-as-h">${th('wrapup.as_finished')} <b>${_s(as.finished.length)}</b></div>${list(as.finished, x => `${esc(x.title)} <span class="t3">· ${x.series_done ? th('wrapup.whole_series') : th('wrapup.season_n', {n: x.season ?? ''})}</span>`)}</div>
        <div><div class="wu-as-h">${th('wrapup.as_dropped')} <b>${_s(as.dropped.length)}</b></div>${list(as.dropped, x => `${esc(x.title)} <span class="t3">· ${ep(x)}</span>`)}</div>
      </div>`,
      t('wrapup.season_share', {started: as.started.length, finished: as.finished.length, dropped: as.dropped.length})));
  }
  if (e?.decade_top && Object.keys(e.decades || {}).length >= 2) {
    const prev = wu.data?.decades_prev || null;
    const rows = Object.entries(e.decades).map(([k, v]) => {
      const was = prev ? Math.round(100 * (prev[k] || 0)) : null, now = _pct(v);
      const d = was == null ? '' : now - was;
      return `<div class="wu-dec-row"><span class="wu-dec-k">${esc(k)}</span>${_bar('wu-dec-line', now)}
        <span class="wu-dec-v"><b>${now}%</b>${d !== '' && d !== 0 ? ` <em class="${d > 0 ? 't-amber' : ''}">${d > 0 ? '+' : ''}${d}</em>` : ''}</span>
        <span class="wu-dec-t">${esc((e.decade_top[k] || []).slice(0, 2).join(', '))}</span></div>`;
    }).join('');
    tiles.push(_tile('wu-span-split', t('wrapup.tile.decades'),
      `<div class="wu-dec-list">${rows}</div>${prev ? `<div class="wu-meta">${th('wrapup.vs_last_year')}</div>` : ''}`,
      t('wrapup.decades_share', {list: Object.entries(e.decades).map(([k, v]) => `${k} ${_pct(v)}%`).join(', ')})));
  }
  if (e?.shortest) tiles.push(_tile('wu-span-sm', t('wrapup.tile.lengths'),
    `<div class="wu-pairline"><b>${_s(e.shortest.minutes)}</b> min <span>${esc(e.shortest.title)}</span></div>
     <div class="wu-pairline"><b>${_s(e.longest.minutes)}</b> min <span>${esc(e.longest.title)}</span></div>`,
    t('wrapup.lengths_share', {short: e.shortest.title, smin: e.shortest.minutes, long: e.longest.title, lmin: e.longest.minutes})));
  const ds = f.discovery;
  if (ds?.new_share != null) tiles.push(_tile('wu-span-sm', _traitLabel(ds.new_share >= 0.5 ? 'scout' : 'loyalist'),
    `<div class="wu-big">${_num(_pct(ds.new_share), '%')}</div><div class="wu-meta">${th('wrapup.new_time')}${ds.best_new ? ` · ${th('wrapup.best_find', {title: ds.best_new})}` : ''}</div>`,
    t('wrapup.new_share', {pct: _pct(ds.new_share)})));
  const cb = (f.comebacks || [])[0];
  if (cb) tiles.push(_tile('wu-span-sm', t('wrapup.tile.comeback'),
    `<div class="wu-tile-title">${esc(cb.title)}</div><div class="wu-meta">${th('wrapup.back_after', {n: Math.round(cb.gap_days / 30)})}</div>`,
    t('wrapup.comeback_share', {title: cb.title, n: Math.round(cb.gap_days / 30)})));
  if (tp.title_streak || (tp.top_calendar || []).length > 2) {
    const ts = tp.title_streak, n = (tp.top_calendar || []).length;
    tiles.push(_tile('wu-span-cal', t('wrapup.tile.number_one'),
      `<div class="wu-meta"><b>${esc(f.top[0].title)}</b> ${th('wrapup.on_days', {n})}${ts ? ` · ${th('wrapup.title_run', {n: ts.days || 0, title: ts.title})}` : ''}</div>${_calendar(tp.top_calendar, f)}`,
      `${f.top[0].title} ${t('wrapup.on_days', {n})}`));
  }
  const m = f.music;
  if (m?.top_artists?.length) tiles.push(_tile('wu-span-poster', t('wrapup.tile.stereo'),
    `<ol class="wu-list">${m.top_artists.slice(0, 5).map(a => `<li><span>${esc(a.artist)}</span><b>${_s(a.hours)} h</b></li>`).join('')}</ol>
     ${m.on_repeat ? `<div class="wu-meta">${th('wrapup.on_repeat', {track: m.on_repeat.track, n: m.on_repeat.plays ?? '', date: m.on_repeat.date ?? ''})}</div>` : ''}`,
    t('wrapup.top_artist', {name: m.top_artists[0].artist})));
  const g = f.genres;
  if (g?.first_time?.length || g?.rising?.length) tiles.push(_tile('wu-span-sm', t('wrapup.tile.genres'),
    `${(g.rising || []).map(x => `<div class="wu-pairline"><b class="t-amber">+${_s(x.pp)}</b> ${th('wrapup.pts')} <span>${esc(x.genre)}</span></div>`).join('')}
     ${(g.fading || []).slice(0, 1).map(x => `<div class="wu-pairline"><b>${_s(x.pp)}</b> ${th('wrapup.pts')} <span>${esc(x.genre)}</span></div>`).join('')}
     ${g.first_time?.length ? `<div class="wu-meta">${th('wrapup.genres_new', {n: g.new_count ?? '', list: g.first_time.slice(0, 3).join(', ')})}</div>` : ''}`,
    g.rising?.length ? t('wrapup.genre_rising', {genre: g.rising[0].genre}) : t('wrapup.genres_new_share', {n: g.new_count || 0})));
  const cu = f.curatarr || {};
  if (cu.graveyard) tiles.push(_tile('wu-span-purge', t('wrapup.tile.graveyard'),
    `<div class="wu-big">${_num(cu.graveyard.gb_freed, 'GB')}</div>
     <div class="wu-meta">${th('wrapup.graveyard_meta', {n: cu.graveyard.deleted ?? '', title: cu.graveyard.biggest.title, gb: cu.graveyard.biggest.gb ?? ''})}</div>
     ${cu.came_back ? `<div class="wu-meta">${th('wrapup.came_back')} <b>${esc(cu.came_back.title)}</b></div>` : ''}`,
    t('wrapup.graveyard_share', {gb: cu.graveyard.gb_freed, n: cu.graveyard.deleted || 0})));
  if (cu.shield) tiles.push(_tile('wu-span-sm', t('wrapup.tile.shield'),
    `<div class="wu-big">${_num(cu.shield.overrides)}</div><div class="wu-meta">${th('wrapup.kept_over')}${cu.shield.top_reason ? ` · ${th('wrapup.mostly', {reason: cu.shield.top_reason})}` : ''}${cu.shield.manual_protections ? ` · ${th('wrapup.by_hand', {n: cu.shield.manual_protections})}` : ''}</div>`,
    t('wrapup.shield_share', {n: cu.shield.overrides || 0})));
  if (cu.most_debated) tiles.push(_tile('wu-span-sm', t('wrapup.tile.debated'),
    `<div class="wu-tile-title">${esc(cu.most_debated.title)}</div><div class="wu-meta">${th('wrapup.debated_meta', {n: cu.most_debated.messages || 0, outcome: _word('wrapup.outcome', cu.most_debated.outcome)})}</div>`,
    t('wrapup.debated_share', {title: cu.most_debated.title})));
  const dr = f.drift;
  if (dr?.clusters?.length) {
    const names = Object.fromEntries((wu.data.story?.cluster_names || []).map(n => [n.ref, n.name]));
    const drift = _word('wrapup.drift', dr.label);
    tiles.push(_tile('wu-span-clusters', `${t('wrapup.tile.clusters')} · ${drift}`, dr.clusters.map(k =>
      `<div class="wu-cluster"><span class="wu-cl-name" title="${esc((k.top_titles || []).join(', '))}">${esc(names[k.id] || t('wrapup.around', {title: k.top_titles?.[0] || ''}))}</span>
       <span class="wu-cl-bar">${_bar('wu-cl-fill', _pct(k.share))}</span>
       <span class="wu-cl-trend is-${esc(k.trend)}">${k.trend === 'rising' ? '↑' : k.trend === 'fading' ? '↓' : '→'} ${_pct(k.share)}%</span></div>`).join(''),
      t('wrapup.clusters_share', {drift})));
  }
  if (cu.dust_collector) tiles.push(_tile('wu-span-sm', t('wrapup.tile.dust'),
    `<div class="wu-big">${_num(cu.dust_collector.gb, 'GB')}</div><div class="wu-meta">${th('wrapup.dust_meta', {title: cu.dust_collector.title})}</div>`,
    t('wrapup.dust_share', {title: cu.dust_collector.title, gb: cu.dust_collector.gb})));
  return `<div class="wu-bento">${tiles.join('')}</div>`;
}

// ── story: chapters, roast & toast, look-ahead ──────────────────────────────
function _story(f, d) {
  const s = d.story;
  if (!s?.era_chapters?.length) return '';
  const refs = s.refs || {};
  const fan = it => `<div class="wu-fan-item">${_posterImg(it.poster || _poster(f, it.title), 84, _isMusic(it.media_type) ? 84 : 126, _isMusic(it.media_type))}</div>`;
  const own = s.era_chapters.map(c => (c.item_refs || []).map(r => refs[r]).filter(Boolean).slice(0, 4));
  // A chapter about a number (hours, a streak, a day) names no title: it
  // shows the next of the period's top titles no other chapter shows, so
  // no card keeps an empty picture column; with none left, the column goes.
  const shown = new Set(own.flat().map(it => it.title));
  const spare = (f.top || []).filter(x => !shown.has(x.title) && _poster(f, x.title));
  const ch = s.era_chapters.map((c, i) => {
    const items = own[i].length ? own[i] : spare.splice(0, 1);
    const posters = items.map(fan).join('');
    return `<article class="card wu-chapter${posters ? '' : ' wu-chapter-bare'}">
      ${posters ? `<div class="wu-fan" aria-hidden="true">${posters}</div>` : ''}
      <div class="wu-ch-body"><div class="wu-ch-n">${th('wrapup.chapter', {n: i + 1})}</div>
        <h3 class="wu-ch-title">${esc(c.title)}</h3>
        ${c.kicker ? `<div class="wu-ch-kicker">${esc(c.kicker)}</div>` : ''}
        ${c.commentary ? `<p class="wu-ch-text">${esc(c.commentary)}</p>` : ''}
        <button type="button" class="btn btn-ghost btn-sm" ${act('discussWrapupChapter', i)}>${th('wrapup.discuss_era')}</button></div></article>`;
  }).join('');
  const rt = s.roast_and_toast;
  const pair = rt ? `<div class="wu-pair"><article class="wu-roast"><div class="wu-tile-lbl">${th('story.roast')}</div><p>${esc(rt.roast)}</p></article>
    <article class="wu-toast"><div class="wu-tile-lbl">${th('story.toast')}</div><p>${esc(rt.toast)}</p></article></div>` : '';
  const la = s.look_ahead_hook, pick = la?.item_ref && refs[la.item_ref];
  const hook = la ? `<article class="card wu-hook">${pick ? _posterImg(pick.poster || _poster(f, pick.title), 72, 108, _isMusic(pick.media_type)) : ''}
    <div><div class="wu-tile-lbl">${th('story.next_up')}</div><p>${esc(la.challenge)}</p>
    ${pick ? `<button type="button" class="btn btn-secondary btn-sm" ${act('discussWrapupHook')}>${th('wrapup.talk_me_into')}</button>` : ''}</div></article>` : '';
  const note = d.story_source === 'fallback'
    ? `<p class="wu-note">${th('wrapup.note_fallback')}</p>`
    : d.story_stale ? `<p class="wu-note">${th('wrapup.note_stale')}</p>`
    : _langChanged(d) ? `<p class="wu-note">${th('wrapup.note_language', {was: _langName(d.story_lang), now: _langName(d.user_lang)})}</p>` : '';
  const quotes = `<p class="wu-note">${th(d.quotes ? 'wrapup.quotes_on' : 'wrapup.quotes_off')}
    <button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupQuotes')}>${th(d.quotes ? 'wrapup.quotes_stop' : 'wrapup.quotes_allow')}</button></p>`;
  return `<section class="wu-chapters" aria-label="${th('wrapup.chapters')}">${ch}</section>${pair}${hook}${note}${quotes}`;
}

// The story keeps the language it was written in; after a change of the
// setting (Settings → Account) the next rewrite speaks the new one.
const _langChanged = d => !!(d.story_lang && d.user_lang && d.story_lang !== d.user_lang);
const _langName = code => { const k = `language.${code}`, n = t(k); return n === k ? String(code) : n; };

// The household is opt-in: only members who turned it on are compared, and
// only by rank and median — never another member's name or titles.
function _household(d) {
  const h = d.household || {};
  if (!h.consented) {
    return `<p class="wu-note">${th('wrapup.hh_offer')}
      <button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupHousehold')}>${th('wrapup.hh_compare')}</button></p>`;
  }
  const off = `<button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupHousehold')}>${th('wrapup.hh_stop')}</button>`;
  if (!h.rank_hours) {
    return `<p class="wu-note">${th('wrapup.hh_waiting')} ${off}</p>`;
  }
  const only = (h.only_you ? ` · ${th('wrapup.hh_only_you_pre')} <b>${esc(h.only_you)}</b>` : '')
    + ((h.decades_owned || []).length ? ` · ${th('wrapup.hh_decades', {list: h.decades_owned.join(t('wrapup.hh_and'))})}` : '');
  return `<article class="card wu-household"><div class="wu-tile-lbl">${th('wrapup.hh_title')}</div>
    <p><b>#${_s(h.rank_hours)}</b> ${th('wrapup.hh_rank', {n: h.members ?? ''})}${only}${h.radar_median ? ` · ${th('wrapup.hh_median')}` : ''}</p>
    <p class="wu-note">${th('wrapup.hh_privacy')} ${off}</p></article>`;
}

// ── milestones (services/milestones): the whole history, newest first ─────
const _cap = s => String(s || '').replace(/^./, c => c.toUpperCase());
const _MS_KINDS = ['films', 'series', 'anime', 'episodes', 'hours', 'artists', 'years'];
const _msKind = k => (_MS_KINDS.includes(k) ? t(`wrapup.ms_kind.${k}`) : k);

function _msDate(iso) {
  return new Date(iso).toLocaleDateString(loc(), { day: 'numeric', month: 'short', year: 'numeric' });
}

function _milestones(d) {
  const m = d.milestones || {}, hist = m.history || [], off = new Set(m.off || []);
  const rows = hist.slice(0, 40).map(x => `<li class="wu-ms-row"><span class="wu-ms-date">${esc(_msDate(x.reached_at))}</span>
      <span class="wu-ms-what">${esc(_cap(x.phrase))}${x.title ? ` · <b>${esc(x.title)}</b>` : ''}</span></li>`).join('');
  const kinds = (m.kinds || []).map(k => `<label class="wu-ms-kind"><input type="checkbox" ${off.has(k) ? '' : 'checked'}
      ${actOn('change', 'toggleMilestoneKind', k, EL)}> ${esc(_msKind(k))}</label>`).join('');
  return `<article class="card wu-milestones"><div class="wu-tile-lbl">${th('wrapup.ms_title')}</div>
    ${rows ? `<ol class="wu-ms-list">${rows}</ol>` : `<p class="wu-note">${th('wrapup.ms_none')}</p>`}
    <details class="wu-ms-kinds"><summary class="fs-12 t3">${th('wrapup.ms_kinds')}</summary><div class="wu-ms-kinds-row">${kinds}</div></details></article>`;
}

export async function toggleMilestoneKind(kind, el) {
  try {
    await api('/api/wrapup/milestones/kinds', 'POST', { kind, on: !!el?.checked });
    await loadWrapup();
  } catch (e) { toast(t('wrapup.not_saved', {error: e.message || e}), 'error'); }
}

export async function toggleWrapupHousehold() {
  if (!wu.data) return;
  try {
    const on = !wu.data.household?.consented;
    await api('/api/wrapup/prefs', 'POST', {household: on});
    haptic('tick');
    toast(t(on ? 'wrapup.hh_on_toast' : 'wrapup.hh_off_toast'), 'success');
    await loadWrapup();
  } catch (e) {
    toast(_errMsg(e).slice(0, 200), 'danger');
  }
}

// The user's own words are theirs to withhold (opt-out): the next rewrite
// leaves them out.
export async function toggleWrapupQuotes() {
  if (!wu.data) return;
  try {
    const r = await api('/api/wrapup/prefs', 'POST', {quotes: !wu.data.quotes});
    wu.data.quotes = r.quotes;
    haptic('tick');
    toast(t(r.quotes ? 'wrapup.quotes_on_toast' : 'wrapup.quotes_off_toast'), 'success');
    _render();
  } catch (e) {
    toast(_errMsg(e).slice(0, 200), 'danger');
  }
}

// ── "Ask your wrap-up": a chat thread whose context is the fact sheet ───────
async function _openThread(message) {
  if (_busyStreaming()) return;
  const ctx = {kind: 'wrapup', wrapup_scope: wu.scope, wrapup_period: _periodKey()};
  const title = wu.data?.story?.archetype_title || t('wrapup.your_wrapup');
  if (!await enterThread(ctx, t('wrapup.thread_banner', {title}), null)) return;
  if (message) {
    const input = document.getElementById('chat-input');
    if (input) input.value = message;
    sendMessage();
  } else {
    document.getElementById('chat-input')?.focus();
  }
}
export function askWrapup() { haptic('light'); _openThread(''); }
export function discussWrapupChapter(i) {
  const c = wu.data?.story?.era_chapters?.[i];
  if (!c) return;
  haptic('light');
  _openThread(t('wrapup.ask_chapter', {title: c.title}));
}
export function discussWrapupHook() {
  const s = wu.data?.story, pick = s?.refs?.[s?.look_ahead_hook?.item_ref];
  if (pick) { haptic('light'); _openThread(t('wrapup.ask_hook', {title: pick.title})); }
}

// ── Plex playlist (PlayStation's personal playlist) ────────────────────────
export async function pushWrapupPlaylist(btn) {
  btnBusy(btn, t('wrapup.pl_building'));
  try {
    const r = await api('/api/wrapup/playlist', 'POST', {scope: wu.scope, period: _periodKey()});
    haptic('medium');
    btnDone(btn, t('wrapup.pl_updated'));
    // The play card: the playlist one tap from playing, on the member's server.
    document.getElementById('wu-play-card')?.remove();
    btn.parentElement?.insertAdjacentHTML('afterend', playlistCardHtml(r, 'wu-play-card'));
  } catch (e) {
    toast(_errMsg(e).slice(0, 200), 'danger');
    btnDone(btn, t('wrapup.make_playlist', {server: serverLabel()}));
  }
}

// The answer of POST /api/wrapup/playlist as a card: what is on the server
// now, a button that plays it, what was not found. Shared with the landing's
// "Your week" / "Your month" cards (chat.js).
export function playlistCardHtml(r, id = '') {
  const pushed = r.pushed || [];
  return `<div class="card mt-14"${id ? ` id="${id}"` : ''} role="status">
    <p>${th('wrapup.pl_on', {server: serverLabel()})} ${pushed.map(p => `<b>${esc(p.title)}</b> (${esc(String(p.items))})`).join(th('wrapup.hh_and_plain'))}</p>
    ${r.missing?.length ? `<p class="fs-12 t3">${th('wrapup.pl_missing', {list: r.missing.slice(0, 3).join(', ')})}</p>` : ''}
    <div class="flex gap-8 flex-wrap mt-10">${pushed.filter(p => p.link).map(p =>
      `<a class="btn btn-primary btn-sm" href="${escAttr(p.link)}" target="_blank" rel="noopener">${th('wrapup.pl_play', {title: p.title})}</a>`).join('')}</div>
  </div>`;
}

// ── snapshot: the share card and the festival lineup (Amazon) ──────────────
function _snapshot(f, s) {
  const tp = f.tempo || {}, top = (f.top || []).slice(0, 5), label = periodLabel(f.period);
  const layouts = `<div class="wu-chips wu-snap-layouts" role="group" aria-label="${th('wrapup.layout')}">${['card', 'lineup'].map(k =>
    `<button type="button" class="wu-chip wu-chip-sm${wu.layout === k ? ' is-on' : ''}" aria-pressed="${wu.layout === k}" ${act('setWrapupLayout', k)}>${th(`wrapup.layout_${k}`)}</button>`).join('')}</div>`;
  const share = `<button type="button" class="btn btn-primary btn-sm" ${act('shareWrapup')}>${th('story.share')}</button>`;
  if (wu.layout === 'lineup') {
    const [head, ...rest] = top;
    return layouts + `<section class="card wu-snap wu-lineup" aria-label="${th('wrapup.lineup_aria')}">
      <div class="wu-eyebrow">${th('wrapup.presents', {period: label})}</div>
      <div class="wu-lu-head">${esc(head?.title || '')}</div>
      <div class="wu-lu-rest">${rest.slice(0, 2).map(x => `<span>${esc(x.title)}</span>`).join('')}</div>
      <div class="wu-lu-small">${rest.slice(2).map(x => `<span>${esc(x.title)}</span>`).join('')}${(f.comebacks || []).slice(0, 1).map(c => `<span>${th('wrapup.reunion', {title: c.title})}</span>`).join('')}</div>
      <div class="wu-lu-foot">${esc(_archetype(f, s))} · ${th('wrapup.lu_hours', {n: tp.hours || 0, x: tp.hours ?? ''})} · ${th('wrapup.lu_days', {n: tp.active_days || 0})}</div>
      ${share}</section>`;
  }
  const stats = [[tp.hours, t('wrapup.stat_hours', {n: tp.hours || 0})], [tp.active_days, t('wrapup.active_days', {n: tp.active_days || 0})],
    f.eras ? [f.eras.viewing_age_year, t('wrapup.stat_age')] : [f.curatarr?.graveyard?.gb_freed, t('wrapup.stat_freed')]]
    .filter(([v]) => v != null);
  return layouts + `<section class="card wu-snap" aria-label="${th('wrapup.summary_aria')}">
    <div class="wu-eyebrow">${brandMarkHtml('wu-mark')}Curatarr · ${esc(label)}</div>
    <h2 class="wu-arch">${esc(_archetype(f, s))}</h2>
    <p class="wu-sub">${esc(s?.share_line && s.share_line !== s.archetype_title ? s.share_line : (s?.archetype_subhead || ''))}</p>
    <div class="wu-snap-stats">${stats.map(([v, l]) => `<div><div class="wu-num-static">${_s(v)}</div><div class="wu-meta">${esc(l)}</div></div>`).join('')}</div>
    <div class="wu-snap-posters">${top.map(x => _posterImg(_poster(f, x.title), 64, _isMusic(x.media_type) ? 64 : 96, _isMusic(x.media_type))).join('')}</div>
    <ol class="wu-snap-top">${top.map(x => `<li>${esc(x.title)}</li>`).join('')}</ol>
    ${share}</section>`;
}

async function _shareText(text) {
  try {
    if (navigator.share) await navigator.share({title: t('wrapup.share_title'), text});
    else { await navigator.clipboard.writeText(text); toast(t('wrapup.copied'), 'success'); }
  } catch (e) {
    if (e?.name !== 'AbortError') toast(t('wrapup.share_failed'), 'danger');
  }
}
export function shareWrapup() {
  const d = wu.data, s = d?.story, f = d?.facts || {};
  const head = `${_archetype(f, s)} — ${periodLabel(f.period)}`;
  const line = s?.share_line && s.share_line !== s.archetype_title ? `\n${s.share_line}` : '';
  _shareText(`${head}${line}\n` + (f.top || []).slice(0, 5).map((x, i) => `${i + 1}. ${x.title}`).join('\n'));
}
// Strava's lesson: every scene can be shared on its own.
export function shareWrapupTile(i) {
  const text = wu.tiles[i];
  if (text) _shareText(t('wrapup.tile_share', {text, period: periodLabel(wu.data?.facts?.period) || t('wrapup.your_wrapup')}));
}

// ── generation + ticker ─────────────────────────────────────────────────────
export async function generateWrapup(btn) {
  btnBusy(btn, t('wrapup.starting'));
  try {
    const r = await api('/api/wrapup/generate', 'POST', {scope: wu.scope, period: _periodKey()});
    haptic('medium');
    _follow(r.task_id);
  } catch (e) {
    toast(t('wrapup.failed', {error: _errMsg(e).slice(0, 160)}), 'danger');
    btnDone(btn, t('wrapup.write'));
  }
}

function _ticker(phase) {
  return `<ol class="wu-ticker" role="status" aria-live="polite">${Array.from({length: PHASES}, (_, i) =>
    `<li class="${i < phase ? 'is-done' : i === phase ? 'is-now' : ''}">${th(`wrapup.phase.${i}`)}</li>`).join('')}</ol>`;
}

function _follow(taskId) {
  clearInterval(wu.poll);
  const slot = () => document.getElementById('wu-gen');
  const key = _periodKey(), scope = wu.scope;
  if (slot()) slot().innerHTML = _ticker(0);
  wu.poll = setInterval(async () => {
    // Left the view, or switched period/scope: stop asking (nav.js's own
    // test for the open view). The task keeps running server-side.
    if (!document.getElementById('wrapup-view')?.classList.contains('active') || key !== _periodKey() || scope !== wu.scope) {
      clearInterval(wu.poll);
      return;
    }
    try {
      const st = await api(`/api/wrapup/status/${encodeURIComponent(taskId)}`);
      if (st.status === 'running' || st.status === 'pending') {
        if (slot()) slot().innerHTML = _ticker(st.phase || 0);
        return;
      }
      clearInterval(wu.poll);
      if (st.status === 'error') toast(st.error || t('wrapup.could_not_finish'), 'danger');
      else if (st.status === 'skipped') toast(st.message || t('wrapup.not_enough'), 'info');
      wu.data = await api(`/api/wrapup?scope=${encodeURIComponent(scope)}&period=${encodeURIComponent(key)}`);
      wu.quizOpen = _quizDue();
      _render(!wu.quizOpen);
      if (st.status === 'done') haptic('medium');
    } catch (_) {
      clearInterval(wu.poll);
      loadWrapup();
    }
  }, 1500);
}

// ── counters: count up once, when a tile scrolls into view ──────────────────
function _armCounters(root) {
  const nodes = [...root.querySelectorAll('[data-count]')];
  if (!('IntersectionObserver' in window)) return;   // the final value is already printed
  for (const n of nodes) n.textContent = '0';
  const io = new IntersectionObserver(entries => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      countTo(en.target, +en.target.dataset.count, 900);
      io.unobserve(en.target);
    }
  }, {threshold: 0.4});
  nodes.forEach(n => io.observe(n));
}

function _ledgerSlot(f) {
  if (!f.curatarr?.ledger || !state.currentUser?.is_admin) return '';
  return `<details class="section wu-ledger"><summary class="section-head"><h3>${th('wrapup.ledger')}</h3>
    <span class="section-hint">${th('wrapup.ledger_hint')}</span></summary><div id="wu-ledger" class="section-body"></div></details>`;
}
