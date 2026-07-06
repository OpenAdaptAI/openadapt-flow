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
    '</tr></thead><tbody>' + rows + '</tbody></table>';
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

window.addEventListener('hashchange', route);
route();
