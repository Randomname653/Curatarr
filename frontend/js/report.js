// ── Curation ledger (admin) ─────────────────────────────────────────────
// The old Curation Report, now a section at the bottom of the owner's
// Wrap-Up (wrapup.js): resolutions per month, the debate split, the
// Stubbornness Index, taste evolution. The yearly paragraph is gone — the
// curator writes the whole wrap-up now.
import { _swrCache, _swrRun, api } from './api.js';
import { _errHtml, act, emptyHtml, esc } from './ui.js';
// The report's one computed style: a fill's share as --v (0-100 %). The
// class says what it is and which way it grows (.rep-col-* is a column's
// height, .rep-split-* a share of the debate bar); colours live in app.css.
export function _repBar(cls, pct) {
  return `<i class="${cls}" data-style="--v:${Math.max(0, Math.min(100, pct))}%"></i>`;
}
const _monthShort = key => {
  const dt = new Date(`${key}-01T00:00:00`);
  return isNaN(dt.getTime()) ? key : dt.toLocaleString(undefined, {month: 'short'});
};

let _ledgerEl = null;

export async function renderLedger(el) {
  _ledgerEl = el || _ledgerEl;
  if (!_ledgerEl) return;
  if (!_swrCache.has('report')) _ledgerEl.innerHTML = '<p class="loading" role="status" aria-live="polite">Crunching the curation ledger…</p>';
  try {
    await _swrRun('report', () => api('/api/stats/curation?months=12'), _renderReport);
  } catch (e) {
    _ledgerEl.innerHTML = _errHtml(e, act('renderLedger'));
  }
}

export function _renderReport(d) {
  const el = _ledgerEl;
  if (!el || !el.isConnected) return;
  const t = d.totals || {};
  const resTotal = (t.consensus || 0) + (t.overrides || 0);
  const maxAct = Math.max(1, ...(d.months || []).map(m => m.deleted + m.kept));

  const tiles = `
    <div class="flex-wrap stat-row">
      ${[['GB freed', t.gb_freed || 0], ['Deleted', t.deleted || 0],
         ['Kept after debate', t.kept || 0],
         ['Override rate', resTotal ? Math.round(100 * (t.overrides || 0) / resTotal) + '%' : '—'],
         ['Redundant on disk', (d.duplicates?.total_redundant_gb || 0) + ' GB']]
        .map(([l, v]) => `<div class="minw-140 t-center stat-box"><div class="num">${esc(String(v))}</div><div class="lbl">${esc(l)}</div></div>`).join('')}
    </div>`;

  // Twelve columns, one per month: deleted (amber) stacked on kept (quiet),
  // the height against the busiest month; GB freed sits above as a number.
  const anyAct = (d.months || []).some(m => m.deleted + m.kept);
  const monthRows = anyAct ? `<div class="rep-chart" role="img" aria-label="Resolutions per month">${(d.months || []).map(m => {
    const act = m.deleted + m.kept;
    return `<div class="rep-col" title="${esc(m.month)}: ${m.deleted} deleted · ${m.kept} kept${m.gb_freed ? ` · ${m.gb_freed} GB freed` : ''}">
      <span class="rep-col-gb">${m.gb_freed ? `${Math.round(m.gb_freed)} GB` : ''}</span>
      <span class="rep-col-bar">${_repBar('rep-col-kept', 100 * m.kept / maxAct)}${_repBar('rep-col-del', 100 * m.deleted / maxAct)}</span>
      <span class="rep-col-n">${act || ''}</span>
      <span class="rep-col-m">${esc(_monthShort(m.month))}</span>
    </div>`;
  }).join('')}</div>
    <div class="rep-legend"><span class="rep-key is-del"></span>Deleted <span class="rep-key is-kept"></span>Kept after debate</div>` : '';

  const resSplit = resTotal ? `
    <div class="mb-12">
      <div class="fs-12 t2 mb-8">How debates ended: <b>${t.consensus} consensus</b> · <b class="t-amber">${t.overrides} overrides</b></div>
      <div class="rep-split">
        ${_repBar('rep-split-consensus', 100 * t.consensus / resTotal)}
        <i class="rep-split-override"></i>
      </div>
    </div>` : '';

  const section = (title, hint, body, actions) => `<section class="section">
    <div class="section-head"><h3>${title}</h3>${hint ? `<span class="section-hint">${hint}</span>` : ''}${actions ? `<div class="section-actions">${actions}</div>` : ''}</div>
    <div class="section-body">${body}</div></section>`;

  const stubborn = (d.stubbornness || []).length
    ? section('Stubbornness Index', "kept over the curator's objection, untouched 90+ days",
        d.stubbornness.map(s => `<div class="panel-item">
          <div class="panel-item-head"><div class="panel-item-title">${esc(s.title)}
            <span class="badge muted">${esc(s.category || '')}</span>
            <span class="badge amber">${s.days_since_play == null ? 'never played since' : s.days_since_play + ' days untouched'}</span></div></div>
          ${s.curator_stance ? `<div class="panel-item-sub">Curator's standing objection: ${esc(s.curator_stance)}</div>` : ''}
        </div>`).join(''))
    : '';

  const taste = (d.taste_evolution || []).length
    ? section('Taste evolution', 'your latest recorded verdicts',
        d.taste_evolution.map(f => `<div class="panel-item">
          <div class="panel-item-head"><div class="panel-item-title">${esc(f.title || '')}
            <span class="badge ${f.sentiment === 'positive' ? 'amber' : 'muted'}">${esc(f.sentiment || '')}${f.weight > 1 ? ' ·×' + f.weight : ''}</span>
            <span class="panel-item-meta">${esc((f.date || '').slice(0, 10))} · ${esc(f.category || '')}</span></div></div>
          ${f.reason ? `<div class="panel-item-sub">${esc(f.reason)}</div>` : ''}
        </div>`).join(''))
    : '';

  el.innerHTML = tiles + resSplit +
    section('Monthly activity', 'resolutions · GB freed', monthRows || emptyHtml('No resolutions in the last twelve months.')) +
    stubborn + taste;
}
