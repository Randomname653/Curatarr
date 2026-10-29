// ── Wrap-Up: one person's period in review (replaced the Curation Report) ──
// Facts arrive instantly (computed server-side, cached); the curator's story
// is written in a background task the ticker follows. Tiles render only when
// the server sent their section — an empty tile is a lie in a recap. Every
// number on the page comes from the facts; the story only adds words.
import { api } from './api.js';
import { state } from './state.js';
import { _busyStreaming, enterThread, sendMessage } from './chat.js';
import { _repBar as _bar, renderLedger } from './report.js';
import { EL, _errHtml, _errMsg, _posterImg, act, btnBusy, btnDone, countTo, esc, haptic, loc, toast } from './ui.js';
import { playStory } from './wrapup_story.js';
import { brandMarkHtml } from './brand.js';

const WU_KEY = 'curatarr_wrapup';
const SEEN_KEY = 'curatarr_wrapup_seen';
const PERIODS = [['30d', 'Last 30 days'], ['6m', '6 months'], ['year', 'Year'], ['all', 'All time']];
const SCOPE_LABEL = {all: 'Everything', movie: 'Movies', show: 'TV', anime: 'Anime', music: 'Music'};
const PHASES = ['Counting plays', 'Finding the outliers', 'Mapping taste clusters',
                'Fetching artwork', 'Curator is writing', 'Checking the facts'];
const WEEKDAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];
const TRAIT_LABEL = {night_owl: 'Night Owl', matinee: 'Weekend Matinee', completionist: 'Completionist',
  sampler: 'Sampler', scout: 'Scout', loyalist: 'Loyalist', archivist: 'Archivist', day_one: 'Day One',
  binger: 'Binger', highbrow: 'Highbrow', comfort_seeker: 'Comfort Rewatcher', contrarian: 'Contrarian',
  purger: 'Purger', defender: 'Defender'};
const TRAIT_TITLE = {night_owl: 'The Midnight Shift', matinee: 'The Weekend Matinee', completionist: 'The Completionist',
  sampler: 'The Sampler', scout: 'The Scout', loyalist: 'The Loyalist', archivist: 'The Archivist',
  day_one: 'Day One', binger: 'The Binger', highbrow: 'The Highbrow', comfort_seeker: 'The Comfort Rewatcher',
  contrarian: 'The Contrarian', purger: 'The Purger', defender: 'The Defender'};

const wu = {scope: 'all', period: '30d', year: null, data: null, poll: null, snapshot: false,
            layout: 'card', quizOpen: false, tiles: []};
try { Object.assign(wu, JSON.parse(localStorage.getItem(WU_KEY) || '{}')); } catch (_) { /* private mode: defaults */ }
function _save() {
  try { localStorage.setItem(WU_KEY, JSON.stringify({scope: wu.scope, period: wu.period, year: wu.year})); } catch (_) { /* private mode */ }
}
const _periodKey = () => wu.period === 'year' ? `year:${wu.year || new Date().getFullYear()}` : wu.period;
const _pct = v => Math.round(100 * (v || 0));
const _s = v => esc(String(v ?? ''));
const _isMusic = mt => mt === 'music';
const _poster = (f, title) => (f.posters || {})[title] || null;

export async function loadWrapup() {
  const el = document.getElementById('wrapup-content');
  if (!el) return;
  if (!wu.data) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Pulling your period together…</p>';
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
  if (p === wu.period) return;
  wu.period = p; _save(); haptic('tick'); loadWrapup();
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
    el.innerHTML = _controls(d) + `<div class="wu-empty card"><h2 class="wu-empty-title">Not enough yet</h2>
      <p>${plays ? `${_s(plays)} play${plays === 1 ? '' : 's'} so far in this period.` : 'Nothing played in this period yet.'}
      A wrap-up starts at five plays and three hours — try a longer period or come back later.</p></div>`;
    return;
  }
  el.innerHTML = wu.snapshot
    ? _controls(d) + _snapshot(f, d.story)
    : _controls(d) + _hero(f, d) + '<h2 class="wu-explore">Explore</h2>' + _bento(f) + _story(f, d) + _household(d) + _ledgerSlot(f);
  if (reveal) el.querySelector('.wu-hero')?.classList.add('is-reveal');
  _armCounters(el);
  if (f.curatarr?.ledger && !wu.snapshot) {
    const slot = document.getElementById('wu-ledger');
    if (slot) renderLedger(slot);
  }
}

// ── controls ────────────────────────────────────────────────────────────────
function _controls(d) {
  const per = PERIODS.map(([k, l]) => `<button type="button" class="wu-chip${k === wu.period ? ' is-on' : ''}" aria-pressed="${k === wu.period}" ${act('setWrapupPeriod', k)}>${esc(l)}</button>`).join('');
  const years = wu.period === 'year' && (d.years || []).length > 1
    ? `<div class="wu-chips" role="group" aria-label="Year">${d.years.map(y => {
        const on = +y === +(wu.year || new Date().getFullYear());
        return `<button type="button" class="wu-chip wu-chip-sm${on ? ' is-on' : ''}" aria-pressed="${on}" ${act('setWrapupYear', y)}>${_s(y)}</button>`;
      }).join('')}</div>` : '';
  const scopes = (d.scopes || ['all']).map(s => `<button type="button" class="wu-tab${s === wu.scope ? ' is-on' : ''}" aria-pressed="${s === wu.scope}" ${act('setWrapupScope', s)}>${esc(SCOPE_LABEL[s] || s)}</button>`).join('');
  return `<div class="wu-controls">
    <div class="wu-chips" role="group" aria-label="Period">${per}</div>${years}
    <div class="wu-tabs" role="group" aria-label="Media">${scopes}</div>
    <button type="button" class="btn btn-secondary btn-sm wu-snap-btn" ${act('toggleWrapupSnapshot')}>${wu.snapshot ? 'Full view' : 'Snapshot'}</button></div>`;
}

// ── hero persona ────────────────────────────────────────────────────────────
function _archetype(f, s) {
  return s?.archetype_title || TRAIT_TITLE[f.traits?.[0]?.key] || 'Your period in review';
}

function _hero(f, d) {
  const s = d.story, top = (f.top || [])[0];
  const art = top ? `<div class="wu-hero-art">${_posterImg(_poster(f, top.title), 200, _isMusic(top.media_type) ? 200 : 300, _isMusic(top.media_type))}</div>` : '';
  if (wu.quizOpen) return `<section class="card wu-hero" aria-labelledby="wu-quiz-q">${art}<div class="wu-hero-text">${_quiz(f)}</div></section>`;
  const mainTrait = s?.archetype_trait || f.traits?.[0]?.key;
  const badges = (f.traits || []).filter(t => t.key !== mainTrait).slice(0, 3)
    .map(t => `<span class="wu-badge" title="${esc(t.evidence)}">${esc(TRAIT_LABEL[t.key] || t.key)}</span>`).join('');
  const sub = s?.archetype_subhead || (f.traits?.[0]?.evidence ? `${f.traits[0].evidence}.` : '');
  const gen = d.generating ? _ticker(0) : _genButton(d);
  return `<section class="card wu-hero" aria-labelledby="wu-arch">${art}
    <div class="wu-hero-text">
      <div class="wu-eyebrow">${esc(f.period?.label || '')} · ${esc(SCOPE_LABEL[f.scope] || '')}</div>
      <h2 id="wu-arch" class="wu-arch">${esc(_archetype(f, s))}</h2>
      ${sub ? `<p class="wu-sub">${esc(sub)}</p>` : ''}
      ${badges ? `<div class="wu-badges" aria-label="Also earned">${badges}</div>` : ''}
      ${s?.share_line && s.share_line !== s.archetype_title ? `<p class="wu-share-line">“${esc(s.share_line)}”</p>` : ''}
      <div class="wu-hero-actions" id="wu-gen">${gen}</div>
    </div></section>`;
}

function _genButton(d) {
  const s = d.story;
  const label = !s ? 'Let the curator write it' : d.story_stale ? 'Numbers moved — rewrite' : 'Rewrite';
  const ask = s ? `<button type="button" class="btn btn-secondary btn-sm" ${act('askWrapup')}>Ask about it</button>` : '';
  const pl = `<button type="button" class="btn btn-ghost btn-sm" ${act('pushWrapupPlaylist', EL)}>Make it a Plex playlist</button>`;
  const play = `<button type="button" class="btn btn-primary btn-sm" ${act('playWrapupStory')}>Play your wrap-up</button>`;
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
  const opts = (f.top || []).slice(0, 4).map((t, i) => ({...t, i})).sort((a, b) => a.title.localeCompare(b.title));
  return `<div class="wu-eyebrow">Before the reveal</div>
    <h2 id="wu-quiz-q" class="wu-quiz-q">Which one got the most of your time?</h2>
    <div class="wu-quiz">${opts.map(o => `<button type="button" class="wu-quiz-opt" ${act('answerWrapupQuiz', o.i)}>
      ${_posterImg(_poster(f, o.title), 72, _isMusic(o.media_type) ? 72 : 108, _isMusic(o.media_type))}<span>${esc(o.title)}</span></button>`).join('')}</div>
    <button type="button" class="btn btn-ghost btn-sm" ${act('answerWrapupQuiz', -1)}>Just show me</button>`;
}
export function answerWrapupQuiz(i) {
  const top = wu.data?.facts?.top || [];
  if (i >= 0) {
    if (i === 0) { haptic('medium'); toast(`Right — ${top[0].title}`, 'success'); }
    else { haptic('light'); toast(`It was ${top[0].title}`, 'info'); }
  }
  wu.quizOpen = false;
  _markSeen();
  _render(true);
}

// ── bento ───────────────────────────────────────────────────────────────────
function _tile(cls, label, body, share) {
  const i = wu.tiles.push(share || label) - 1;
  return `<article class="wu-tile ${cls}"><div class="wu-tile-head"><div class="wu-tile-lbl">${esc(label)}</div>
    <button type="button" class="wu-tile-share" aria-label="Share ${esc(label)}" ${act('shareWrapupTile', i)}>
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
  return `<div class="wu-cal" role="img" aria-label="${dates.length} days with ${esc(f.top[0].title)}">${cells.join('')}</div>`;
}

function _bento(f) {
  const t = f.tempo || {}, tiles = [];
  if (t.hours != null) {
    const max = Math.max(1, ...(t.by_hour_h || [0]));
    const clock = (t.by_hour_h || []).map((h, i) => `<span class="wu-hr${i === t.peak_hour ? ' is-peak' : ''}" title="${i}:00 · ${_s(h)} h">${_bar('wu-hr-fill', 100 * h / max)}</span>`).join('');
    const delta = t.prev_hours ? Math.round(100 * (t.hours - t.prev_hours) / t.prev_hours) : null;
    const approx = t.hours_estimated_share > 0.25 ? '≈' : '';
    tiles.push(_tile('wu-span-hours', 'Hours & rhythm',
      `<div class="wu-big">${approx}${_num(t.hours, 'h')}</div>
       <div class="wu-meta">${_num(t.active_days)} active days · best streak ${_s(t.longest_streak_days)} days${delta != null ? ` · <b class="${delta >= 0 ? 't-amber' : ''}">${delta >= 0 ? '+' : ''}${delta}%</b> vs the span before` : ''}</div>
       <div class="wu-clock" role="img" aria-label="Hours by time of day, peak at ${_s(t.peak_hour)}:00">${clock}</div>
       <div class="wu-clock-axis" aria-hidden="true"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
       <div class="wu-meta">Peak ${_s(t.peak_hour)}:00 · busiest on ${esc(WEEKDAYS[t.peak_weekday] || '')} · biggest day ${_s(t.biggest_day?.date)} (${_s(t.biggest_day?.hours)} h)</div>`,
      `${approx}${t.hours} hours on ${t.active_days} days, peak at ${t.peak_hour}:00`));
  }
  const hc = f.highbrow_comfort;
  if (hc) tiles.push(_tile('wu-span-split', 'Highbrow vs comfort',
    `<div class="wu-split">${_bar('wu-split-hb', _pct(hc.highbrow))}${_bar('wu-split-cf', _pct(hc.comfort))}<i class="wu-split-ot"></i></div>
     <div class="wu-split-legend"><span><b>${_pct(hc.highbrow)}%</b> acclaimed${hc.top_highbrow ? ` · ${esc(hc.top_highbrow)}` : ''}</span>
     <span><b>${_pct(hc.comfort)}%</b> comfort${hc.top_comfort ? ` · ${esc(hc.top_comfort)}` : ''}</span><span>${_pct(hc.other)}% other</span></div>`,
    `${_pct(hc.highbrow)}% acclaimed, ${_pct(hc.comfort)}% comfort rewatches`));
  const c = f.contrarian;
  if (c?.title) tiles.push(_tile('wu-span-poster', 'The contrarian gem', _posterTile(f, c,
    `<div class="wu-vs"><span><b>${_s(c.you)}</b>${c.basis === 'rewatched' ? 'you (rewatched)' : 'you'}</span><span><b>${_s(c.community)}</b>everyone</span></div>`),
    `${c.title}: me ${c.you}, everyone ${c.community}`));
  const dm = f.dismissal;
  if (dm?.title) tiles.push(_tile('wu-span-poster', 'The unpopular dismissal', _posterTile(f, dm,
    `<div class="wu-meta">${esc(dm.how)} · everyone else gives it ${_s(dm.community)}/10</div>`),
    `Walked away from ${dm.title} (${dm.community}/10)`));
  const b = f.binge?.in_48h || f.binge?.deepest_sitting;
  if (b) tiles.push(_tile('wu-span-sm', f.binge?.in_48h ? '48-hour binge' : 'Deepest sitting',
    `<div class="wu-big">${_num(b.episodes, 'eps')}</div><div class="wu-meta">${esc(b.series)}</div>`,
    `${b.episodes} episodes of ${b.series}${f.binge?.in_48h ? ' in 48 hours' : ' in one sitting'}`));
  const e = f.eras;
  if (e) tiles.push(_tile('wu-span-sm', 'Viewing age',
    `<div class="wu-big wu-year">${_s(e.viewing_age_year)}</div>
     <div class="wu-meta">${esc(e.oldest.title)} (${_s(e.oldest.year)}) → ${esc(e.newest.title)} (${_s(e.newest.year)})</div>
     <div class="wu-decades">${Object.entries(e.decades).map(([k, v]) => `<span title="${esc(k)} · ${_pct(v)}%">${_bar('wu-dec-fill', _pct(v))}<em>${esc(k.slice(2))}</em></span>`).join('')}</div>`,
    `Viewing age ${e.viewing_age_year}`));
  if (e?.shortest) tiles.push(_tile('wu-span-sm', 'Shortest vs longest',
    `<div class="wu-pairline"><b>${_s(e.shortest.minutes)}</b> min <span>${esc(e.shortest.title)}</span></div>
     <div class="wu-pairline"><b>${_s(e.longest.minutes)}</b> min <span>${esc(e.longest.title)}</span></div>`,
    `${e.shortest.title} (${e.shortest.minutes} min) to ${e.longest.title} (${e.longest.minutes} min)`));
  const ds = f.discovery;
  if (ds?.new_share != null) tiles.push(_tile('wu-span-sm', ds.new_share >= 0.5 ? 'Scout' : 'Loyalist',
    `<div class="wu-big">${_num(_pct(ds.new_share), '%')}</div><div class="wu-meta">of the time on titles new to you${ds.best_new ? ` · best find ${esc(ds.best_new)}` : ''}</div>`,
    `${_pct(ds.new_share)}% on titles new to me`));
  const cb = (f.comebacks || [])[0];
  if (cb) tiles.push(_tile('wu-span-sm', 'The comeback',
    `<div class="wu-tile-title">${esc(cb.title)}</div><div class="wu-meta">back after ${_s(Math.round(cb.gap_days / 30))} months away</div>`,
    `${cb.title} is back after ${Math.round(cb.gap_days / 30)} months`));
  if (t.title_streak || (t.top_calendar || []).length > 2) {
    const ts = t.title_streak;
    tiles.push(_tile('wu-span-cal', 'Days with your #1',
      `<div class="wu-meta"><b>${esc(f.top[0].title)}</b> on ${_s((t.top_calendar || []).length)} days${ts ? ` · longest run ${_s(ts.days)} days in a row (${esc(ts.title)})` : ''}</div>${_calendar(t.top_calendar, f)}`,
      `${f.top[0].title} on ${(t.top_calendar || []).length} days`));
  }
  const m = f.music;
  if (m?.top_artists?.length) tiles.push(_tile('wu-span-poster', 'On the stereo',
    `<ol class="wu-list">${m.top_artists.slice(0, 5).map(a => `<li><span>${esc(a.artist)}</span><b>${_s(a.hours)} h</b></li>`).join('')}</ol>
     ${m.on_repeat ? `<div class="wu-meta">On repeat: “${esc(m.on_repeat.track)}” ${_s(m.on_repeat.plays)}× on ${_s(m.on_repeat.date)}</div>` : ''}`,
    `Top artist: ${m.top_artists[0].artist}`));
  const g = f.genres;
  if (g?.first_time?.length || g?.rising?.length) tiles.push(_tile('wu-span-sm', 'Genre drift',
    `${(g.rising || []).map(x => `<div class="wu-pairline"><b class="t-amber">+${_s(x.pp)}</b> pts <span>${esc(x.genre)}</span></div>`).join('')}
     ${(g.fading || []).slice(0, 1).map(x => `<div class="wu-pairline"><b>${_s(x.pp)}</b> pts <span>${esc(x.genre)}</span></div>`).join('')}
     ${g.first_time?.length ? `<div class="wu-meta">${_s(g.new_count)} new to you: ${esc(g.first_time.slice(0, 3).join(', '))}</div>` : ''}`,
    g.rising?.length ? `${g.rising[0].genre} is rising` : `${g.new_count} genres new to me`));
  const cu = f.curatarr || {};
  if (cu.graveyard) tiles.push(_tile('wu-span-purge', 'The graveyard',
    `<div class="wu-big">${_num(cu.graveyard.gb_freed, 'GB')}</div>
     <div class="wu-meta">${_s(cu.graveyard.deleted)} deleted · biggest ${esc(cu.graveyard.biggest.title)} (${_s(cu.graveyard.biggest.gb)} GB)</div>
     ${cu.came_back ? `<div class="wu-meta">Came back anyway: <b>${esc(cu.came_back.title)}</b></div>` : ''}`,
    `${cu.graveyard.gb_freed} GB freed, ${cu.graveyard.deleted} titles deleted`));
  if (cu.shield) tiles.push(_tile('wu-span-sm', 'The shield',
    `<div class="wu-big">${_num(cu.shield.overrides)}</div><div class="wu-meta">kept over the curator's objection${cu.shield.top_reason ? ` · mostly “${esc(cu.shield.top_reason)}”` : ''}${cu.shield.manual_protections ? ` · ${_s(cu.shield.manual_protections)} protected by hand` : ''}</div>`,
    `${cu.shield.overrides} titles kept over the curator's objection`));
  if (cu.most_debated) tiles.push(_tile('wu-span-sm', 'Most debated',
    `<div class="wu-tile-title">${esc(cu.most_debated.title)}</div><div class="wu-meta">${_s(cu.most_debated.messages)} messages · ended ${esc(cu.most_debated.outcome)}</div>`,
    `Most debated: ${cu.most_debated.title}`));
  const dr = f.drift;
  if (dr?.clusters?.length) {
    const names = Object.fromEntries((wu.data.story?.cluster_names || []).map(n => [n.ref, n.name]));
    tiles.push(_tile('wu-span-clusters', `Taste clusters · ${dr.label}`, dr.clusters.map(k =>
      `<div class="wu-cluster"><span class="wu-cl-name" title="${esc((k.top_titles || []).join(', '))}">${esc(names[k.id] || `Around ${k.top_titles?.[0] || ''}`)}</span>
       <span class="wu-cl-bar">${_bar('wu-cl-fill', _pct(k.share))}</span>
       <span class="wu-cl-trend is-${esc(k.trend)}">${k.trend === 'rising' ? '↑' : k.trend === 'fading' ? '↓' : '→'} ${_pct(k.share)}%</span></div>`).join(''),
      `My taste this period: ${dr.label}`));
  }
  if (cu.dust_collector) tiles.push(_tile('wu-span-sm', 'Dust collector',
    `<div class="wu-big">${_num(cu.dust_collector.gb, 'GB')}</div><div class="wu-meta">${esc(cu.dust_collector.title)} — never played by you</div>`,
    `${cu.dust_collector.title}: ${cu.dust_collector.gb} GB, never played`));
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
  const spare = (f.top || []).filter(t => !shown.has(t.title) && _poster(f, t.title));
  const ch = s.era_chapters.map((c, i) => {
    const items = own[i].length ? own[i] : spare.splice(0, 1);
    const posters = items.map(fan).join('');
    return `<article class="card wu-chapter${posters ? '' : ' wu-chapter-bare'}">
      ${posters ? `<div class="wu-fan" aria-hidden="true">${posters}</div>` : ''}
      <div class="wu-ch-body"><div class="wu-ch-n">Chapter ${i + 1}</div>
        <h3 class="wu-ch-title">${esc(c.title)}</h3>
        ${c.kicker ? `<div class="wu-ch-kicker">${esc(c.kicker)}</div>` : ''}
        ${c.commentary ? `<p class="wu-ch-text">${esc(c.commentary)}</p>` : ''}
        <button type="button" class="btn btn-ghost btn-sm" ${act('discussWrapupChapter', i)}>Discuss this era</button></div></article>`;
  }).join('');
  const rt = s.roast_and_toast;
  const pair = rt ? `<div class="wu-pair"><article class="wu-roast"><div class="wu-tile-lbl">Roast</div><p>${esc(rt.roast)}</p></article>
    <article class="wu-toast"><div class="wu-tile-lbl">Toast</div><p>${esc(rt.toast)}</p></article></div>` : '';
  const la = s.look_ahead_hook, pick = la?.item_ref && refs[la.item_ref];
  const hook = la ? `<article class="card wu-hook">${pick ? _posterImg(pick.poster || _poster(f, pick.title), 72, 108, _isMusic(pick.media_type)) : ''}
    <div><div class="wu-tile-lbl">Next up</div><p>${esc(la.challenge)}</p>
    ${pick ? `<button type="button" class="btn btn-secondary btn-sm" ${act('discussWrapupHook')}>Talk me into it</button>` : ''}</div></article>` : '';
  const note = d.story_source === 'fallback'
    ? '<p class="wu-note">The curator was busy, so these chapters are the numbers without its commentary. Rewrite once it is free.</p>'
    : d.story_stale ? '<p class="wu-note">The numbers moved since this was written — the tiles are current, the words are from before.</p>' : '';
  const quotes = `<p class="wu-note">${d.quotes
    ? 'The curator may quote your own debate messages back to you — only here, in your own wrap-up.'
    : 'The curator does not quote your debate messages.'}
    <button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupQuotes')}>${d.quotes ? 'Stop quoting me' : 'Allow quotes'}</button></p>`;
  return `<section class="wu-chapters" aria-label="Chapters">${ch}</section>${pair}${hook}${note}${quotes}`;
}

// The household is opt-in: only members who turned it on are compared, and
// only by rank and median — never another member's name or titles.
function _household(d) {
  const h = d.household || {};
  if (!h.consented) {
    return `<p class="wu-note">Compare your wrap-up with the household: your rank by hours and the household's taste line, among the members who turn this on too.
      <button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupHousehold')}>Compare with the household</button></p>`;
  }
  const off = `<button type="button" class="btn btn-ghost btn-sm" ${act('toggleWrapupHousehold')}>Stop comparing</button>`;
  if (!h.rank_hours) {
    return `<p class="wu-note">You are in. The comparison starts once another member turns it on and has a wrap-up for this period. ${off}</p>`;
  }
  const only = h.only_you ? ` · only you watched <b>${esc(h.only_you)}</b>` : '';
  return `<article class="card wu-household"><div class="wu-tile-lbl">In the house</div>
    <p><b>#${_s(h.rank_hours)}</b> of ${_s(h.members)} by hours${only}${h.radar_median ? ' · the dashed line on your taste radar is the household median' : ''}</p>
    <p class="wu-note">Only members who turned this on are compared, and nobody sees anyone else's titles. ${off}</p></article>`;
}

export async function toggleWrapupHousehold() {
  if (!wu.data) return;
  try {
    const on = !wu.data.household?.consented;
    await api('/api/wrapup/prefs', 'POST', {household: on});
    haptic('tick');
    toast(on ? 'You are compared with the members who said yes too.' : 'You are out of every household comparison.', 'success');
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
    toast(r.quotes ? 'The curator may quote you again from the next rewrite.' : 'No more quotes from the next rewrite on.', 'success');
    _render();
  } catch (e) {
    toast(_errMsg(e).slice(0, 200), 'danger');
  }
}

// ── "Ask your wrap-up": a chat thread whose context is the fact sheet ───────
async function _openThread(message) {
  if (_busyStreaming()) return;
  const ctx = {kind: 'wrapup', wrapup_scope: wu.scope, wrapup_period: _periodKey()};
  const title = wu.data?.story?.archetype_title || 'your wrap-up';
  if (!await enterThread(ctx, `Discussing your wrap-up — ${title}`, null)) return;
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
  _openThread(`About the chapter "${c.title}" — is that fair? What does it say about what I should watch next?`);
}
export function discussWrapupHook() {
  const s = wu.data?.story, pick = s?.refs?.[s?.look_ahead_hook?.item_ref];
  if (pick) { haptic('light'); _openThread(`My wrap-up says I should play "${pick.title}" next. Make the case.`); }
}

// ── Plex playlist (PlayStation's personal playlist) ────────────────────────
export async function pushWrapupPlaylist(btn) {
  btnBusy(btn, 'Building the playlist…');
  try {
    const r = await api('/api/wrapup/playlist', 'POST', {scope: wu.scope, period: _periodKey()});
    const names = (r.pushed || []).map(p => `“${p.title}” (${p.items})`).join(' and ');
    haptic('medium');
    toast(`On Plex now: ${names}${r.missing?.length ? ` · not found: ${r.missing.slice(0, 3).join(', ')}` : ''}`, 'success');
    btnDone(btn, 'Playlist updated');
  } catch (e) {
    toast(_errMsg(e).slice(0, 200), 'danger');
    btnDone(btn, 'Make it a Plex playlist');
  }
}

// ── snapshot: the share card and the festival lineup (Amazon) ──────────────
function _snapshot(f, s) {
  const t = f.tempo || {}, top = (f.top || []).slice(0, 5);
  const layouts = `<div class="wu-chips wu-snap-layouts" role="group" aria-label="Layout">${[['card', 'Card'], ['lineup', 'Lineup']].map(([k, l]) =>
    `<button type="button" class="wu-chip wu-chip-sm${wu.layout === k ? ' is-on' : ''}" aria-pressed="${wu.layout === k}" ${act('setWrapupLayout', k)}>${l}</button>`).join('')}</div>`;
  const share = `<button type="button" class="btn btn-primary btn-sm" ${act('shareWrapup')}>Share</button>`;
  if (wu.layout === 'lineup') {
    const [head, ...rest] = top;
    return layouts + `<section class="card wu-snap wu-lineup" aria-label="Shareable lineup">
      <div class="wu-eyebrow">Curatarr presents · ${esc(f.period?.label || '')}</div>
      <div class="wu-lu-head">${esc(head?.title || '')}</div>
      <div class="wu-lu-rest">${rest.slice(0, 2).map(x => `<span>${esc(x.title)}</span>`).join('')}</div>
      <div class="wu-lu-small">${rest.slice(2).map(x => `<span>${esc(x.title)}</span>`).join('')}${(f.comebacks || []).slice(0, 1).map(c => `<span>${esc(c.title)} (reunion)</span>`).join('')}</div>
      <div class="wu-lu-foot">${esc(_archetype(f, s))} · ${_s(t.hours)} hours · ${_s(t.active_days)} days</div>
      ${share}</section>`;
  }
  const stats = [[t.hours, 'hours'], [t.active_days, 'active days'],
    f.eras ? [f.eras.viewing_age_year, 'viewing age'] : [f.curatarr?.graveyard?.gb_freed, 'GB freed']]
    .filter(([v]) => v != null);
  return layouts + `<section class="card wu-snap" aria-label="Shareable summary">
    <div class="wu-eyebrow">${brandMarkHtml('wu-mark')}Curatarr · ${esc(f.period?.label || '')}</div>
    <h2 class="wu-arch">${esc(_archetype(f, s))}</h2>
    <p class="wu-sub">${esc(s?.share_line && s.share_line !== s.archetype_title ? s.share_line : (s?.archetype_subhead || ''))}</p>
    <div class="wu-snap-stats">${stats.map(([v, l]) => `<div><div class="wu-num-static">${_s(v)}</div><div class="wu-meta">${esc(l)}</div></div>`).join('')}</div>
    <div class="wu-snap-posters">${top.map(x => _posterImg(_poster(f, x.title), 64, _isMusic(x.media_type) ? 64 : 96, _isMusic(x.media_type))).join('')}</div>
    <ol class="wu-snap-top">${top.map(x => `<li>${esc(x.title)}</li>`).join('')}</ol>
    ${share}</section>`;
}

async function _shareText(text) {
  try {
    if (navigator.share) await navigator.share({title: 'My Curatarr wrap-up', text});
    else { await navigator.clipboard.writeText(text); toast('Copied to clipboard', 'success'); }
  } catch (e) {
    if (e?.name !== 'AbortError') toast("Couldn't share — copy it by hand", 'danger');
  }
}
export function shareWrapup() {
  const d = wu.data, s = d?.story, f = d?.facts || {};
  const head = `${_archetype(f, s)} — ${f.period?.label || ''}`;
  const line = s?.share_line && s.share_line !== s.archetype_title ? `\n${s.share_line}` : '';
  _shareText(`${head}${line}\n` + (f.top || []).slice(0, 5).map((x, i) => `${i + 1}. ${x.title}`).join('\n'));
}
// Strava's lesson: every scene can be shared on its own.
export function shareWrapupTile(i) {
  const text = wu.tiles[i];
  if (text) _shareText(`${text} — my ${wu.data?.facts?.period?.label || 'wrap-up'} on Curatarr`);
}

// ── generation + ticker ─────────────────────────────────────────────────────
export async function generateWrapup(btn) {
  btnBusy(btn, 'Starting…');
  try {
    const r = await api('/api/wrapup/generate', 'POST', {scope: wu.scope, period: _periodKey()});
    haptic('medium');
    _follow(r.task_id);
  } catch (e) {
    toast('Failed: ' + _errMsg(e).slice(0, 160), 'danger');
    btnDone(btn, 'Let the curator write it');
  }
}

function _ticker(phase) {
  return `<ol class="wu-ticker" role="status" aria-live="polite">${PHASES.map((p, i) =>
    `<li class="${i < phase ? 'is-done' : i === phase ? 'is-now' : ''}">${esc(p)}</li>`).join('')}</ol>`;
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
      if (st.status === 'error') toast(st.error || 'The curator could not finish', 'danger');
      else if (st.status === 'skipped') toast(st.message || 'Not enough played yet', 'info');
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
  return `<details class="section wu-ledger"><summary class="section-head"><h3>Curation ledger</h3>
    <span class="section-hint">the debates, month by month — admin only</span></summary><div id="wu-ledger" class="section-body"></div></details>`;
}
