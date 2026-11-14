// ── Settings → Integrations: the bench's advice per Ollama host ──────────────
// (GET /api/setup/hosts-advice, services/host_advice.py, 2026-11-02.) The
// models cards showed free-text model names and three numbers with nothing to
// compare them against, while the app knows each card: this server's from
// nvidia-smi, the node's from its agent. Per host: the card, the bench's
// package for it next to what is set (badges where they differ), one button
// that fills the bench's values in, and the model fields as lists of the
// catalog's choices marked installed / too big for that card.
import { api } from './api.js';
import { EL, _errMsg, act, actOn, btnBusy, btnDone, esc, escAttr, setStatus } from './ui.js';
import { t, th } from './i18n.js';
import { renderMinibench } from './setup.js';

// Which form field holds which value, per host card.
export const ADVICE_FIELDS = {
  server: {curator: 'base_curator_model', summarizer: 'base_summarizer_model',
           embedding: 'embedding_model', pitcher: 'base_pitcher_model',
           profile: 'llm_profile', num_ctx: 'curator_num_ctx', num_gpu: 'curator_num_gpu'},
  node: {curator: 'pc_base_curator_model', summarizer: 'pc_base_summarizer_model',
         pitcher: 'pc_base_pitcher_model',
         profile: 'pc_llm_profile', num_ctx: 'pc_curator_num_ctx', num_gpu: 'pc_curator_num_gpu'},
};
const ROLES = ['curator', 'summarizer', 'embedding', 'pitcher'];
const SIZES = [4, 6, 8, 10, 12, 16, 20, 24, 32, 48];
const PROFILE_CTX = {large: 16384, small: 8192};

const _advice = {};   // host -> its part of the last answer
const _picks = {};    // host -> card size the owner picked (when nothing reads it)

const _field = (host, key) => document.getElementById(`int-${ADVICE_FIELDS[host][key]}`);

export async function loadHostAdvice() {
  const hosts = ['server', 'node'].filter(h => document.getElementById(`int-advice-${h}`));
  if (!hosts.length) return;
  const q = new URLSearchParams();
  if (_picks.server) q.set('server_vram_gb', _picks.server);
  if (_picks.node) q.set('node_vram_gb', _picks.node);
  let d;
  try { d = await api(`/api/setup/hosts-advice${q.toString() ? `?${q}` : ''}`); }
  catch (e) {
    for (const h of hosts) {
      document.getElementById(`int-advice-${h}`).innerHTML =
        `<p class="fs-12 t-amber">${th('hostadv.unavailable', {error: _errMsg(e)})}</p>`;
    }
    return;
  }
  for (const h of hosts) {
    const box = document.getElementById(`int-advice-${h}`);
    if (!d[h]) { box.innerHTML = ''; continue; }
    _advice[h] = d[h];
    for (const role of ROLES) _fillSelect(h, role);
    _render(h);
  }
}

// The catalog's choices for one role on that host's card. The value set now
// always stays in the list and selected: the list informs, it never changes
// a setting by itself.
function _fillSelect(host, role) {
  const key = ADVICE_FIELDS[host][role];
  const sel = key && document.getElementById(`int-${key}`);
  const part = _advice[host];
  if (!sel || !part) return;
  const cur = sel.value || part.current[role] || '';
  const entries = part.advice[role] || [];
  const installed = part.advice.installed || [];
  const pick = role === 'curator' ? part.advice.package?.recommended?.model : null;
  const names = entries.map(e => e.model);
  const opts = entries.map(e => {
    const tags = [];
    if (e.model === pick) tags.push(t('hostadv.bench_pick', {gb: part.advice.package.tier_gb}));
    if (e.installed) tags.push(t('hostadv.installed'));
    if (e.fits === false) tags.push(t('hostadv.needs', {gb: Math.round(e.need_gb)}));
    else if (e.tight) tags.push(t('hostadv.tight'));
    return `<option value="${esc(e.model)}">${esc(e.model)} — ${esc(e.label)}${tags.length ? esc(` · ${tags.join(' · ')}`) : ''}</option>`;
  });
  // Curatarr's own bakes are built FROM these bases, never one of them.
  const extra = installed.filter(m => !names.includes(m) && !m.startsWith('curatarr-'))
    .map(m => `<option value="${esc(m)}">${esc(m)} — ${th('hostadv.installed_unbenched')}</option>`);
  if (cur && !names.includes(cur) && !installed.includes(cur)) {
    extra.unshift(`<option value="${esc(cur)}">${esc(cur)} — ${th('hostadv.set_not_here')}</option>`);
  }
  sel.innerHTML = opts.concat(extra).join('');
  sel.value = cur;
}

// The bench's values for a model on this card: its own package when the bench
// measured it there, else none (the recommended model's values do not carry).
function _measured(part, model) {
  const by = part.advice.package?.by_model || {};
  return by[model] || by[String(model).replace(/:latest$/, '')] || null;
}

const _window = (profile, ctx) => Number(ctx) || PROFILE_CTX[profile] || 16384;

// ``passed``: what is set is not the bench's pick but passed on this card
// too (an alternative) - a choice, not a mismatch.
function _row(label, now, bench, same, passed = false) {
  const badge = bench === null ? ''
    : same ? ` <span class="badge success badge-sm">${th('hostadv.as_measured')}</span>`
      : passed ? ` <span class="badge badge-sm" title="${th('hostadv.pick_here', {model: bench})}">${th('hostadv.passed_here')}</span>`
        : ` <span class="badge amber badge-sm">${th('hostadv.bench', {value: bench})}</span>`;
  return `<div class="fs-12">${esc(label)}: <b>${esc(now)}</b>${badge}</div>`;
}

export function _adviceHtml(host, part, form) {
  const card = part.card || {};
  const who = host === 'server' ? t('hostadv.server_card') : t('hostadv.node_card', {name: part.name || t('hostadv.the_node')});
  const size = card.vram_gb ? `${card.vram_gb} GB` : t('hostadv.not_read');
  const src = card.source ? ` (${card.source})` : '';
  const sizes = SIZES.map(g => `<option value="${g}"${Number(card.vram_gb) === g ? ' selected' : ''}>${g} GB</option>`).join('');
  const picker = `<select class="w-auto" id="int-advice-size-${host}" aria-label="${th('hostadv.memory_aria', {who})}" ${actOn('change', 'onAdviceSizeChange', host)}>
      <option value=""${card.vram_gb ? '' : ' selected'}>${th('hostadv.card_memory')}</option>${sizes}</select>`;
  const head = `<div class="row fs-12"><span>${esc(who)}: <b>${esc(card.name ? `${card.name} · ${size}` : size)}</b>${esc(src)}</span>${picker}</div>`;
  const pkg = part.advice.package;
  if (!pkg?.recommended) {
    const why = pkg?.note || t(card.vram_gb ? 'hostadv.no_package' : 'hostadv.pick_memory');
    return `${head}<p class="fs-12 t3 mt-4">${esc(why)}</p>`;
  }
  const rec = pkg.recommended;
  const curator = form.curator || part.current.curator;
  const mine = _measured(part, curator);
  const vals = mine || null;
  const profile = form.profile || part.current.profile;
  const win = _window(profile, form.num_ctx);
  const rows = [
    _row(t('hostadv.curator_model'), curator, rec.model, curator === rec.model,
         (pkg.alternatives || []).some(a => a.model === curator)),
    _row(t('hostadv.profile'), profile, vals ? vals.profile : null, vals && vals.profile === profile),
    _row(t('hostadv.context_window'), String(win), vals ? String(_window(vals.profile, vals.num_ctx)) : null,
         vals && _window(vals.profile, vals.num_ctx) === win),
    _row(t('hostadv.layers'), String(form.num_gpu), vals ? String(vals.num_gpu) : null,
         vals && Number(vals.num_gpu) === Number(form.num_gpu)),
  ].join('');
  const which = t(mine ? 'hostadv.badges_are' : 'hostadv.not_measured', {model: curator, gb: pkg.tier_gb});
  const ex = rec.expected || {};
  const q = rec.quality || {};
  const ran = t('hostadv.ran', {model: rec.model, tok: ex.gen_tok_s ?? '?', judge: ex.judge_s ?? '?'})
    + `${q.score25 != null ? `, ${t('hostadv.quality', {score: q.score25})}` : ''}`
    + `${ex.gpu_share_pct != null && ex.gpu_share_pct < 100 ? `, ${t('hostadv.on_card', {pct: ex.gpu_share_pct})}` : ''}.`;
  const srv = Object.entries(rec.server || {})
    .filter(([k]) => k === 'OLLAMA_KV_CACHE_TYPE' || k === 'OLLAMA_FLASH_ATTENTION')
    .map(([k, v]) => `${k}=${v}`);
  const server = srv.length ? `<div class="fs-12 t3">${th('hostadv.set_on_ollama', {vars: srv.join(' · ')})}</div>` : '';
  return `${head}<div class="mt-8">${rows}</div>
    <div class="fs-12 t3 mt-4">${esc(which)} ${esc(ran)}</div>${server}
    <div class="row mt-8"><button type="button" class="btn btn-secondary btn-sm" ${act('applyBenchValues', host)}>${th('hostadv.use_bench')}</button>
      ${host === 'server' ? `<button type="button" class="btn btn-secondary btn-sm" ${act('runSpeedCheck', EL, host)}>${th('hostadv.speed_check')}</button>` : ''}
      <span id="int-advice-note-${host}" class="status"></span></div>
    <div id="int-speed-${host}" class="mt-8"></div>`;
}

// About a minute of production calls on the card (a warm-up, two deletion
// verdicts, one card note) against what the bench measured for it. The
// server runs it through the curator gate: it waits for a running chat and
// pauses background work rather than racing it. It left the onboarding
// (2026-11-14), where it ran beside the first enrichment.
export async function runSpeedCheck(btn, host) {
  const el = document.getElementById(`int-speed-${host}`);
  const card = (_advice[host] || {}).card || {};
  btnBusy(btn, t('setup.mb_running'));
  let mb = null, mbErr = null;
  try {
    mb = await api('/api/setup/minibench', 'POST', {vram_gb: card.vram_gb ?? null, gpu_name: card.name ?? null});
  } catch (e) { mbErr = _errMsg(e); }
  if (el) el.innerHTML = renderMinibench(mb, mbErr);
  btnDone(btn);
}

function _form(host) {
  const v = key => _field(host, key)?.value;
  return {curator: v('curator'), profile: v('profile'), num_ctx: v('num_ctx'), num_gpu: v('num_gpu')};
}

function _render(host) {
  const box = document.getElementById(`int-advice-${host}`);
  if (box && _advice[host]) box.innerHTML = _adviceHtml(host, _advice[host], _form(host));
}

function _set(el, value) {
  if (!el || value === undefined || value === null) return;
  el.value = String(value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
}

function _applyProfile(host, vals) {
  _set(_field(host, 'profile'), vals.profile);
  _set(_field(host, 'num_ctx'), vals.num_ctx);
  _set(_field(host, 'num_gpu'), vals.num_gpu);
}

// The bench's package for this card: its curator and that curator's profile,
// into the fields (Save still decides).
export function applyBenchValues(host) {
  const part = _advice[host];
  const rec = part?.advice.package?.recommended;
  if (!rec) return;
  const sel = _field(host, 'curator');
  if (sel && ![...sel.options].some(o => o.value === rec.model)) {
    sel.insertAdjacentHTML('afterbegin', `<option value="${esc(rec.model)}">${esc(rec.model)}</option>`);
  }
  _set(sel, rec.model);
  _applyProfile(host, _measured(part, rec.model) || rec);
  _render(host);
  setStatus(document.getElementById(`int-advice-note-${host}`), t('hostadv.filled_in'), 'ok');
}

// A curator the bench measured on this card brings its own profile along,
// as in the wizard; any other leaves the three fields alone.
export function onAdviceModelChange(host, role) {
  const part = _advice[host];
  if (!part) return;
  if (role === 'curator') {
    const vals = _measured(part, _field(host, 'curator')?.value);
    if (vals) _applyProfile(host, vals);
  }
  _render(host);
}

export function onAdviceSizeChange(host) {
  const v = parseFloat(document.getElementById(`int-advice-size-${host}`)?.value);
  if (Number.isFinite(v)) _picks[host] = v; else delete _picks[host];
  loadHostAdvice();
}
