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

export async function _pollGameProcesses() {
  try {
    // Update topbar indicator
    const statusR = await api('/api/processes/status').catch(() => null);
    const indicator = document.getElementById('game-indicator');
    if (indicator && statusR) {
      indicator.classList.toggle('show', !!statusR.game_running);
    }

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
