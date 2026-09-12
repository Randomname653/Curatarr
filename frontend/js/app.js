import { _setNotifPref, cleanupOrphans, clearIntegrationSecret, loadDepsStatus, loadIntegrations, loadNotificationPreferences, openSettingsPane, reattributeHistory, rebuildModels, saveIntegrations, submitPinChange, submitPinSet, testIntegration } from './settings.js';
import { addArrItem, addBacklogArtist, debouncedAddSearch, goToLibrarySettings, loadArrPage, reEnrich, renderSpotifyBacklog, renderSynopsisBrowser, setArrTab, setBacklogNotAddedOnly, setBacklogOnlyResolved, setBrowserFilter, setBrowserSearch, setBrowserSort } from './arr.js';
import { approveDelete, bulkDelete, delClearSelection, delToggleAll, loadDeletions, onFixMatch, onReevaluateDeletion, rejectDelete, startArrPreEnrich, toggleDelSelect, toggleRecentOnly, updateDelBulkCount } from './deletions.js';
import { auditRequeueEnrichments, computeTaste, loadMusicStatus, omdbBackfill, startEnrichForce, startEnrichNew, startMusicPipeline, stopMusicPipeline } from './music.js';
import { buildOnboardingModels, detectGpu, hideOnboarding, logout, refreshModelRecs, renderSetupStep, saveOnboardingLibraries, setupNav, startOnboardingSync, testConn } from './setup.js';
import { cancelTask, loadTaskHistory } from './activity.js';
import { checkMappingCoverage, closeKbDrilldown, kbDismissFinding, kbFixMatch, kbIgnore, kbRetry, kbUnignore, loadCacheInventory, loadEnrichStatus, loadKbAttention, loadKbItems, loadMappingStats, loadProfiles, runMaintenance, showKbTab, startBackfill, stopBackfill } from './kb.js';
import { checkOrphans, loadLibraryConfig, saveLibraries } from './libraries.js';
import { closeModal, toggleMenu } from './ui.js';
import { condensePrinciples, downscaleDone, liftProtection, setPrinciple, shutdownServer } from './curation.js';
import { correctChatAnchor, deleteFromDiscussion, discussLastPlayed, exitDiscussion, fillPrompt, handleKey, newChat, onApplyOrphanRepair, onDiscussDeletion, onDiscussRec, saveComment, sendMessage, useStarter } from './chat.js';
import { discussPrinciple, respondToMessage, skipMessage, toggleMsgPanel } from './notifications.js';
import { finishSetup, handleSpotifyDrop, runSpotifyImport, uploadSpotify } from './spotify_import.js';
import { loadArrProfiles, loadLibrarySettings, saveArrConfig, saveArrDefaults, testArr } from './library_settings.js';
import { loadHistoryStatus, recomputeTaste, showTasteTab, syncHistory } from './history.js';
import { loadReclassify, moveReclassify, rcClearSelection, rcPickUncertain, rcToggleSection, updateReclassifyCount } from './reclassify.js';
import { loadRecs, onAddRecToArr, regenerateRecs, searchLibrary, setRecLane } from './recs.js';
import { loadReport, writeYearlyReview } from './report.js';
import { loadUsers, toggleUser } from './admin.js';
import { pickerFreePin, pickerPin, pickerReject, removeFixMatch } from './picker.js';
import { showView, toggleMobileSidebar, toggleSidebar, topbarSearch } from './nav.js';
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

Object.assign(window, {
  _setNotifPref,
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
  debouncedAddSearch,
  delClearSelection,
  deleteFromDiscussion,
  delToggleAll,
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
  loadEnrichStatus,
  loadHistoryStatus,
  loadIntegrations,
  loadKbAttention,
  loadKbItems,
  loadLibraryConfig,
  loadLibrarySettings,
  loadMappingStats,
  loadMusicStatus,
  loadNotificationPreferences,
  loadProfiles,
  loadReclassify,
  loadRecs,
  loadReport,
  loadTaskHistory,
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
  onReevaluateDeletion,
  openSettingsPane,
  pickerFreePin,
  pickerPin,
  pickerReject,
  rcClearSelection,
  rcPickUncertain,
  rcToggleSection,
  reattributeHistory,
  rebuildModels,
  recomputeTaste,
  reEnrich,
  refreshModelRecs,
  regenerateRecs,
  rejectDelete,
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
});

init();