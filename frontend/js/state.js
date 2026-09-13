// Shared mutable state, one object so every module reads and writes the same
// binding (an imported `let` cannot be assigned). Initial values are the ones
// the single-file app declared: a null where the code expects [] or {} is a
// TypeError on the first `.map` or `[svc]`.
export const state = {
  taskEventSource: null,
  currentDelCategory: null,
  _delProposalsAll: null,
  _kbTab: 'overview',
  discoverSections: [],
  currentRecsLane: 'all',
  currentRecsCategory: null,
  token: localStorage.getItem('curatarr_token') || '',
  currentUser: null,
  setupData: {},
  pendingDiscussContext: null,
  _lastPlayedEntries: [],
  _backlogState: {},   // svc -> { only_resolved, not_added_only, limit, data, loaded_at }
  pollInterval: null,
  taskStreamRetries: 0,
  _recsPollTimer: null,
  _recsPollKillswitch: null,
  setupStep: 0,
  libraryCfg: [],
  _modal: null,
};
