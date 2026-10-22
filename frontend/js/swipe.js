// ── Swipe: rate titles near your taste, one card at a time ──────────────────
// Library titles closest to the user's taste vector that they have neither
// played in Plex nor answered yet (services/swipe.py). Six reactions in two
// groups: not seen (Not seen, Want to watch) and seen (Disliked, Meh, Liked,
// Loved = stars 2 / 5 / 7 / 10 in the same answer store). Exact stars 1-10
// are a long-press on a seen reaction, or keys 1-0 / S. Most cards are
// unseen, so the two unseen answers own the two easy swipes.
//
// The deck: the top card follows the pointer (tilt from where it was
// grabbed, a stamp and a glow growing toward the decision), a drag past the
// threshold or a quick flick commits, and the card flies off the way it was
// thrown while the next one rises from behind. Buttons and keys play the
// same flight. A loved title chains a few of its nearest neighbours into
// the stack; undo flies the last card back in from where it left.
//
// Gestures: ← not seen, → want to watch, ↑ loved, ↓ disliked (Meh and
// Liked are buttons / M, L).
// Keys: ← N not seen · → W want to watch · ↓ D disliked · M meh · L liked ·
// ↑ F loved ·
// 1-9, 0 exact stars · S exact-star row · Enter seen without stars (row
// open) · U / Backspace undo · Esc closes the star row.
import { api } from './api.js';
import { _errHtml, _errMsg, act, activeDialog, emptyHtml, esc, proxyImg, toast } from './ui.js';
import { haptic } from './mobile.js';

const CATS = [['movie', 'Movies'], ['show', 'TV Shows'], ['anime', 'Anime']];
const REACT = {
  notseen:  {label: 'Not seen',      stamp: 'Not seen', dir: 'left',  stars: null, keys: 'N ArrowLeft'},
  want:     {label: 'Want to watch', stamp: 'Want it',  dir: 'right', stars: null, keys: 'W ArrowRight'},
  disliked: {label: 'Disliked',      stamp: 'Nope',     dir: 'down',  stars: 2,    keys: 'D ArrowDown'},
  meh:      {label: 'Meh',           stamp: 'Meh',      dir: null,    stars: 5,    keys: 'M'},
  liked:    {label: 'Liked',         stamp: 'Liked',    dir: null,    stars: 7,    keys: 'L'},
  loved:    {label: 'Loved',         stamp: 'Loved',    dir: 'up',    stars: 10,   keys: 'F ArrowUp'},
};
const ORDER = ['notseen', 'want', 'disliked', 'meh', 'liked', 'loved'];
const UNSEEN = new Set(['notseen', 'want']);
const DIR_REACT = {left: 'notseen', right: 'want', down: 'disliked', up: 'loved'};
const KEY_REACT = {arrowleft: 'notseen', n: 'notseen', arrowright: 'want', w: 'want', arrowdown: 'disliked',
                   d: 'disliked', m: 'meh', l: 'liked', arrowup: 'loved', f: 'loved'};
const LONG_PRESS_MS = 450;    // hold a seen reaction this long for exact stars
const _SVG = p => `<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const ICON = {
  notseen: _SVG('<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24M1 1l22 22"/>'),
  disliked: _SVG('<path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zM17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/>'),
  want: _SVG('<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>'),
  meh: _SVG('<circle cx="12" cy="12" r="10"/><path d="M8 15h8M9 9h.01M15 9h.01"/>'),
  liked: _SVG('<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/>'),
  loved: _SVG('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),
  seen: _SVG('<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'),
  star: _SVG('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>'),
};
const DRAG_DECIDE_PX = 110;   // drag this far along one axis and it commits
const FLICK_PX_MS = 0.5;      // ...or throw it this fast past FLICK_MIN_PX
const FLICK_MIN_PX = 36;
const DECK_DEPTH = 3;
// A decision you can feel (Android; silent under reduced motion): loved
// gets a double pulse, everything else one short tap.
const BUZZ = {notseen: 10, want: 12, disliked: 14, meh: 8, liked: 14, loved: [12, 50, 18], seen: 10};

let _cat = 'show';
let _queue = [];
let _gen = 1;                 // bumps on every (re)load: late answers of a stale stack are dropped
let _loading = 0;             // the _gen a fill is running for, 0 when idle
let _keysBound = false;
let _exactOpen = false;
let _undoing = false;
let _watchlistWarned = false;  // a failed watchlist write is said once per session, not on every card
let _history = [];            // [{card, cat, kind, seen, stars, want}] this session, oldest first
let _progress = {since: 0, every: 10};
let _ambIdx = 0, _ambUrl = null;
let _uid = 0;

function _el(id) { return document.getElementById(id); }
function _active() { return !!_el('swipe-view')?.classList.contains('active'); }
function _top() { return _el('swipe-deck')?.querySelector('.swipe-card[data-depth="0"]:not(.is-leaving)'); }

export function reactionOf(seen, stars, want) {
  if (!seen) return want ? 'want' : 'notseen';
  if (stars == null) return 'seen';
  return stars <= 3 ? 'disliked' : stars <= 6 ? 'meh' : stars <= 8 ? 'liked' : 'loved';
}

export async function loadSwipe() {
  if (!_keysBound) {
    document.addEventListener('keydown', _onKey);
    _keysBound = true;
  }
  _gen += 1;
  _queue = [];
  _closeExact();
  _renderTabs();
  _renderReactions();
  _renderRecent();
  _renderSession();
  _loadStats();
  _deckMsg('<p class="loading" role="status">Finding titles near your taste…</p>');
  await _fill();
}

export function swipeCategory(cat) {
  if (!CATS.some(([c]) => c === cat) || cat === _cat) return;
  _cat = cat;
  loadSwipe();
}

// ── rails ──────────────────────────────────────────────────────────────────
function _renderTabs() {
  const el = _el('swipe-tabs');
  if (!el) return;
  el.innerHTML = CATS.map(([c, label]) =>
    `<button class="cat-tab${c === _cat ? ' active' : ''}" ${act('swipeCategory', c)}
       aria-pressed="${c === _cat}">${esc(label)}</button>`).join('');
}

function _renderReactions() {
  const el = _el('swipe-reactions');
  if (!el) return;
  el.innerHTML = ORDER.map(k => {
    const r = REACT[k];
    const hint = r.keys.split(' ').map(x => x.replace('Arrow', '')).join(' / ');
    const exact = UNSEEN.has(k) ? '' : ' · hold for exact stars';
    return `<button class="swipe-react" data-kind="${k}" data-group="${UNSEEN.has(k) ? 'unseen' : 'seen'}"
              ${act('swipeReact', k)} aria-keyshortcuts="${r.keys}" title="${esc(r.label)} (${esc(hint)})${exact}">
              <span class="swipe-react-ic">${ICON[k]}</span><span class="swipe-react-l">${esc(r.label)}</span></button>`;
  }).join('');
  _bindLongPress(el);
}

// Hold a seen reaction (Disliked ... Loved) for the exact-star row; the
// click that ends the hold is swallowed so it doesn't answer as well.
function _bindLongPress(el) {
  if (el._longPress) return;
  el._longPress = true;
  let timer = null, fired = false;
  el.addEventListener('pointerdown', e => {
    const b = e.target.closest('.swipe-react[data-group="seen"]');
    if (!b || e.button !== 0) return;
    fired = false;
    clearTimeout(timer);
    timer = setTimeout(() => { fired = true; haptic(8); if (!_exactOpen) swipeExact(); }, LONG_PRESS_MS);
  });
  for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) el.addEventListener(ev, () => clearTimeout(timer));
  el.addEventListener('click', e => {
    if (fired) { fired = false; e.preventDefault(); e.stopPropagation(); }
  }, true);
  el.addEventListener('contextmenu', e => { if (e.target.closest('.swipe-react')) e.preventDefault(); });
}

async function _loadStats() {
  const el = _el('swipe-stats');
  const cat = _cat, gen = _gen;
  try {
    const r = await api('/api/swipe/stats');
    if (gen !== _gen) return;
    const s = (r.stats || {})[cat] || {};
    _progress = {since: (r.since_rebuild || {})[cat] || 0, every: r.rebuild_every || 10};
    _renderProgress();
    const total = (s.seen || 0) + (s.not_seen || 0);
    if (el) el.textContent = total
      ? `All time here: ${total} answered · ${s.seen || 0} seen (${s.rated || 0} rated) · ${s.not_seen || 0} not seen`
        + (s.want ? ` (${s.want} want to watch)` : '')
      : 'Nothing answered here yet.';
  } catch (e) {
    if (el) el.textContent = `Couldn't load your counts - ${_errMsg(e)}`;
  }
}

function _renderProgress(refreshing) {
  const el = _el('swipe-progress');
  if (!el) return;
  const {since, every} = _progress;
  const frac = refreshing ? 1 : Math.min(1, since / every);
  el.classList.toggle('is-refreshing', !!refreshing);
  el.innerHTML = `
    <div class="swipe-ring" role="img" aria-label="${refreshing ? 'Taste profile refreshing' : `${since} of ${every} answers until the next taste refresh`}">
      <svg viewBox="0 0 36 36" aria-hidden="true"><circle class="swipe-ring-bg" cx="18" cy="18" r="15.9"/>
        <circle class="swipe-ring-fg" cx="18" cy="18" r="15.9" pathLength="100"/></svg>
      <span>${refreshing ? '✓' : `${since}/${every}`}</span>
    </div>
    <div class="swipe-progress-t">${refreshing
      ? '<b>Refreshing your taste</b><span>Your answers are being folded in.</span>'
      : `<b>${every - since} to go</b><span>seen-it answers until your taste profile refreshes</span>`}</div>`;
  el.querySelector('.swipe-ring-fg')?.style.setProperty('stroke-dasharray', `${Math.round(frac * 100)} 100`);
}

function _renderSession() {
  const el = _el('swipe-session');
  if (!el) return;
  const n = {notseen: 0, want: 0, disliked: 0, meh: 0, liked: 0, loved: 0, seen: 0};
  for (const h of _history) n[h.kind] += 1;
  const total = _history.length;
  el.innerHTML = total
    ? `<div class="swipe-session-n"><b>${total}</b> answered</div>
       <div class="swipe-session-grid">${[...ORDER, 'seen'].filter(k => n[k]).map(k =>
         `<span class="swipe-chip" data-kind="${k}" title="${esc(REACT[k]?.label || 'Seen, no stars')}">${ICON[k]}${n[k]}</span>`).join('')}</div>`
    : '<span class="fs-12 t3">Your answers will add up here.</span>';
}

function _renderRecent() {
  const el = _el('swipe-recent');
  if (!el) return;
  const mine = _history.filter(h => h.cat === _cat).slice(-12).reverse();
  el.innerHTML = mine.length ? mine.map((h, i) => {
    const thumb = h.card.poster_url
      ? `<img src="${esc(proxyImg(h.card.poster_url))}" alt="" loading="lazy">`
      : '<span class="swipe-recent-ph" aria-hidden="true"></span>';
    const what = h.kind === 'seen' ? 'Seen' : REACT[h.kind].label;
    const exact = h.seen && h.stars != null && h.stars !== REACT[h.kind]?.stars ? ` · ${h.stars}/10` : '';
    return `<li class="${i === 0 ? 'is-new' : ''}">${thumb}
      <span class="swipe-recent-t">${esc(h.card.title)}</span>
      <span class="swipe-chip" data-kind="${h.kind}" title="${esc(what + exact)}">${ICON[h.kind]}<span class="sr-only">${esc(what + exact)}</span></span></li>`;
  }).join('') : '<li class="swipe-recent-empty">Nothing yet in this category.</li>';
}

function _say(text) {
  const el = _el('swipe-live');
  if (!el) return;
  el.textContent = '';
  requestAnimationFrame(() => { el.textContent = text; });
}

function _note(html) {
  const deck = _el('swipe-deck');
  if (!deck) return;
  deck.querySelector('.swipe-note')?.remove();
  const n = document.createElement('div');
  n.className = 'swipe-note';
  n.innerHTML = html;
  deck.appendChild(n);
  setTimeout(() => n.remove(), 2600);
}

// ── the stack ──────────────────────────────────────────────────────────────
function _add(cards, at) {
  const ids = new Set(_queue.map(c => c.doc_id));
  const titles = new Set(_queue.map(c => (c.title || '').toLowerCase()));
  const fresh = (cards || []).filter(c => {
    const t = (c.title || '').toLowerCase();
    if (ids.has(c.doc_id) || titles.has(t)) return false;
    ids.add(c.doc_id); titles.add(t);
    return true;
  });
  if (at == null) _queue.push(...fresh);
  else _queue.splice(at, 0, ...fresh);
  return fresh;
}

function _skipParam() {
  const ids = _queue.slice(0, 40).map(c => c.doc_id).join(',');
  return ids ? `&skip=${encodeURIComponent(ids)}` : '';
}

async function _fill() {
  const gen = _gen, cat = _cat;
  if (_loading === gen) return;
  _loading = gen;
  try {
    const r = await api(`/api/swipe/next?category=${encodeURIComponent(cat)}&n=8${_skipParam()}`);
    if (gen !== _gen) return;
    _add(r.cards);
    if (!_queue.length) {
      _deckMsg(r.reason === 'no_taste'
        ? emptyHtml('No taste profile for this category yet. Watch a few titles in Plex first, then come back.')
        : _doneHtml());
      return;
    }
    _layout();
  } catch (e) {
    if (gen === _gen && !_queue.length) _deckMsg(_errHtml(e, act('loadSwipe')));
  } finally {
    if (_loading === gen) _loading = 0;
  }
}

function _doneHtml() {
  const label = (CATS.find(([c]) => c === _cat) || [])[1] || 'this category';
  const others = CATS.filter(([c]) => c !== _cat).map(([c, l]) =>
    `<button class="btn btn-secondary btn-sm" ${act('swipeCategory', c)}>${esc(l)}</button>`).join('');
  const here = _history.filter(h => h.cat === _cat);
  const loved = here.filter(h => h.kind === 'loved').map(h => h.card.title).slice(-3);
  return `<div class="swipe-done" role="status">
    <div class="swipe-done-ic">${ICON.star}</div>
    <h2>You've been through everything near your taste in ${esc(label)}.</h2>
    ${here.length ? `<p>${here.length} answered this session${loved.length ? ` · loved ${loved.map(t => `<b>${esc(t)}</b>`).join(', ')}` : ''}.</p>` : ''}
    <div class="row swipe-done-cta">${others}</div></div>`;
}

function _deckMsg(html) {
  const deck = _el('swipe-deck');
  if (!deck) return;
  deck.querySelectorAll('.swipe-card:not(.is-leaving), .swipe-deck-msg').forEach(n => n.remove());
  deck.insertAdjacentHTML('beforeend', `<div class="swipe-deck-msg">${html}</div>`);
  _el('swipe-view')?.classList.add('swipe-is-empty');
  _ambient(null);
}

// Keyed render of the top DECK_DEPTH cards: a card already on screen keeps
// its node, so moving up a slot is a CSS transition rather than a redraw.
function _layout(returnFrom) {
  const deck = _el('swipe-deck');
  if (!deck) return;
  if (!_queue.length) {
    _deckMsg('<p class="loading" role="status">Finding more titles…</p>');
    return;
  }
  deck.querySelector('.swipe-deck-msg')?.remove();
  _el('swipe-view')?.classList.remove('swipe-is-empty');
  const top = _queue.slice(0, DECK_DEPTH);
  const want = new Set(top.map(c => c.doc_id));
  const nodes = new Map();
  for (const n of deck.querySelectorAll('.swipe-card:not(.is-leaving)')) {
    if (want.has(n.dataset.doc)) nodes.set(n.dataset.doc, n);
    else n.remove();
  }
  top.forEach((c, i) => {
    let n = nodes.get(c.doc_id);
    if (!n) {
      n = _cardNode(c);
      if (i === 0 && returnFrom !== undefined) _flyIn(n, returnFrom);
      else n.classList.add('is-new');
      deck.appendChild(n);
    }
    n.dataset.depth = String(i);
    n.inert = i > 0;
    n.setAttribute('aria-hidden', i > 0 ? 'true' : 'false');
  });
  _ambient(top[0]);
  _prefetch();
}

function _prefetch() {
  for (const c of _queue.slice(DECK_DEPTH, DECK_DEPTH + 4)) {
    if (c.poster_url && !c._pre) { c._pre = new Image(); c._pre.src = proxyImg(c.poster_url); }
  }
}

function _ambient(c) {
  const url = c?.poster_url ? proxyImg(c.poster_url) : '';
  if (url === _ambUrl) return;
  _ambUrl = url;
  const layers = document.querySelectorAll('#swipe-view .swipe-ambient-layer');
  if (layers.length < 2) return;
  _ambIdx ^= 1;
  const next = layers[_ambIdx], prev = layers[_ambIdx ^ 1];
  if (url) next.style.setProperty('background-image', `url("${url.replace(/["\\]/g, encodeURIComponent)}")`);
  next.classList.toggle('on', !!url);
  prev.classList.remove('on');
}

function _fmtRuntime(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}h${m ? ` ${m}m` : ''}` : `${m}m`;
}

function _cardNode(c) {
  const id = `swipe-t-${++_uid}`;
  const d = c.details || {};
  const yours = new Set(c.yours || []);
  const tag = (t, cls) => `<span class="badge ${cls} badge-sm${yours.has(t) ? ' is-yours' : ''}"${yours.has(t) ? ' title="Part of your taste"' : ''}>${esc(t)}</span>`;
  const tags = [...(c.genres || []).map(g => tag(g, 'muted')), ...(c.themes || []).map(t => tag(t, 'theme'))].join('');
  const meta = [
    c.year ? esc(String(c.year)) : '',
    d.runtime_min && _cat === 'movie' ? _fmtRuntime(d.runtime_min) : '',
    d.seasons ? `${d.seasons} season${d.seasons === 1 ? '' : 's'}` : '',
    d.episodes ? `${d.episodes} eps` : '',
    d.content_rating ? `<span class="swipe-cr">${esc(d.content_rating)}</span>` : '',
    d.score ? `<span title="Audience score">★ ${esc(String(d.score))}</span>` : '',
    d.rt_score ? `<span title="Rotten Tomatoes">RT ${esc(String(d.rt_score))}%</span>` : '',
  ].filter(Boolean).join('<span class="swipe-dot" aria-hidden="true">·</span>');
  const people = [
    d.director?.length ? `<dt>Director</dt><dd>${esc(d.director.join(', '))}</dd>` : '',
    d.cast?.length ? `<dt>Starring</dt><dd>${esc(d.cast.join(', '))}</dd>` : '',
    d.mood?.length ? `<dt>Mood</dt><dd>${esc(d.mood.join(', '))}</dd>` : '',
  ].join('');
  const why = c.chain_from
    ? `<div class="swipe-ribbon">${ICON.loved}Because you loved <b>${esc(c.chain_from)}</b></div>`
    : c.because?.length
      ? `<div class="swipe-because">Near your taste for ${c.because.map(t => `<b>${esc(t)}</b>`).join(', ')}</div>`
      : '';
  const pct = Math.round(((c.chain_from ? c.likeness : c.fit) || 0) * 100);
  const poster = c.poster_url
    ? `<img class="swipe-poster" src="${esc(proxyImg(c.poster_url))}" alt="" draggable="false">`
    : `<div class="swipe-poster swipe-poster-ph" aria-hidden="true">${esc((c.title || '?').slice(0, 1))}</div>`;
  const n = document.createElement('article');
  n.className = 'swipe-card';
  n.dataset.doc = c.doc_id;
  n.setAttribute('aria-labelledby', id);
  n.innerHTML = `
    <div class="swipe-poster-wrap">${poster}</div>
    <div class="swipe-info">
      ${why}
      <h2 id="${id}" class="swipe-title">${esc(c.title)}</h2>
      ${meta ? `<div class="swipe-meta">${meta}</div>` : ''}
      ${tags ? `<div class="swipe-tags">${tags}</div>` : ''}
      <p class="swipe-synopsis" title="Click to read it all">${esc(c.synopsis || 'No synopsis on file.')}</p>
      ${people ? `<dl class="swipe-people">${people}</dl>` : ''}
      <div class="swipe-fit"><span>${c.chain_from ? `Like ${esc(c.chain_from)}` : 'Taste fit'}</span>
        <span class="swipe-meter"><i></i></span><b>${pct}%</b></div>
    </div>
    ${ORDER.map(k =>
      `<div class="swipe-stamp" data-kind="${k}" data-pos="${REACT[k].dir || 'none'}" aria-hidden="true">${esc(REACT[k].stamp)}</div>`).join('')}`;
  n.querySelector('.swipe-meter i')?.style.setProperty('width', `${pct}%`);
  const img = n.querySelector('img.swipe-poster');
  img?.addEventListener('error', () => img.replaceWith(Object.assign(document.createElement('div'),
    {className: 'swipe-poster swipe-poster-ph', textContent: (c.title || '?').slice(0, 1)})));
  n.querySelector('.swipe-synopsis')?.addEventListener('click', e => {
    if (!n._moved) e.currentTarget.classList.toggle('open');
  });
  _bindDrag(n);
  return n;
}

// ── reactions ──────────────────────────────────────────────────────────────
export function swipeReact(kind) {
  if (!REACT[kind] || !_queue.length) return;
  return _submit(!UNSEEN.has(kind), REACT[kind].stars, kind);
}

export function swipeStars(n) {
  if (!_queue.length) return;
  const stars = n == null ? null : Math.max(1, Math.min(10, Number(n)));
  return _submit(true, stars, reactionOf(true, stars));
}

export function swipeExact() {
  if (_exactOpen) { _closeExact(); return; }
  if (!_queue.length) return;
  const el = _el('swipe-exact');
  if (!el) return;
  _exactOpen = true;
  el.innerHTML = `
    <div class="swipe-exact-h">Seen it - how good, out of 10?</div>
    <div class="swipe-exact-row" role="group" aria-label="Stars out of 10">
      ${Array.from({length: 10}, (_, i) =>
        `<button class="swipe-star" data-kind="${reactionOf(true, i + 1)}" ${act('swipeStars', i + 1)}
           aria-label="${i + 1} of 10" aria-keyshortcuts="${(i + 1) % 10}">${i + 1}</button>`).join('')}
    </div>
    <div class="row mt-8"><button class="btn btn-ghost btn-sm" ${act('swipeStars', null)}>Seen, no rating (Enter)</button>
      <button class="btn btn-ghost btn-sm ml-auto" ${act('swipeExact')}>Close (Esc)</button></div>`;
  el.hidden = false;
  el.querySelector('.swipe-star')?.focus();
}

function _closeExact() {
  _exactOpen = false;
  const el = _el('swipe-exact');
  if (el) { el.hidden = true; el.innerHTML = ''; }
}

function _flash(kind) {
  const b = _el('swipe-reactions')?.querySelector(`[data-kind="${kind}"]`);
  if (!b) return;
  b.classList.remove('is-hit');
  void b.offsetWidth;            // restart the pulse
  b.classList.add('is-hit');
}

async function _submit(seen, stars, kind, gesture) {
  const c = _queue[0];
  if (!c) return;
  const cat = _cat, gen = _gen;
  _closeExact();
  haptic(BUZZ[kind] ?? 10);
  _flyOut(_top(), kind, gesture);
  _queue.shift();
  _layout();
  _flash(kind);
  const want = kind === 'want';
  const entry = {card: c, cat, kind, seen, stars, want};
  _history.push(entry);
  _renderRecent();
  _renderSession();
  _say(`${kind === 'seen' ? 'Seen' : REACT[kind].label}${seen && stars != null ? `, ${stars} of 10` : ''}: ${c.title}`);
  // refill two cards early: a stack of eight with posters takes a moment
  if (_queue.length <= DECK_DEPTH + 2) _fill();
  let r;
  try {
    r = await api('/api/swipe/answer', 'POST',
                  {category: cat, doc_id: c.doc_id, title: c.title, seen, stars, want, year: c.year || null});
  } catch (e) {
    _history = _history.filter(h => h !== entry);
    if (gen === _gen) {
      _queue.unshift(c);          // not saved: the card comes back
      _layout(REACT[kind]?.dir ?? null);
      _renderRecent();
      _renderSession();
    }
    toast(`Couldn't save that answer - ${_errMsg(e)}`, 'error');
    return;
  }
  if (gen !== _gen) return;
  _progress = {since: r.since_rebuild ?? _progress.since + 1, every: r.rebuild_every || _progress.every};
  if (r.taste_rebuild) {
    _renderProgress(true);
    _say('Your answers are being folded into your taste profile.');
    setTimeout(() => { if (gen === _gen) _renderProgress(); }, 4000);
  } else {
    _renderProgress();
  }
  if (r.watchlist?.ok) {
    _note(`${ICON.want}<span><b>${esc(c.title)}</b> is on your Plex watchlist</span>`);
  } else if (r.watchlist && !_watchlistWarned) {
    _watchlistWarned = true;
    toast(`Saved as Want to watch, but not added to your Plex watchlist - ${r.watchlist.error || 'no match on plex.tv'}`, 'info');
  } else if (r.watchlist) {
    _say(`Saved as Want to watch; ${c.title} is not on your Plex watchlist.`);
  }
  if (kind === 'loved') _chain(c, gen);
}

// A loved title pulls a few of its nearest neighbours to the front of the
// stack (after the card already showing, so nothing jumps under the pointer).
async function _chain(c, gen) {
  try {
    const r = await api(`/api/swipe/next?category=${encodeURIComponent(_cat)}&n=3&seed=${encodeURIComponent(c.doc_id)}${_skipParam()}`);
    if (gen !== _gen) return;
    const added = _add(r.cards, Math.min(1, _queue.length));
    if (!added.length) return;
    _layout();
    _note(`${ICON.loved}<span>${added.length} more like <b>${esc(c.title)}</b> coming up</span>`);
    _say(`${added.length} more titles like ${c.title} added next.`);
  } catch (e) {
    console.debug('[swipe] chain failed', e);
  }
}

export async function swipeUndo() {
  if (_undoing) return;
  _undoing = true;
  const gen = _gen, cat = _cat;
  try {
    const r = await api(`/api/swipe/undo?category=${encodeURIComponent(cat)}`, 'POST');
    if (gen !== _gen) return;
    const u = r.undone || {};
    let i = _history.length - 1;
    while (i >= 0 && !(_history[i].cat === cat && _history[i].card.doc_id === u.doc_id)) i--;
    _closeExact();
    if (i >= 0) {
      const h = _history.splice(i, 1)[0];
      _queue = _queue.filter(q => q.doc_id !== h.card.doc_id);
      _queue.unshift(h.card);
      _layout(REACT[h.kind]?.dir ?? null);
    } else {
      _queue = [];
      _deckMsg('<p class="loading" role="status">Bringing it back…</p>');
      await _fill();
    }
    _renderRecent();
    _renderSession();
    // the plex.tv watchlist entry stays: it may have been there before
    const kept = u.want ? ' - it stays on your Plex watchlist' : '';
    _say(`Took back your answer for ${u.title || 'that title'}${kept}.`);
    _note(`<span>Took back <b>${esc(u.title || 'that title')}</b>${esc(kept)}</span>`);
  } catch (e) {
    toast(e.message && e.message.includes('Nothing to undo') ? 'Nothing to undo here.' : `Couldn't undo - ${_errMsg(e)}`,
          'info');
  } finally {
    _undoing = false;
  }
}

function _onKey(e) {
  if (!_active() || e.altKey || e.ctrlKey || e.metaKey || e.repeat || activeDialog()) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
  const k = e.key.toLowerCase();
  const onButton = t && t.tagName === 'BUTTON';
  if (/^[0-9]$/.test(k)) { e.preventDefault(); swipeStars(k === '0' ? 10 : Number(k)); return; }
  if (k === 'escape' && _exactOpen) { e.preventDefault(); _closeExact(); return; }
  if (k === 'enter' && _exactOpen && !onButton) { e.preventDefault(); swipeStars(null); return; }
  if (k === 's') { e.preventDefault(); swipeExact(); return; }
  if (k === 'u' || (k === 'backspace' && !onButton)) { e.preventDefault(); swipeUndo(); return; }
  if (KEY_REACT[k]) { e.preventDefault(); swipeReact(KEY_REACT[k]); }
}

// ── motion ─────────────────────────────────────────────────────────────────
function _dirOf(dx, dy) {
  if (Math.hypot(dx, dy) < 12) return null;
  if (Math.abs(dy) > Math.abs(dx) * 1.15) return dy < 0 ? 'up' : 'down';
  return dx > 0 ? 'right' : 'left';
}

function _along(dir, x, y) {
  return dir === 'left' ? -x : dir === 'right' ? x : dir === 'up' ? -y : dir === 'down' ? y : 0;
}

// Stamp, glow and the matching reaction button grow with p (0..1).
function _lean(node, kind, p) {
  node.dataset.lean = kind || '';
  node.style.setProperty('--lean-a', String(kind ? p : 0));
  for (const s of node.querySelectorAll('.swipe-stamp')) {
    s.style.setProperty('opacity', s.dataset.kind === kind ? String(p) : '0');
  }
  for (const b of _el('swipe-reactions')?.querySelectorAll('.swipe-react') || []) {
    b.classList.toggle('is-leaning', !!kind && b.dataset.kind === kind && p > 0.15);
  }
}

function _bindDrag(node) {
  let st = null;
  node.addEventListener('pointerdown', e => {
    if (e.button !== 0 || node.dataset.depth !== '0' || node.classList.contains('is-leaving')) return;
    if (e.target.closest('button, a, .swipe-synopsis.open')) return;
    const r = node.getBoundingClientRect();
    // grabbed below the middle, the card tilts the other way (a hand on a card)
    st = {id: e.pointerId, x0: e.clientX, y0: e.clientY, dx: 0, dy: 0, armed: false,
          sign: e.clientY > r.top + r.height / 2 ? -1 : 1, pts: [[e.clientX, e.clientY, e.timeStamp]]};
    node._moved = false;
    node.setPointerCapture(e.pointerId);
    node.classList.add('dragging');
  });
  node.addEventListener('pointermove', e => {
    if (!st || e.pointerId !== st.id) return;
    st.dx = e.clientX - st.x0;
    st.dy = e.clientY - st.y0;
    if (Math.hypot(st.dx, st.dy) > 6) node._moved = true;
    st.pts.push([e.clientX, e.clientY, e.timeStamp]);
    if (st.pts.length > 6) st.pts.shift();
    node.style.transform = `translate(${st.dx}px, ${st.dy}px) rotate(${st.dx * 0.05 * st.sign}deg)`;
    const dir = _dirOf(st.dx, st.dy);
    const p = Math.min(1, _along(dir, st.dx, st.dy) / DRAG_DECIDE_PX);
    _lean(node, dir ? DIR_REACT[dir] : null, p);
    if (p >= 1 && !st.armed) haptic(8);      // one tick as the card crosses the threshold
    st.armed = p >= 1;
  });
  const end = e => {
    if (!st || e.pointerId !== st.id) return;
    const s = st;
    st = null;
    node.classList.remove('dragging');
    const [x1, y1, t1] = s.pts[0], [x2, y2, t2] = s.pts[s.pts.length - 1];
    const dt = Math.max(1, t2 - t1);
    const vx = (x2 - x1) / dt, vy = (y2 - y1) / dt;
    const dir = e.type === 'pointercancel' ? null : _dirOf(s.dx, s.dy);
    const along = _along(dir, s.dx, s.dy);
    if (dir && (along >= DRAG_DECIDE_PX || (_along(dir, vx, vy) > FLICK_PX_MS && along > FLICK_MIN_PX))) {
      const kind = DIR_REACT[dir];
      _submit(!UNSEEN.has(kind), REACT[kind].stars, kind, {dx: s.dx, dy: s.dy, vx, vy, sign: s.sign});
      return;
    }
    node.classList.add('springing');       // back to the deck with a little overshoot
    node.style.transform = '';
    _lean(node, null, 0);
    setTimeout(() => node.classList.remove('springing'), 420);
  };
  node.addEventListener('pointerup', end);
  node.addEventListener('pointercancel', end);
}

const _VEC = {left: [-1, 0], right: [1, 0], up: [0, -1], down: [0, 1]};

function _flyOut(node, kind, g) {
  if (!node) return;
  const dir = REACT[kind]?.dir ?? null;
  node.classList.remove('dragging', 'springing');
  node.classList.add('is-leaving');
  node.inert = true;
  node.removeAttribute('aria-labelledby');
  if (REACT[kind]) _lean(node, kind, 1);
  else _lean(node, null, 0);     // seen without stars: no stamp says that
  for (const b of _el('swipe-reactions')?.querySelectorAll('.is-leaning') || []) b.classList.remove('is-leaning');
  const x0 = g?.dx || 0, y0 = g?.dy || 0, sign = g?.sign || 1;
  let end;
  if (dir) {
    const [ux, uy] = _VEC[dir];
    const x = x0 + ux * window.innerWidth * 0.85 + (g ? g.vx * 160 : 0);
    const y = y0 + uy * window.innerHeight * 0.85 + (g ? g.vy * 160 : 0);
    end = `translate(${x}px, ${y}px) rotate(${(x * 0.04 * sign).toFixed(1)}deg)`;
  } else {
    end = 'translateY(30px) scale(.88)';     // Meh / exact stars: the card sinks away
  }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    node.style.transform = end;
    node.style.opacity = '0';
  }));
  setTimeout(() => node.remove(), 520);
}

// An undone (or unsaved) card flies back in from the side it left on.
function _flyIn(node, dir) {
  const [ux, uy] = _VEC[dir] || [0, 0];
  node.classList.add('no-anim');
  node.style.transform = dir
    ? `translate(${ux * window.innerWidth * 0.7}px, ${uy * window.innerHeight * 0.7}px) rotate(${ux * 18}deg)`
    : 'translateY(30px) scale(.88)';
  node.style.opacity = '0';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    node.classList.remove('no-anim');
    node.style.transform = '';
    node.style.opacity = '';
  }));
}
