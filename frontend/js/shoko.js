// ── SHOKO: plays the watched sync could not place, and the owner's pins ──────
// Settings -> Integrations, under the Shoko card (services/shoko_pins). A pin
// is per series and season as the watch history spells them, from an episode
// on: Link names the AniDB entry (the pin's first episode = its episode 1),
// Skip stops the retries. AniDB splits a season into cours, so one season can
// carry two links (episodes 1-11 -> part 1, from episode 12 -> part 2).
import { EL, _errHtml, _errMsg, act, actOn, btnBusy, btnDone, closeModal, emptyHtml, esc, loc, openModal, toast, loadingHtml } from './ui.js';
import { api } from './api.js';
import { t, th } from './i18n.js';

let _groups = [];
let _pinTarget = null;     // the group the search dialog links

function _seasonLabel(s) { return s === null || s === undefined ? t('shoko.no_season') : t('shoko.season', {n: s}); }

function _when(iso) { return iso ? new Date(iso).toLocaleDateString(loc()) : ''; }

function _pinLine(p) {
  const from = p.from_episode > 1 ? `${t('shoko.from_episode', {n: p.from_episode})} ` : '';
  const what = p.action === 'skip' ? th('shoko.skipped_not_in')
    : th('shoko.linked_to', {title: p.shoko_title || 'AniDB ' + p.anidb_id, id: String(p.anidb_id)});
  return `<div class="row panel-item-sub fs-12 t3"><span class="grow">${esc(from)}${what}</span>
    <button type="button" class="btn btn-secondary btn-sm" ${act('shokoUnpin', p.id, EL)}>${th('shoko.unpin')}</button></div>`;
}

function _row(g, i) {
  const pins = g.pins || [];
  const badge = !pins.length ? `<span class="badge muted badge-sm">${th('shoko.no_match')}</span>`
    : pins.every(p => p.action === 'skip') ? `<span class="badge muted badge-sm">${th('shoko.skipped')}</span>`
    : `<span class="badge amber badge-sm">${th('shoko.linked')}</span>`;
  const meta = `${esc(_seasonLabel(g.season))} · ${th('shoko.plays', {n: g.plays})}${g.last_watched ? ' · ' + th('shoko.last', {date: _when(g.last_watched)}) : ''}`;
  // Linked plays that are still listed fall outside every pinned entry
  // (a cour the pins do not cover yet): Link... adds the next one.
  const skip = pins.length ? '' : `<button type="button" class="btn btn-secondary btn-sm" ${act('shokoSkip', i, EL)} title="${th('shoko.skip_title')}">${th('shoko.skip')}</button>`;
  return `<div class="panel-item">
    <div class="panel-item-head">
      <div class="panel-item-title">${esc(g.series)} ${badge}<span class="panel-item-meta">${meta}</span></div>
      <div class="panel-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('shokoOpenLink', i)}>${th('shoko.link_more')}</button>${skip}</div>
    </div>${pins.map(_pinLine).join('')}
  </div>`;
}

// The section under the Shoko card; loadIntegrations adds the shell.
export async function loadShokoUnmatched() {
  const box = document.getElementById('shoko-unmatched');
  if (!box) return;
  box.innerHTML = loadingHtml(t('common.loading'));
  let r;
  try { r = await api('/api/shoko/unmatched'); }
  catch (e) { box.innerHTML = _errHtml(e, act('loadShokoUnmatched')); return; }
  _groups = r.groups || [];
  const shown = new Set(_groups.flatMap(g => (g.pins || []).map(p => p.id)));
  const others = (r.pins || []).filter(p => !shown.has(p.id));
  const more = r.total_groups > _groups.length ? `<p class="fs-12 t3">${th('shoko.more_not_shown', {n: r.total_groups - _groups.length})}</p>` : '';
  const otherRows = others.map(p => `<div class="panel-item">
    <div class="panel-item-head"><div class="panel-item-title">${esc(p.series)}<span class="panel-item-meta">${esc(_seasonLabel(p.season))}</span></div></div>
    ${_pinLine(p)}</div>`).join('');
  box.innerHTML = `
    <div class="section-head"><h3>${th('shoko.without_episode')}</h3><span class="badge muted badge-sm">${r.total_groups || 0}</span>
      <span class="section-hint">${th('shoko.without_episode_hint')}</span></div>
    <div class="section-body">
      ${_groups.length ? _groups.map(_row).join('') + more
                       : emptyHtml(th('shoko.all_found'), null, null, {good: true})}
      ${otherRows ? `<h4 class="mt-12 fs-12 t2">${th('shoko.pins_done')}</h4>${otherRows}` : ''}
    </div>`;
}

export function shokoOpenLink(i) {
  const g = _groups[i];
  if (!g) return;
  _pinTarget = g;
  // A season already linked from episode 1 most likely needs its next cour.
  const starts = (g.pins || []).map(p => p.from_episode);
  const next = starts.length ? Math.max(...starts) + 1 : 1;
  openModal({
    title: t('shoko.link_title', {series: g.series, season: _seasonLabel(g.season)}), size: 'wide',
    body: `<p class="fs-12 t3 mb-8">${th('shoko.link_hint')}</p>
      <div class="row"><label for="shoko-from" class="fs-12">${th('shoko.starts_at')}</label>
        <input id="shoko-from" class="input w-auto" type="number" min="1" value="${next}"></div>
      <div class="row mt-8"><input id="shoko-q" class="grow" type="search" value="${esc(g.series)}" aria-label="${th('shoko.search_aria')}" ${actOn('keydown', 'shokoSearchOnEnter', '$event')}>
        <button type="button" class="btn btn-secondary btn-sm" ${act('shokoSearch', EL)}>${th('shoko.search')}</button></div>
      <div id="shoko-results" class="mt-8" role="status" aria-live="polite"></div>`,
  });
  shokoSearch();
}

export function shokoSearchOnEnter(event) { if (event.key === 'Enter') shokoSearch(); }

export async function shokoSearch(btn) {
  const q = document.getElementById('shoko-q')?.value?.trim();
  const out = document.getElementById('shoko-results');
  if (!q || !out) return;
  if (btn) btnBusy(btn, t('common.searching'));
  out.innerHTML = loadingHtml(t('shoko.searching'));
  try {
    const r = await api(`/api/shoko/search?q=${encodeURIComponent(q)}`);
    const list = r.results || [];
    out.innerHTML = list.length ? list.map(a => `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(a.title)} <span class="badge muted badge-sm">${esc(a.type || '?')}</span>
            <span class="panel-item-meta">${a.air_date ? esc(String(a.air_date).slice(0, 4)) + ' · ' : ''}${th('shoko.episodes', {x: String(a.episodes ?? '?')})} · AniDB ${esc(String(a.anidb_id))}</span></div>
          <div class="panel-actions"><button type="button" class="btn btn-primary btn-sm" ${act('shokoLink', a.anidb_id, EL)}>${th('shoko.link')}</button></div>
        </div></div>`).join('')
      : emptyHtml(th('shoko.nothing_matches'));
  } catch (e) { out.innerHTML = _errHtml(e); }
  if (btn) btnDone(btn);
}

async function _pin(body, btn) {
  if (btn) btnBusy(btn, t('shoko.saving'));
  try {
    const r = await api('/api/shoko/pins', 'POST', body);
    toast(body.action === 'skip' ? t('shoko.skipped_toast', {series: body.series}) : t('shoko.linked_toast', {series: body.series, title: r.shoko_title || 'AniDB ' + body.anidb_id}), 'success');
    return true;
  } catch (e) { toast(_errMsg(e), 'danger'); return false; }
  finally { if (btn) btnDone(btn); }
}

export async function shokoLink(anidbId, btn) {
  const g = _pinTarget;
  if (!g) return;
  const from = Math.max(1, parseInt(document.getElementById('shoko-from')?.value, 10) || 1);
  if (await _pin({series: g.series, season: g.season, action: 'link', anidb_id: anidbId, from_episode: from}, btn)) {
    closeModal();
    loadShokoUnmatched();
  }
}

export async function shokoSkip(i, btn) {
  const g = _groups[i];
  if (g && await _pin({series: g.series, season: g.season, action: 'skip'}, btn)) loadShokoUnmatched();
}

export async function shokoUnpin(pinId, btn) {
  btnBusy(btn, t('shoko.unpinning'));
  try {
    await api(`/api/shoko/pins/${encodeURIComponent(pinId)}`, 'DELETE');
    toast(t('shoko.unpinned'), 'success');
    loadShokoUnmatched();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
