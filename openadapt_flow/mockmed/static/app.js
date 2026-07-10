'use strict';

/*
 * MockMed — hash-routed static SPA. All data is fake.
 *
 * Drift modes are read from the query string EXACTLY ONCE at load time.
 * Navigation only ever changes location.hash, which never touches
 * location.search — so ?drift=... persists across every screen.
 */

var DRIFT = new Set(
  (new URLSearchParams(location.search).get('drift') || '')
    .split(',')
    .map(function (s) { return s.trim(); })
    .filter(Boolean)
);

if (DRIFT.has('theme')) { document.body.classList.add('drift-theme'); }
if (DRIFT.has('move')) { document.body.classList.add('drift-move'); }
if (DRIFT.has('font')) { document.body.classList.add('drift-font'); }
if (DRIFT.has('zoom')) { document.body.classList.add('drift-zoom'); }

// Navigation renders are delayed by SLOW_MS when drift mode "slow" is on
// (the previous screen stays up meanwhile, like a slow server round-trip).
// The initial render is never delayed. Override the delay with ?slowms=N.
var SLOW_MS = DRIFT.has('slow')
  ? parseInt(new URLSearchParams(location.search).get('slowms') || '4000', 10)
  : 0;

var LABEL_SAVE = DRIFT.has('rename') ? 'Submit Encounter' : 'Save Encounter';
var LABEL_OPEN = DRIFT.has('rename') ? 'View' : 'Open';

var PATIENTS = [
  { id: 'p1', name: 'Jane Sample', dob: '1980-01-01',
    reason: 'Knee pain referral', priority: 'High' },
  { id: 'p2', name: 'Alex Testcase', dob: '1975-05-05',
    reason: 'Cardiology follow-up', priority: 'Medium' },
  { id: 'p3', name: 'Sam Specimen', dob: '1990-09-09',
    reason: 'Dermatology consult', priority: 'Low' }
];

// Data-drift modes rewrite the referral list before first render:
//  - grow: four unrelated referrals arrive above the recorded target, so
//    every recorded row moves ~180 px down (data growth between runs).
//  - lookalike: ONE new referral with the same reason and priority as the
//    recorded target lands directly above it — the pixels around its Open
//    button are identical to the recorded crop (the patient name column is
//    outside the 160 px template), at exactly the recorded position.
//  - missing: the recorded target's referral is gone; similar rows remain.
//  - empty: no referrals at all.
if (DRIFT.has('grow')) {
  PATIENTS = [
    { id: 'g1', name: 'Pat Placeholder', dob: '1988-02-02',
      reason: 'Orthopedics intake', priority: 'Low' },
    { id: 'g2', name: 'Robin Redacted', dob: '1971-03-03',
      reason: 'Neurology referral', priority: 'Medium' },
    { id: 'g3', name: 'Casey Control', dob: '1969-04-04',
      reason: 'Endocrine consult', priority: 'Low' },
    { id: 'g4', name: 'Drew Dataset', dob: '1994-06-06',
      reason: 'Physio assessment', priority: 'Medium' }
  ].concat(PATIENTS);
}
if (DRIFT.has('lookalike')) {
  PATIENTS = [
    { id: 'p0', name: 'Taylor Duplicate', dob: '1982-12-12',
      reason: 'Knee pain referral', priority: 'High' }
  ].concat(PATIENTS);
}
if (DRIFT.has('missing')) {
  PATIENTS = PATIENTS.filter(function (p) { return p.id !== 'p1'; });
}
if (DRIFT.has('empty')) { PATIENTS = []; }

var state = {
  currentPatientId: null,
  encounterType: null,
  banner: null,        // { patientId, text }
  encounters: {}       // patientId -> [{ type, note }]
};

function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

var app = document.getElementById('app');
var modalRoot = document.getElementById('modal-root');

function renderLogin() {
  app.innerHTML =
    '<h1>Sign in to MockMed</h1>' +
    '<div id="login-card">' +
    '<label for="username">Username</label>' +
    '<input id="username" type="text" autocomplete="off">' +
    '<label for="password">Password</label>' +
    '<input id="password" type="password" autocomplete="off">' +
    '<div class="actions"><button id="signin">Sign In</button></div>' +
    '</div>';
  document.getElementById('signin').addEventListener('click', function () {
    location.hash = '#tasks';
  });
}

function renderTasks() {
  state.banner = null;
  var rows = PATIENTS.map(function (p) {
    return '<tr>' +
      '<td>' + esc(p.name) + '</td>' +
      '<td>' + esc(p.reason) + '</td>' +
      '<td>' + esc(p.priority) + '</td>' +
      '<td><button class="open-btn" id="open-' + p.id + '" data-id="' +
      p.id + '">' + LABEL_OPEN + '</button></td>' +
      '</tr>';
  }).join('');
  app.innerHTML =
    '<h1>Referral Tasks</h1>' +
    '<table id="tasks-table"><thead><tr>' +
    '<th>Patient</th><th>Reason</th><th>Priority</th><th>Action</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>' +
    (PATIENTS.length ? '' : '<p id="no-referrals">No pending referrals.</p>');
  app.querySelectorAll('.open-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      location.hash = '#patient/' + btn.getAttribute('data-id');
    });
  });
}

function renderPatient(id) {
  var p = null;
  for (var k = 0; k < PATIENTS.length; k++) {
    if (PATIENTS[k].id === id) { p = PATIENTS[k]; }
  }
  if (!p) { p = PATIENTS[0]; }
  state.currentPatientId = p.id;

  var bannerHtml = '';
  if (state.banner && state.banner.patientId === p.id) {
    bannerHtml = '<div id="saved-banner">' + esc(state.banner.text) + '</div>';
  }

  var encs = state.encounters[p.id] || [];
  var encHtml;
  if (encs.length) {
    encHtml = '<ul id="encounter-list">' + encs.map(function (e) {
      return '<li class="enc-item">' + esc(e.type) + ' — ' +
        esc(e.note.slice(0, 60)) + '</li>';
    }).join('') + '</ul>';
  } else {
    encHtml = '<p id="no-encounters">No encounters yet.</p>';
  }

  app.innerHTML =
    '<div id="patient-banner">' + esc(p.name) + ' — MRN ' +
    esc(p.id.toUpperCase()) + ' — DOB ' + esc(p.dob) + '</div>' +
    bannerHtml +
    '<div class="actions movable">' +
    '<button id="new-encounter">New Encounter</button></div>' +
    '<h2>Encounters</h2>' + encHtml;

  document.getElementById('new-encounter')
    .addEventListener('click', function () {
      state.banner = null;
      location.hash = '#encounter';
    });
}

function showSurveyModal() {
  modalRoot.innerHTML =
    '<div id="modal-overlay"><div id="survey-modal">' +
    '<h2>Survey</h2>' +
    '<p>Before saving, please rate your experience with MockMed today.</p>' +
    '<div class="actions"><button id="survey-dismiss">Maybe later</button>' +
    '</div></div></div>';
  document.getElementById('survey-dismiss')
    .addEventListener('click', function () {
      modalRoot.innerHTML = '';
    });
}

function renderEncounter() {
  state.banner = null;
  if (!state.currentPatientId) { state.currentPatientId = PATIENTS[0].id; }
  state.encounterType = null;

  app.innerHTML =
    '<h1>New Encounter</h1>' +
    '<label id="type-label">Encounter Type</label>' +
    '<div class="segmented" id="type-seg">' +
    '<button id="type-triage" class="seg-btn">Triage</button>' +
    '<button id="type-consult" class="seg-btn">Consult</button>' +
    '</div>' +
    '<label for="note" id="note-label">Note</label>' +
    '<textarea id="note" rows="6"></textarea>' +
    '<div class="actions movable">' +
    '<button id="save-encounter">' + LABEL_SAVE + '</button></div>';

  ['triage', 'consult'].forEach(function (t) {
    document.getElementById('type-' + t)
      .addEventListener('click', function () {
        state.encounterType = t === 'triage' ? 'Triage' : 'Consult';
        document.querySelectorAll('.seg-btn').forEach(function (b) {
          b.classList.remove('selected');
        });
        this.classList.add('selected');
      });
  });

  document.getElementById('save-encounter')
    .addEventListener('click', function () {
      var note = document.getElementById('note').value;
      if (DRIFT.has('modal')) {
        // Semantic drift: a blocking modal appears INSTEAD of the saved
        // banner; the encounter is not saved.
        showSurveyModal();
        return;
      }
      var pid = state.currentPatientId;
      if (!state.encounters[pid]) { state.encounters[pid] = []; }
      state.encounters[pid].push({
        type: state.encounterType || 'Triage',
        note: note
      });
      state.banner = {
        patientId: pid,
        text: 'Encounter saved — ' + note.slice(0, 40)
      };
      location.hash = '#patient/' + pid;
    });
}

function route() {
  modalRoot.innerHTML = '';
  var hash = location.hash;
  if (!hash || hash === '#' || hash === '#login') {
    renderLogin();
  } else if (hash === '#tasks') {
    renderTasks();
  } else if (hash.indexOf('#patient/') === 0) {
    renderPatient(hash.slice('#patient/'.length));
  } else if (hash === '#encounter') {
    renderEncounter();
  } else {
    renderLogin();
  }
}

var slowTimer = null;
window.addEventListener('hashchange', function () {
  if (!SLOW_MS) { route(); return; }
  // Slow drift: keep the previous screen up for SLOW_MS, then render.
  if (slowTimer !== null) { clearTimeout(slowTimer); }
  slowTimer = setTimeout(function () { slowTimer = null; route(); }, SLOW_MS);
});
route();
