// ── GAME PROCESS MONITOR ──────────────────────────────────────────────────────
import { toast } from './ui.js';
import { api } from './api.js';
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

export function _renderLaneBadge(statusR) {
  const indicator = document.getElementById('game-indicator');
  if (!indicator || !statusR) return;
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
    const statusR = await api('/api/processes/status').catch(() => null);
    _renderLaneBadge(statusR);

    // Check for unknown processes — show one toast per unknown exe
    const r = await api('/api/processes/unknown').catch(() => null);
    if (!r || !r.processes) return;
    for (const proc of r.processes) {
      const nl = proc.name.toLowerCase();
      if (_shownProcesses.has(nl)) continue;
      _shownProcesses.add(nl);
      _showProcessToast(proc.name);
    }
  } catch(_) {}
}

export function startProcessMonitor() {
  if (_processPollTimer) return;
  _pollGameProcesses();   // immediate first check
  _processPollTimer = setInterval(_pollGameProcesses, 30_000);
}
