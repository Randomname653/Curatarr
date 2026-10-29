// ── GAME PROCESS MONITOR ──────────────────────────────────────────────────────
import { _errMsg, toast } from './ui.js';
import { api } from './api.js';
import { loadUnreadMessages } from './notifications.js';
import { setServerModelBusy } from './brand.js';
const _shownProcesses = new Set();   // names already shown in a toast this session
let   _processPollTimer = null;

// One question per unknown exe, its two answers as toast actions — on the
// shared toast() stack, not a component of its own.
export function _showProcessToast(name) {
  toast(name, 'amber', {title: 'Unknown process detected — is this a game?', actions: [
    {label: 'Yes, game', primary: true, onClick: () => _classifyProcess(name, true)},
    {label: 'Ignore', onClick: () => _classifyProcess(name, false)},
  ]});
}

export async function _classifyProcess(name, isGame) {
  try {
    await api('/api/processes/classify', 'POST', { name, is_game: isGame });
    if (isGame) document.getElementById('game-indicator')?.classList.add('show');
    return true;
  } catch (e) {
    toast(`Could not save the answer for ${name} — try again.`, 'danger');
    return false;   // keeps the question on screen
  }
}

// The same question from the bell (services/notifications.py): it stays
// there until answered, for an admin who was not looking when it ran.
export async function answerGameQuestion(name, isGame) {
  _shownProcesses.add(String(name).toLowerCase());     // no toast for it after this
  if (await _classifyProcess(name, isGame)) loadUnreadMessages();
}

// What the badge says per lane — the watcher records the state every 30 s
// (scheduler.job_game_watcher), this only renders it.
const LANE_BADGE = {
  game:   {label: 'Game mode',
           title: 'Game detected — model work paused, API pre-fetching only'},
  cpu:    {label: 'GPU busy · CPU lane',
           title: 'Another program holds the graphics card — enrichment and the other background work run on the processor, a conversation waits'},
  paused: {label: 'GPU busy · paused',
           title: 'Another program holds the graphics card — model work is paused until it is free'},
};

// statusR._err: the status read failed. That used to return early here, so a
// "Game mode" badge stayed up after the game had ended for as long as the
// endpoint kept failing. A badge on screen now says it is out of date; with
// no badge up, nothing is claimed either way.
export function _renderLaneBadge(statusR) {
  const indicator = document.getElementById('game-indicator');
  if (!indicator || !statusR) return;
  if (statusR._err) {
    if (!indicator.classList.contains('show')) return;
    indicator.classList.add('busy');
    const label = document.getElementById('game-indicator-label');
    if (label) label.textContent = 'Status unknown';
    indicator.title = `Couldn't check the game / GPU status (${statusR._err}) — this badge may be out of date. Checking again every 30 s.`;
    return;
  }
  const lane = statusR.lane || (statusR.game_running ? 'game' : 'free');
  const badge = LANE_BADGE[lane];
  indicator.classList.toggle('show', !!badge);
  indicator.classList.toggle('busy', lane === 'cpu' || lane === 'paused');
  if (!badge) return;
  const label = document.getElementById('game-indicator-label');
  if (label) label.textContent = badge.label;
  indicator.title = statusR.lane_reason
    ? `${badge.title} (${statusR.lane_reason})` : badge.title;
}

export async function _pollGameProcesses() {
  try {
    // Update topbar indicator
    const statusR = await api('/api/processes/status').catch(e => ({_err: _errMsg(e)}));
    _renderLaneBadge(statusR);
    if (!statusR._err) setServerModelBusy(statusR.model_busy);

    // Check for unknown processes — show one toast per unknown exe
    const r = await api('/api/processes/unknown')
      .catch(e => { console.warn('[game] unknown-process poll failed', e); return null; });
    if (!r || !r.processes) return;
    for (const proc of r.processes) {
      const nl = proc.name.toLowerCase();
      if (_shownProcesses.has(nl)) continue;
      _shownProcesses.add(nl);
      _showProcessToast(proc.name);
    }
  } catch (e) {
    console.warn('[game] process poll failed', e);   // the next 30 s tick retries
  }
}

export function startProcessMonitor() {
  if (_processPollTimer) return;
  _pollGameProcesses();   // immediate first check
  _processPollTimer = setInterval(_pollGameProcesses, 30_000);
}
