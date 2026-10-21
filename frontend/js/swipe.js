// ── Swipe: rate titles near your taste, one card at a time ──────────────────
// Library titles closest to the user's taste vector that they have neither
// played in Plex nor answered yet (services/swipe.py). "Not seen" moves on;
// "Seen" asks for stars 1-10 (or none). Answers feed the taste profile.
// Keyboard: N = not seen, S = seen, 1-9 / 0 (=10) = stars, Enter = seen
// without stars, U = undo. Touch/mouse: drag the card right = seen, left =
// not seen.
import { api } from './api.js';
import { _errHtml, _errMsg, act, emptyHtml, esc, proxyImg, toast } from './ui.js';

const CATS = [['movie', 'Movies'], ['show', 'TV Shows'], ['anime', 'Anime']];
const DRAG_DECIDE_PX = 90;

let _cat = 'show';
let _queue = [];
let _loading = false;
let _rating = false;          // the star row is open for the current card
let _keysBound = false;

function _el(id) { return document.getElementById(id); }
function _active() { return !!_el('swipe-view')?.classList.contains('active'); }

export async function loadSwipe() {
  if (!_keysBound) {
    document.addEventListener('keydown', _onKey);
    _keysBound = true;
  }
  _queue = [];
  _rating = false;
  _renderTabs();
  _loadStats();
  await _fill(true);
}

export function swipeCategory(cat) {
  if (!CATS.some(([c]) => c === cat) || cat === _cat) return;
  _cat = cat;
  loadSwipe();
}

function _renderTabs() {
  const el = _el('swipe-tabs');
  if (!el) return;
  el.innerHTML = CATS.map(([c, label]) =>
    `<button class="cat-tab${c === _cat ? ' active' : ''}" ${act('swipeCategory', c)}
       aria-pressed="${c === _cat}">${esc(label)}</button>`).join('');
}

async function _loadStats() {
  const el = _el('swipe-stats');
  if (!el) return;
  try {
    const r = await api('/api/swipe/stats');
    const s = (r.stats || {})[_cat] || {};
    const total = (s.seen || 0) + (s.not_seen || 0);
    el.textContent = total
      ? `${total} answered here · ${s.seen || 0} seen (${s.rated || 0} rated) · ${s.not_seen || 0} not seen`
      : 'Nothing answered here yet.';
  } catch (e) {
    el.textContent = `Couldn't load your counts — ${_errMsg(e)}`;
  }
}

async function _fill(showLoading) {
  const box = _el('swipe-card');
  if (!box || _loading) return;
  if (showLoading && !_queue.length) {
    box.innerHTML = '<p class="loading" role="status" aria-live="polite">Finding titles near your taste…</p>';
  }
  _loading = true;
  try {
    const r = await api(`/api/swipe/next?category=${encodeURIComponent(_cat)}&n=8`);
    const have = new Set(_queue.map(c => c.doc_id));
    for (const c of r.cards || []) if (!have.has(c.doc_id)) _queue.push(c);
    if (!_queue.length) {
      box.innerHTML = r.reason === 'no_taste'
        ? emptyHtml('No taste profile for this category yet. Watch a few titles in Plex first, then come back.')
        : emptyHtml("You've been through everything near your taste here. Try another category.", '', '', {good: true});
      return;
    }
    if (showLoading || !box.querySelector('.swipe-card')) _render();
  } catch (e) {
    if (!_queue.length) box.innerHTML = _errHtml(e, act('loadSwipe'));
  } finally {
    _loading = false;
  }
}

function _render() {
  const box = _el('swipe-card');
  const c = _queue[0];
  if (!box || !c) return;
  _rating = false;
  const tags = [...(c.genres || []).map(g => `<span class="badge muted badge-sm">${esc(g)}</span>`),
                ...(c.themes || []).map(t => `<span class="badge theme badge-sm">${esc(t)}</span>`)].join('');
  const poster = c.poster_url
    ? `<img class="swipe-poster" src="${esc(proxyImg(c.poster_url))}" alt="">`
    : '<div class="swipe-poster swipe-poster-ph" aria-hidden="true">?</div>';
  box.innerHTML = `
    <article class="swipe-card" aria-labelledby="swipe-title">
      ${poster}
      <div class="swipe-body">
        <h2 id="swipe-title" class="swipe-title">${esc(c.title)}${c.year ? ` <span class="t3">(${esc(String(c.year))})</span>` : ''}</h2>
        <div class="gap-5 row mt-8">${tags}</div>
        <p class="swipe-synopsis">${esc(c.synopsis || 'No synopsis on file.')}</p>
        <div class="fs-11 t3">Taste fit ${Math.round((c.fit || 0) * 100)}%</div>
      </div>
    </article>
    <div class="swipe-actions" id="swipe-actions">
      <button class="btn btn-secondary" ${act('swipeNotSeen')} aria-keyshortcuts="N">Not seen</button>
      <button class="btn btn-primary" ${act('swipeSeen')} aria-keyshortcuts="S">Seen it</button>
    </div>
    <div class="swipe-stars" id="swipe-stars" hidden>
      <div class="fs-12 t2 mb-6">How good was it?</div>
      <div class="row gap-5 swipe-star-row" role="group" aria-label="Stars out of 10">
        ${Array.from({length: 10}, (_, i) =>
          `<button class="btn btn-secondary btn-sm swipe-star" ${act('swipeStars', i + 1)} aria-label="${i + 1} of 10">${i + 1}</button>`).join('')}
      </div>
      <button class="btn btn-ghost btn-sm mt-8" ${act('swipeStars', null)}>Seen, no rating</button>
    </div>
    <div class="row mt-12">
      <button class="btn btn-ghost btn-sm" ${act('swipeUndo')} aria-keyshortcuts="U">Undo last</button>
      <span class="fs-11 t3 swipe-hint">N not seen · S seen · 1-9, 0 = stars · U undo · or drag the card</span>
    </div>`;
  _bindDrag(box.querySelector('.swipe-card'));
}

export function swipeNotSeen() { return _answer(false, null); }

export function swipeSeen() {
  if (!_queue.length) return;
  _rating = true;
  const stars = _el('swipe-stars');
  if (stars) {
    stars.hidden = false;
    stars.querySelector('.swipe-star')?.focus();
  }
}

export function swipeStars(n) { return _answer(true, n); }

async function _answer(seen, stars) {
  const c = _queue.shift();
  if (!c) return;
  _rating = false;
  if (_queue.length) _render();
  else _el('swipe-card').innerHTML = '<p class="loading" role="status" aria-live="polite">Finding more titles…</p>';
  try {
    const r = await api('/api/swipe/answer', 'POST',
                        {category: _cat, doc_id: c.doc_id, title: c.title, seen, stars});
    if (r.taste_rebuild) toast('Your answers are being folded into your taste profile.', 'info');
  } catch (e) {
    _queue.unshift(c);            // not saved: the card comes back
    _render();
    toast(`Couldn't save that answer — ${_errMsg(e)}`, 'error');
    return;
  }
  _loadStats();
  if (_queue.length <= 2) _fill(false);
}

export async function swipeUndo() {
  try {
    const r = await api(`/api/swipe/undo?category=${encodeURIComponent(_cat)}`, 'POST');
    const u = r.undone || {};
    toast(`Took back your answer for "${u.title || 'that title'}".`, 'info');
    _queue = [];
    _loadStats();
    await _fill(true);
  } catch (e) {
    toast(e.message && e.message.includes('Nothing to undo') ? 'Nothing to undo here.' : `Couldn't undo — ${_errMsg(e)}`,
          'info');
  }
}

function _onKey(e) {
  if (!_active() || e.altKey || e.ctrlKey || e.metaKey) return;
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  const k = e.key.toLowerCase();
  if (_rating && /^[0-9]$/.test(k)) { e.preventDefault(); swipeStars(k === '0' ? 10 : Number(k)); return; }
  if (_rating && k === 'enter') { e.preventDefault(); swipeStars(null); return; }
  if (k === 'n') { e.preventDefault(); swipeNotSeen(); }
  else if (k === 's') { e.preventDefault(); swipeSeen(); }
  else if (k === 'u') { e.preventDefault(); swipeUndo(); }
}

// Drag right = seen (opens the stars), drag left = not seen.
function _bindDrag(card) {
  if (!card) return;
  let x0 = null, dx = 0;
  card.addEventListener('pointerdown', e => {
    if (e.button !== 0 || _rating) return;
    x0 = e.clientX; dx = 0;
    card.setPointerCapture(e.pointerId);
    card.classList.add('dragging');
  });
  card.addEventListener('pointermove', e => {
    if (x0 === null) return;
    dx = e.clientX - x0;
    card.style.transform = `translateX(${dx}px) rotate(${dx / 30}deg)`;
    card.classList.toggle('lean-right', dx > DRAG_DECIDE_PX);
    card.classList.toggle('lean-left', dx < -DRAG_DECIDE_PX);
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null;
    card.classList.remove('dragging', 'lean-right', 'lean-left');
    card.style.transform = '';
    if (dx > DRAG_DECIDE_PX) swipeSeen();
    else if (dx < -DRAG_DECIDE_PX) swipeNotSeen();
  };
  card.addEventListener('pointerup', end);
  card.addEventListener('pointercancel', end);
}
