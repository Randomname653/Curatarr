// ── Curation ledger (admin) ─────────────────────────────────────────────
// The old Curation Report, now a section at the bottom of the owner's
// Wrap-Up (wrapup.js): resolutions per month, the debate split, the
// Stubbornness Index, taste evolution. The yearly paragraph is gone — the
// curator writes the whole wrap-up now.
import { _swrCache, _swrRun, api } from './api.js';
import { _errHtml, act, emptyHtml, esc, loc, skeletonHtml } from './ui.js';
import { t, th } from './i18n.js';
// The report's one computed style: a fill's share as --v (0-100 %). The
// class says what it is and which way it grows (.rep-col-* is a column's
// height, .rep-split-* a share of the debate bar); colours live in app.css.
export function _repBar(cls, pct) {
  return `<i class="${cls}" data-style="--v:${Math.max(0, Math.min(100, pct))}%"></i>`;
}
const _monthShort = key => {
  const dt = new Date(`${key}-01T00:00:00`);
  return isNaN(dt.getTime()) ? key : dt.toLocaleString(loc(), {month: 'short'});
};

let _ledgerEl = null;

export async function renderLedger(el) {
  _ledgerEl = el || _ledgerEl;
  if (!_ledgerEl) return;
  if (!_swrCache.has('report')) _ledgerEl.innerHTML = skeletonHtml(t('report.loading'), {kind: 'rows'});
  try {
    await _swrRun('report', () => api('/api/stats/curation?months=12'), _renderReport);
  } catch (e) {
    _ledgerEl.innerHTML = _errHtml(e, act('renderLedger'));
  }
}

export function _renderReport(d) {
  const el = _ledgerEl;
  if (!el || !el.isConnected) return;
  const tot = d.totals || {};
  const resTotal = (tot.consensus || 0) + (tot.overrides || 0);
  const maxAct = Math.max(1, ...(d.months || []).map(m => m.deleted + m.kept));

  const tiles = `
    <div class="flex-wrap stat-row">
      ${[[t('report.gb_freed'), tot.gb_freed || 0], [t('report.deleted'), tot.deleted || 0],
         [t('report.kept'), tot.kept || 0],
         [t('report.override_rate'), resTotal ? t('report.pct', {pct: Math.round(100 * (tot.overrides || 0) / resTotal)}) : '—'],
         [t('report.redundant'), (d.duplicates?.total_redundant_gb || 0) + ' GB']]
        .map(([l, v]) => `<div class="minw-140 t-center stat-box"><div class="num">${esc(String(v))}</div><div class="lbl">${esc(l)}</div></div>`).join('')}
    </div>`;

  // Twelve columns, one per month: deleted (amber) stacked on kept (quiet),
  // the height against the busiest month; GB freed sits above as a number.
  const anyAct = (d.months || []).some(m => m.deleted + m.kept);
  const monthRows = anyAct ? `<div class="rep-chart" role="img" aria-label="${th('report.per_month_aria')}">${(d.months || []).map(m => {
    const act = m.deleted + m.kept;
    return `<div class="rep-col" title="${esc(m.month)}: ${th('report.col_title', {deleted: m.deleted, kept: m.kept})}${m.gb_freed ? ` · ${th('report.col_gb', {gb: m.gb_freed})}` : ''}">
      <span class="rep-col-gb">${m.gb_freed ? `${Math.round(m.gb_freed)} GB` : ''}</span>
      <span class="rep-col-bar">${_repBar('rep-col-kept', 100 * m.kept / maxAct)}${_repBar('rep-col-del', 100 * m.deleted / maxAct)}</span>
      <span class="rep-col-n">${act || ''}</span>
      <span class="rep-col-m">${esc(_monthShort(m.month))}</span>
    </div>`;
  }).join('')}</div>
    <div class="rep-legend"><span class="rep-key is-del"></span>${th('report.deleted')} <span class="rep-key is-kept"></span>${th('report.kept')}</div>` : '';

  const resSplit = resTotal ? `
    <div class="mb-12">
      <div class="fs-12 t2 mb-8">${th('report.debates_ended')} <b>${th('report.consensus', {n: tot.consensus})}</b> · <b class="t-amber">${th('report.overrides', {n: tot.overrides})}</b></div>
      <div class="rep-split">
        ${_repBar('rep-split-consensus', 100 * tot.consensus / resTotal)}
        <i class="rep-split-override"></i>
      </div>
    </div>` : '';

  const section = (title, hint, body, actions) => `<section class="section">
    <div class="section-head"><h3>${title}</h3>${hint ? `<span class="section-hint">${hint}</span>` : ''}${actions ? `<div class="section-actions">${actions}</div>` : ''}</div>
    <div class="section-body">${body}</div></section>`;

  const stubborn = (d.stubbornness || []).length
    ? section(th('report.stubborn'), th('report.stubborn_hint'),
        d.stubbornness.map(s => `<div class="panel-item">
          <div class="panel-item-head"><div class="panel-item-title">${esc(s.title)}
            <span class="badge muted">${esc(s.category || '')}</span>
            <span class="badge amber">${s.days_since_play == null ? th('report.never_played') : th('report.days_untouched', {n: s.days_since_play})}</span></div></div>
          ${s.curator_stance ? `<div class="panel-item-sub">${th('report.standing_objection')} ${esc(s.curator_stance)}</div>` : ''}
        </div>`).join(''))
    : '';

  const taste = (d.taste_evolution || []).length
    ? section(th('report.taste'), th('report.taste_hint'),
        d.taste_evolution.map(f => `<div class="panel-item">
          <div class="panel-item-head"><div class="panel-item-title">${esc(f.title || '')}
            <span class="badge ${f.sentiment === 'positive' ? 'amber' : 'muted'}">${esc(f.sentiment || '')}${f.weight > 1 ? ' ·×' + f.weight : ''}</span>
            <span class="panel-item-meta">${esc((f.date || '').slice(0, 10))} · ${esc(f.category || '')}</span></div></div>
          ${f.reason ? `<div class="panel-item-sub">${esc(f.reason)}</div>` : ''}
        </div>`).join(''))
    : '';

  el.innerHTML = tiles + resSplit +
    section(th('report.monthly'), th('report.monthly_hint'), monthRows || emptyHtml(th('report.no_resolutions'))) +
    stubborn + taste;
}
