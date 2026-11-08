// ── SHOKO: plays the watched sync could not place, and the owner's pins ──────
// Settings -> Integrations, under the Shoko card (services/shoko_pins). A pin
// is per series and season as the watch history spells them, from an episode
// on: Link names the AniDB entry (the pin's first episode = its episode 1),
// Skip stops the retries. AniDB splits a season into cours, so one season can
// carry two links (episodes 1-11 -> part 1, from episode 12 -> part 2).
import { EL, _errHtml, _errMsg, act, actOn, btnBusy, btnDone, closeModal, emptyHtml, esc, loc, openModal, toast } from './ui.js';
import { api } from './api.js';

let _groups = [];
let _pinTarget = null;     // the group the search dialog links

function _seasonLabel(s) { return s === null || s === undefined ? 'no season' : `season ${s}`; }

function _when(iso) { return iso ? new Date(iso).toLocaleDateString(loc()) : ''; }

function _pinLine(p) {
  const from = p.from_episode > 1 ? `from episode ${p.from_episode}: ` : '';
  const what = p.action === 'skip' ? 'skipped (not in Shoko)'
    : `linked to ${esc(p.shoko_title || 'AniDB ' + p.anidb_id)} (AniDB ${esc(String(p.anidb_id))})`;
  return `<div class="row panel-item-sub fs-12 t3"><span class="grow">${esc(from)}${what}</span>
    <button type="button" class="btn btn-secondary btn-sm" ${act('shokoUnpin', p.id, EL)}>Unpin</button></div>`;
}

function _row(g, i) {
  const pins = g.pins || [];
  const badge = !pins.length ? '<span class="badge muted badge-sm">No match</span>'
    : pins.every(p => p.action === 'skip') ? '<span class="badge muted badge-sm">Skipped</span>'
    : '<span class="badge amber badge-sm">Linked</span>';
  const meta = `${esc(_seasonLabel(g.season))} · ${g.plays} play${g.plays === 1 ? '' : 's'}${g.last_watched ? ' · last ' + esc(_when(g.last_watched)) : ''}`;
  // Linked plays that are still listed fall outside every pinned entry
  // (a cour the pins do not cover yet): Link... adds the next one.
  const skip = pins.length ? '' : `<button type="button" class="btn btn-secondary btn-sm" ${act('shokoSkip', i, EL)} title="Not in Shoko (not on AniDB): stop retrying these plays">Skip</button>`;
  return `<div class="panel-item">
    <div class="panel-item-head">
      <div class="panel-item-title">${esc(g.series)} ${badge}<span class="panel-item-meta">${meta}</span></div>
      <div class="panel-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('shokoOpenLink', i)}>Link…</button>${skip}</div>
    </div>${pins.map(_pinLine).join('')}
  </div>`;
}

// The section under the Shoko card; loadIntegrations adds the shell.
export async function loadShokoUnmatched() {
  const box = document.getElementById('shoko-unmatched');
  if (!box) return;
  box.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  let r;
  try { r = await api('/api/shoko/unmatched'); }
  catch (e) { box.innerHTML = _errHtml(e, act('loadShokoUnmatched')); return; }
  _groups = r.groups || [];
  const shown = new Set(_groups.flatMap(g => (g.pins || []).map(p => p.id)));
  const others = (r.pins || []).filter(p => !shown.has(p.id));
  const more = r.total_groups > _groups.length ? `<p class="fs-12 t3">${r.total_groups - _groups.length} more not shown.</p>` : '';
  const otherRows = others.map(p => `<div class="panel-item">
    <div class="panel-item-head"><div class="panel-item-title">${esc(p.series)}<span class="panel-item-meta">${esc(_seasonLabel(p.season))}</span></div></div>
    ${_pinLine(p)}</div>`).join('');
  box.innerHTML = `
    <div class="section-head"><h3>Without a Shoko episode</h3><span class="badge muted badge-sm">${r.total_groups || 0}</span>
      <span class="section-hint">Plays the sync could not place on its own. Link a series and season to its AniDB entry, or skip what is not on AniDB. A pin wins over the automatic match; the next sync run tries those plays again.</span></div>
    <div class="section-body">
      ${_groups.length ? _groups.map(_row).join('') + more
                       : emptyHtml('Every play the sync has tried found its Shoko episode.', null, null, {good: true})}
      ${otherRows ? `<h4 class="mt-12 fs-12 t2">Pins whose plays all reached Shoko</h4>${otherRows}` : ''}
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
    title: `Link ${g.series} (${_seasonLabel(g.season)})`, size: 'wide',
    body: `<p class="fs-12 t3 mb-8">Pick the AniDB entry these plays belong to. AniDB keeps one entry per season, and often one per half-season (cour): link the first part from episode 1, then the second from the episode it starts at.</p>
      <div class="row"><label for="shoko-from" class="fs-12">Starts at the season's episode</label>
        <input id="shoko-from" class="input w-auto" type="number" min="1" value="${next}"></div>
      <div class="row mt-8"><input id="shoko-q" class="grow" type="search" value="${esc(g.series)}" aria-label="Search your Shoko collection" ${actOn('keydown', 'shokoSearchOnEnter', '$event')}>
        <button type="button" class="btn btn-secondary btn-sm" ${act('shokoSearch', EL)}>Search</button></div>
      <div id="shoko-results" class="mt-8" role="status" aria-live="polite"></div>`,
  });
  shokoSearch();
}

export function shokoSearchOnEnter(event) { if (event.key === 'Enter') shokoSearch(); }

export async function shokoSearch(btn) {
  const q = document.getElementById('shoko-q')?.value?.trim();
  const out = document.getElementById('shoko-results');
  if (!q || !out) return;
  if (btn) btnBusy(btn, 'Searching…');
  out.innerHTML = '<p class="loading">Searching your Shoko collection…</p>';
  try {
    const r = await api(`/api/shoko/search?q=${encodeURIComponent(q)}`);
    const list = r.results || [];
    out.innerHTML = list.length ? list.map(a => `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(a.title)} <span class="badge muted badge-sm">${esc(a.type || '?')}</span>
            <span class="panel-item-meta">${a.air_date ? esc(String(a.air_date).slice(0, 4)) + ' · ' : ''}${esc(String(a.episodes ?? '?'))} episodes · AniDB ${esc(String(a.anidb_id))}</span></div>
          <div class="panel-actions"><button type="button" class="btn btn-primary btn-sm" ${act('shokoLink', a.anidb_id, EL)}>Link</button></div>
        </div></div>`).join('')
      : emptyHtml('Nothing in your Shoko collection matches. Try the Japanese title or an AniDB id.');
  } catch (e) { out.innerHTML = _errHtml(e); }
  if (btn) btnDone(btn);
}

async function _pin(body, btn) {
  if (btn) btnBusy(btn, 'Saving…');
  try {
    const r = await api('/api/shoko/pins', 'POST', body);
    toast(body.action === 'skip' ? `${body.series}: skipped` : `${body.series} linked to ${r.shoko_title || 'AniDB ' + body.anidb_id}`, 'success');
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
  btnBusy(btn, 'Unpinning…');
  try {
    await api(`/api/shoko/pins/${encodeURIComponent(pinId)}`, 'DELETE');
    toast('Pin lifted: the next sync run tries those plays again', 'success');
    loadShokoUnmatched();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
