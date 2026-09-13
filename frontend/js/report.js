// ── Curation Report (admin) ──────────────────────────────────────────────
import { _swrCache, _swrInvalidate, _swrRun, api } from './api.js';
import { _errHtml, _errMsg, btnBusy, btnDone, emptyHtml, esc, toast } from './ui.js';
export function _repBar(pct, color) {
  return `<div style="background:var(--bg3);border-radius:3px;height:8px;overflow:hidden"><div style="width:${Math.min(100, pct)}%;height:100%;background:${color}"></div></div>`;
}

export async function loadReport() {
  const el = document.getElementById('report-content');
  if (!_swrCache.has('report')) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Crunching the curation ledger…</p>';
  try {
    await _swrRun('report', () => api('/api/stats/curation?months=12'), _renderReport);
  } catch (e) {
    el.innerHTML = _errHtml(e, 'loadReport()');
  }
}

export function _renderReport(d) {
  const el = document.getElementById('report-content');
  const t = d.totals || {};
  const resTotal = (t.consensus || 0) + (t.overrides || 0);
  const maxAct = Math.max(1, ...(d.months || []).map(m => m.deleted + m.kept));
  const maxGb = Math.max(1, ...(d.months || []).map(m => m.gb_freed || 0));

  const tiles = `
    <div class="stat-row" style="flex-wrap:wrap">
      ${[['GB freed', t.gb_freed || 0], ['Deleted', t.deleted || 0],
         ['Kept after debate', t.kept || 0],
         ['Override rate', resTotal ? Math.round(100 * (t.overrides || 0) / resTotal) + '%' : '—'],
         ['Redundant on disk', (d.duplicates?.total_redundant_gb || 0) + ' GB']]
        .map(([l, v]) => `<div class="stat-box" style="min-width:140px;text-align:center"><div class="num">${esc(String(v))}</div><div class="lbl">${esc(l)}</div></div>`).join('')}
    </div>`;

  const monthRows = (d.months || []).map(m => {
    const act = m.deleted + m.kept;
    return `<div class="fs-11 t3" style="display:grid;grid-template-columns:64px 1fr 1fr 90px;gap:10px;align-items:center;margin-bottom:6px">
      <span>${esc(m.month)}</span>
      <div title="${m.deleted} deleted / ${m.kept} kept">${_repBar(100 * act / maxAct, 'var(--amber)')}</div>
      <div title="${m.gb_freed} GB freed">${_repBar(100 * (m.gb_freed || 0) / maxGb, 'var(--success)')}</div>
      <span>${act ? `${m.deleted} deleted · ${m.kept} kept` : ''}${m.gb_freed ? ` · ${m.gb_freed} GB` : ''}</span>
    </div>`;
  }).join('');

  const resSplit = resTotal ? `
    <div class="mb-12">
      <div class="fs-12 t2 mb-8">How debates ended: <b>${t.consensus} consensus</b> · <b class="t-amber">${t.overrides} overrides</b></div>
      <div class="progress-bar" style="margin-top:0;height:10px;display:flex">
        <div style="width:${Math.round(100 * t.consensus / resTotal)}%;background:var(--border2)"></div>
        <div style="flex:1;background:var(--amber)"></div>
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

  const narrative = section('Yearly review', "in the curator's own words",
    `<div id="report-narrative" class="fs-13 t2" style="line-height:1.7">${d.narrative ? esc(d.narrative) : '<span class="t3">Not written yet.</span>'}</div>`,
    `<button type="button" class="btn btn-secondary btn-sm" onclick="writeYearlyReview(this)">${d.narrative ? 'Rewrite' : 'Write'} yearly review</button>`);

  el.innerHTML = tiles + resSplit +
    section('Monthly activity', 'resolutions · GB freed', monthRows || emptyHtml('No resolutions in the last twelve months.')) +
    stubborn + taste + narrative;
}

export async function writeYearlyReview(btn) {
  btnBusy(btn, 'Curator is writing…');
  try {
    const r = await api('/api/stats/curation/narrative', 'POST');
    if (r.ok) {
      document.getElementById('report-narrative').textContent = r.narrative;
      _swrInvalidate('report'); // next visit re-fetches instead of flashing the pre-rewrite text
      toast('Yearly review written', 'success');
    } else {
      toast(r.error || 'Curator unavailable', 'danger');
    }
  } catch (e) {
    toast('Failed: ' + _errMsg(e).slice(0, 160), 'danger');
  }
  btnDone(btn, 'Rewrite yearly review');
}
