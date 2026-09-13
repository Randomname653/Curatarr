import { _setNotifPref, cleanupOrphans, clearIntegrationSecret, loadDepsStatus, loadIntegrations, loadNotificationPreferences, openLibrarySettings, openSettingsPane, openUsersSettings, reattributeHistory, rebuildModels, saveIntegrations, showSettingsAccount, submitPinChange, submitPinSet, testIntegration } from './settings.js';
import { addArrItem, addBacklogArtist, debouncedAddSearch, goToLibrarySettings, loadArrPage, reEnrich, renderSpotifyBacklog, renderSynopsisBrowser, setArrTab, setBacklogNotAddedOnly, setBacklogOnlyResolved, setBrowserFilter, setBrowserSearch, setBrowserSort } from './arr.js';
import { _syncDelPosterVisual, approveDelete, bulkDelete, delClearSelection, delToggleAll, loadDeletions, onFixMatch, onReevaluateDeletion, rejectDelete, reloadDeletions, startArrPreEnrich, toggleDelSelect, toggleRecentOnly, updateDelBulkCount, onRecentOnlyChange } from './deletions.js';
import { auditRequeueEnrichments, computeTaste, loadMusicStatus, omdbBackfill, startEnrichForce, startEnrichNew, startMusicPipeline, stopMusicPipeline } from './music.js';
import { buildOnboardingModels, detectGpu, hideOnboarding, logout, refreshModelRecs, renderSetupStep, saveOnboardingLibraries, setupNav, startOnboardingSync, testConn } from './setup.js';
import { cancelTask, loadTaskHistory } from './activity.js';
import { checkMappingCoverage, closeKbDrilldown, kbDismissFinding, kbFixMatch, kbIgnore, kbRetry, kbUnignore, loadCacheInventory, loadEnrichStatus, loadKbAttention, loadKbItems, loadMappingStats, loadProfiles, runMaintenance, showKbTab, startBackfill, stopBackfill } from './kb.js';
import { checkOrphans, loadLibraryConfig, saveLibraries, searchOnEnter } from './libraries.js';
import { closeModal, toggleMenu } from './ui.js';
import { condensePrinciples, downscaleDone, liftProtection, loadDownscale, loadJudgeProtections, loadPrinciples, loadRedundancy, loadUpgrades, setPrinciple, shutdownServer, curationSection } from './curation.js';
import { correctChatAnchor, deleteFromDiscussion, discussLastPlayed, exitDiscussion, fillPrompt, handleKey, newChat, onApplyOrphanRepair, onDiscussDeletion, onDiscussRec, saveComment, sendMessage, useStarter } from './chat.js';
import { discussPrinciple, respondToMessage, skipMessage, toggleMsgPanel } from './notifications.js';
import { finishSetup, handleSpotifyDrop, runSpotifyImport, uploadSpotify } from './spotify_import.js';
import { loadArrProfiles, loadLibrarySettings, saveArrConfig, saveArrDefaults, testArr } from './library_settings.js';
import { loadHistoryStatus, recomputeTaste, showTasteTab, syncHistory } from './history.js';
import { loadReclassify, moveReclassify, rcClearSelection, rcPickUncertain, rcToggleSection, updateReclassifyCount } from './reclassify.js';
import { loadRecs, onAddRecToArr, regenerateRecs, reloadRecs, searchLibrary, setRecLane } from './recs.js';
import { loadReport, writeYearlyReview } from './report.js';
import { loadUsers, toggleUser, loadProfilesOnEnter } from './admin.js';
import { pickerFreePin, pickerPin, pickerReject, removeFixMatch } from './picker.js';
import { showView, toggleMobileSidebar, toggleSidebar, topbarSearch, showLibrariesForce } from './nav.js';
import { setUser, showApp, startPlexLogin } from './auth.js';
import { api } from './api.js';
import { state } from './state.js';
export function init() {
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    document.getElementById('topbar-search')?.focus();
  }
});
if (localStorage.getItem('curatarr_sidebar_collapsed') === '1') {
  document.getElementById('sidebar').classList.add('collapsed');
  document.getElementById('sb-collapse-toggle').title = 'Expand sidebar';
}
window.onload = async () => {
  // Check if setup is needed
  const status = await api('/api/setup/status').catch(()=>({first_run:true}));
  if (status.first_run) {
    renderSetupStep(0);
    return;
  }
  document.getElementById('setup-overlay').classList.add('hidden');

  if (state.token) {
    try {
      const r = await api('/api/auth/status');
      if (r.authenticated) { setUser(r); showApp(); return; }
    } catch (e) {
      // Only a genuine rejection (401/403) means the state.token itself is bad.
      // Anything else here (500, server mid-reload, network hiccup) used to
      // wipe a perfectly valid session and force a full Plex re-login for a
      // failure that had nothing to do with the state.token — now it just shows
      // the login screen for this load; the stored state.token survives so the
      // next successful check logs the user back in without Plex again.
      if (e.status === 401 || e.status === 403) {
        state.token = ''; localStorage.removeItem('curatarr_token');
      }
    }
  }
  document.getElementById('auth-overlay').classList.remove('hidden');
};
document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  const menu = document.querySelector('.menu.open');
  if (menu) { menu.classList.remove('open'); return; }   // innermost layer first
  if (state._modal) closeModal();
});
document.addEventListener('click', ev => {
  const inMenu = ev.target.closest('.menu');
  if (ev.target.closest('.menu-item')) { inMenu?.classList.remove('open'); return; }
  if (!inMenu) document.querySelectorAll('.menu.open').forEach(m => m.classList.remove('open'));
});
}


const actions = {
  _setNotifPref,
  _syncDelPosterVisual,
  addArrItem,
  addBacklogArtist,
  approveDelete,
  auditRequeueEnrichments,
  buildOnboardingModels,
  bulkDelete,
  cancelTask,
  checkMappingCoverage,
  checkOrphans,
  cleanupOrphans,
  clearIntegrationSecret,
  closeKbDrilldown,
  closeModal,
  computeTaste,
  condensePrinciples,
  correctChatAnchor,
  curationSection,
  debouncedAddSearch,
  delClearSelection,
  delToggleAll,
  deleteFromDiscussion,
  detectGpu,
  discussLastPlayed,
  discussPrinciple,
  downscaleDone,
  exitDiscussion,
  fillPrompt,
  finishSetup,
  goToLibrarySettings,
  handleKey,
  handleSpotifyDrop,
  hideOnboarding,
  kbDismissFinding,
  kbFixMatch,
  kbIgnore,
  kbRetry,
  kbUnignore,
  liftProtection,
  loadArrPage,
  loadArrProfiles,
  loadCacheInventory,
  loadDeletions,
  loadDepsStatus,
  loadDownscale,
  loadEnrichStatus,
  loadHistoryStatus,
  loadIntegrations,
  loadJudgeProtections,
  loadKbAttention,
  loadKbItems,
  loadLibraryConfig,
  loadLibrarySettings,
  loadMappingStats,
  loadMusicStatus,
  loadNotificationPreferences,
  loadPrinciples,
  loadProfiles,
  loadProfilesOnEnter,
  loadReclassify,
  loadRecs,
  loadRedundancy,
  loadReport,
  loadTaskHistory,
  loadUpgrades,
  loadUsers,
  logout,
  moveReclassify,
  newChat,
  omdbBackfill,
  onAddRecToArr,
  onApplyOrphanRepair,
  onDiscussDeletion,
  onDiscussRec,
  onFixMatch,
  onRecentOnlyChange,
  onReevaluateDeletion,
  openLibrarySettings,
  openSettingsPane,
  openUsersSettings,
  pickerFreePin,
  pickerPin,
  pickerReject,
  rcClearSelection,
  rcPickUncertain,
  rcToggleSection,
  reEnrich,
  reattributeHistory,
  rebuildModels,
  recomputeTaste,
  refreshModelRecs,
  regenerateRecs,
  rejectDelete,
  reloadDeletions,
  reloadRecs,
  removeFixMatch,
  renderSpotifyBacklog,
  renderSynopsisBrowser,
  respondToMessage,
  runMaintenance,
  runSpotifyImport,
  saveArrConfig,
  saveArrDefaults,
  saveComment,
  saveIntegrations,
  saveLibraries,
  saveOnboardingLibraries,
  searchLibrary,
  searchOnEnter,
  sendMessage,
  setArrTab,
  setBacklogNotAddedOnly,
  setBacklogOnlyResolved,
  setBrowserFilter,
  setBrowserSearch,
  setBrowserSort,
  setPrinciple,
  setRecLane,
  setupNav,
  showKbTab,
  showLibrariesForce,
  showSettingsAccount,
  showTasteTab,
  showView,
  shutdownServer,
  skipMessage,
  startArrPreEnrich,
  startBackfill,
  startEnrichForce,
  startEnrichNew,
  startMusicPipeline,
  startOnboardingSync,
  startPlexLogin,
  stopBackfill,
  stopMusicPipeline,
  submitPinChange,
  submitPinSet,
  syncHistory,
  testArr,
  testConn,
  testIntegration,
  toggleDelSelect,
  toggleMenu,
  toggleMobileSidebar,
  toggleMsgPanel,
  toggleRecentOnly,
  toggleSidebar,
  toggleUser,
  topbarSearch,
  updateDelBulkCount,
  updateReclassifyCount,
  uploadSpotify,
  useStarter,
  writeYearlyReview,
};

function dispatchAction(el, nameAttr, e) {
  if (!el) return;
  const name = el.getAttribute(nameAttr);
  if (!name) return;

  if (!actions[name]) {
    console.error(`Unknown action: ${name}`, el);
    return;
  }

  const argsAttr = el.getAttribute('data-args');
  let args = [];
  if (argsAttr) {
    try {
      args = JSON.parse(argsAttr).map(arg => {
        if (arg === '$el') return el;
        if (arg === '$event') return e;
        return arg;
      });
    } catch (err) {
      console.error(`Invalid data-args on ${name}:`, argsAttr, err);
    }
  }

  actions[name](...args);
}

document.addEventListener('click', e => {
  const el = e.target.closest('[data-action]');
  if (el) dispatchAction(el, 'data-action', e);
});

['change', 'input', 'keydown', 'submit', 'drop', 'dragover', 'dragleave'].forEach(evt => {
  document.addEventListener(evt, e => {
    const el = e.target.closest(`[data-on-${evt}]`);
    if (el) dispatchAction(el, `data-on-${evt}`, e);
  });
});

['toggle', 'blur', 'error'].forEach(evt => {
  document.addEventListener(evt, e => {
    // toggle, blur and error do not bubble: listen in the capture phase at the
    // document and resolve the element from the event target as usual.
    const target = e.target;
    if (target && target.closest) {
      const el = target.closest(`[data-on-${evt}]`);
      if (el) dispatchAction(el, `data-on-${evt}`, e);
    }
  }, true);
});

Object.assign(window, {
  _setNotifPref,
  _syncDelPosterVisual,
  addArrItem,
  addBacklogArtist,
  approveDelete,
  buildOnboardingModels,
  cancelTask,
  clearIntegrationSecret,
  closeKbDrilldown,
  closeModal,
  condensePrinciples,
  debouncedAddSearch,
  detectGpu,
  discussLastPlayed,
  discussPrinciple,
  downscaleDone,
  fillPrompt,
  finishSetup,
  goToLibrarySettings,
  handleSpotifyDrop,
  hideOnboarding,
  kbDismissFinding,
  kbFixMatch,
  kbIgnore,
  kbRetry,
  kbUnignore,
  liftProtection,
  loadArrPage,
  loadArrProfiles,
  loadCacheInventory,
  loadDeletions,
  loadEnrichStatus,
  loadHistoryStatus,
  loadIntegrations,
  loadKbAttention,
  loadKbItems,
  loadLibraryConfig,
  loadLibrarySettings,
  loadNotificationPreferences,
  loadProfiles,
  loadReclassify,
  loadReport,
  loadTaskHistory,
  loadUsers,
  onAddRecToArr,
  onApplyOrphanRepair,
  onDiscussDeletion,
  onDiscussRec,
  onFixMatch,
  onReevaluateDeletion,
  pickerFreePin,
  pickerPin,
  pickerReject,
  rcPickUncertain,
  rcToggleSection,
  rebuildModels,
  reEnrich,
  refreshModelRecs,
  regenerateRecs,
  rejectDelete,
  reloadRecs,
  removeFixMatch,
  renderSpotifyBacklog,
  renderSynopsisBrowser,
  respondToMessage,
  runMaintenance,
  saveArrConfig,
  saveArrDefaults,
  saveComment,
  saveIntegrations,
  saveOnboardingLibraries,
  setArrTab,
  setBacklogNotAddedOnly,
  setBacklogOnlyResolved,
  setBrowserFilter,
  setBrowserSearch,
  setBrowserSort,
  setPrinciple,
  showKbTab,
  showTasteTab,
  showView,
  skipMessage,
  startArrPreEnrich,
  startBackfill,
  startOnboardingSync,
  stopBackfill,
  syncHistory,
  testArr,
  testConn,
  testIntegration,
  toggleDelSelect,
  toggleMenu,
  toggleUser,
  updateDelBulkCount,
  updateReclassifyCount,
  uploadSpotify,
  useStarter,
  writeYearlyReview,
});

init();