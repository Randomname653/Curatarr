// ── LIBRARIES ─────────────────────────────────────────────────────────────────
// Moved state.discoverSections to state
// noAutoReplace: each section's <select> can carry a user's in-progress,
// unsaved category choice (read directly from the DOM by saveLibraries(),
// never mirrored back into any JSON) -- a background revalidation that
// found the fetched payload changed (very plausible: enriched_count/
// scanned_at/updated_at move on their own as background enrichment runs)
// must never blindly overwrite this markup and discard that choice. The
// cache still refreshes in the background so the NEXT full visit starts
// from fresh data; saveLibraries() invalidates explicitly on success since
// this view doesn't otherwise self-reload after a save.
import { state } from './state.js';
import { SVG_CHECK, _errHtml, _errMsg, _fmtRel, btnBusy, btnDone, emptyHtml, esc, escAttr, toast } from './ui.js';
import { _swrCache, _swrInvalidate, _swrRun, api } from './api.js';
export function _renderLibraryConfig([cfg, disc]) {
  const el=document.getElementById('lib-config-content');
  state.discoverSections=disc.sections||[];
  const cfgMap=Object.fromEntries((cfg.libraries||[]).map(l=>[l.key,l.category]));

  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return d.toLocaleDateString(undefined,{day:'2-digit',month:'short',year:'numeric'})
           + ' ' + d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
  }
  el.innerHTML = state.discoverSections.map(s => {
      const cat = cfgMap[s.key] || s.suggested_category;
      const enrichPct = s.plex_item_count > 0
        ? Math.min(100, Math.round(100 * s.enriched_count / Math.max(s.plex_item_count, 1))) : 0;
      const paths = (s.locations||[]).map(esc).join('<br>');
      return `
      <div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title" style="font-size:15px">${esc(s.title)}<span class="badge muted badge-sm">${esc(s.type)}</span></div>
          <div class="panel-actions">
            <label for="libcat-${s.key}" class="fs-11 t3">Category</label>
            <select id="libcat-${s.key}" class="input" aria-label="Select category for library">
              ${['music','movie','show','anime','ignore'].map(o=>`<option value="${o}" ${cat===o?'selected':''}>${o}</option>`).join('')}
            </select>
          </div>
        </div>
        ${paths ? `<div class="fs-11 t3 mono mt-4">${paths}</div>` : ''}
        <div class="panel-item-meta row mt-8" style="gap:16px">
          <span title="Items in Plex">${s.plex_item_count.toLocaleString()} items in Plex</span>
          <span title="Playback events in Curatarr DB">${s.db_entry_count.toLocaleString()} plays tracked</span>
          <span title="Enriched with metadata">${s.enriched_count.toLocaleString()} enriched</span>
          <span>last scan <span class="t2" title="${escAttr(fmtDate(s.scanned_at))}">${s.scanned_at ? _fmtRel(s.scanned_at) : '—'}</span> · last update <span class="t2" title="${escAttr(fmtDate(s.updated_at))}">${s.updated_at ? _fmtRel(s.updated_at) : '—'}</span></span>
        </div>
        ${s.plex_item_count > 0 ? `
        <div class="panel-item-foot" style="max-width:260px">
          <div class="fs-11 t3 mb-4">Enrichment ${enrichPct}%</div>
          <div class="progress-bar" style="margin-top:0;height:4px"><div class="progress-fill" style="width:${Math.min(enrichPct,100)}%"></div></div>
        </div>` : ''}
      </div>`;
    }).join('') || emptyHtml('No Plex libraries found.');
  // Save enables on the first changed category and disables again after a save.
  const save = document.getElementById('lib-save-btn');
  if (save) { save.disabled = true; el.onchange = () => { save.disabled = false; }; }
}

export async function loadLibraryConfig() {
  const el=document.getElementById('lib-config-content');
  if (!_swrCache.has('libraries')) el.innerHTML='<p class="loading" role="status" aria-live="polite">Fetching library info from Plex…</p>';
  try {
    await _swrRun('libraries', () => Promise.all([
      api('/api/libraries/config'),
      api('/api/libraries/discover').catch(()=>({sections:[]}))
    ]), _renderLibraryConfig, { noAutoReplace: true });
  } catch(e){el.innerHTML=_errHtml(e, 'loadLibraryConfig()');}
}

export async function saveLibraries(btn) {
  const libraries=state.discoverSections.map(s=>({
    key:s.key, title:s.title, plex_type:s.type,
    category:document.getElementById(`libcat-${s.key}`)?.value||s.suggested_category
  }));
  btnBusy(btn, 'Saving…');
  try {
    await api('/api/libraries/configure','POST',{libraries});
    _swrInvalidate('libraries'); // this view doesn't self-reload after a save -- without this the next visit would show stale pre-save data
    toast('Library configuration saved', 'success');
    btnDone(btn, null, {keepDisabled: true});
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// ── ORPHAN REPAIR ─────────────────────────────────────────────────────────────
export async function checkOrphans(btn) {
  const el = document.getElementById('orphan-content');
  btnBusy(btn, 'Scanning…');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Scanning Plex history for orphaned entries…</p>';
  try {
    const r = await api('/api/libraries/orphaned');
    if (!r.has_orphans) {
      el.innerHTML = emptyHtml('No orphaned entries found.', null, null, {good: true});
      return;
    }
    const sections = r.orphaned;
    el.innerHTML = `
      <div class="banner warn"><div class="banner-text">Found ${sections.reduce((s,x)=>s+x.count,0)} entries from ${sections.length} removed libraries. Assign each to a category to import them into your taste profile.</div></div>
      <div class="tbl-wrap mb-12">
        <table class="tbl">
          <thead><tr><th>Section ID</th><th>Entries</th><th>Sample titles</th><th>Assign to</th></tr></thead>
          <tbody>
          ${sections.map(s=>`<tr>
            <td class="mono t3">${esc(s.section_id)}</td>
            <td>${s.count}</td>
            <td class="fs-12 t2">${s.sample_titles.slice(0,4).map(esc).join(' · ')}</td>
            <td><select id="orphan-cat-${s.section_id}" class="input" aria-label="Assign orphaned entries to category">
              ${['anime','music','movie','show','ignore'].map(o=>`<option value="${o}" ${o===s.suggested_category?'selected':''}>${o}</option>`).join('')}
            </select></td>
          </tr>`).join('')}
          </tbody>
        </table>
      </div>
      <div class="row"><button type="button" class="btn btn-primary" onclick="onApplyOrphanRepair(this)" data-sections="${escAttr(JSON.stringify(sections))}">Import missing entries</button></div>`;
  } catch(e) {
    el.innerHTML = _errHtml(e);
  } finally {
    btnDone(btn);
  }
}

export async function applyOrphanRepair(sections) {
  const el = document.getElementById('orphan-content');
  const mappings = sections.map(s => ({
    section_id: s.section_id,
    title: `Recovered Section ${s.section_id}`,
    plex_type: Object.keys(s.plex_types)[0] === 'track' ? 'artist' : 'show',
    category: document.getElementById(`orphan-cat-${s.section_id}`)?.value || s.suggested_category,
  }));
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Importing entries… this may take a moment.</p>';
  try {
    const r = await api('/api/libraries/repair-orphans', 'POST', {mappings});
    el.innerHTML = `<div class="banner ok"><span class="banner-icon">${SVG_CHECK}</span><div class="banner-text">Saved ${r.saved} library mappings · imported ${r.synced} new history entries. Run Force sync to recompute your taste profile with the new data.</div></div>`;
    toast(`Imported ${r.synced} history entries`, 'success');
    _swrInvalidate('libraries');
    _swrInvalidate('history-stats');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}
