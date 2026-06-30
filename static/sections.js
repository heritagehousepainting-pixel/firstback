/* sections.js — Section-switcher progressive enhancement.
   Progressive: without JS (or on no-JS browsers), all .app-section elements
   stay visible — this script adds 'sectioned-on' to body to enable single-
   section mode. CSS display:none on hidden sections does NOT prevent form
   field serialisation (only 'disabled' does), so all form POSTs work correctly.
*/
(function () {
  'use strict';
  var sections = Array.from(document.querySelectorAll('.app-section'));
  if (!sections.length) return;

  document.body.classList.add('sectioned-on');

  /* For the settings page: the main form (set-profile through set-ai) must be
     hidden when a standalone-form section (set-setup/growth/billing/password)
     is active, otherwise an empty form with a ghost save-bar floats on screen. */
  var mainForm = document.getElementById('settings-main-form');
  var mainFormIds = new Set([
    'set-profile', 'set-voice', 'set-calendar', 'set-crm',
    'set-screening', 'set-scheduling', 'set-alerts',
    'set-reminders', 'set-widget', 'set-ai'
  ]);

  function activate(id) {
    /* Find the target section; fall back to first if id is empty or unmatched. */
    var target = null;
    sections.forEach(function (s) { if (s.id === id) target = s; });
    if (!target) target = sections[0];
    var activeId = target.id;

    sections.forEach(function (s) {
      s.classList.toggle('is-active', s === target);
    });

    /* Sync aria-current on sub-nav items and mobile pill anchors. */
    document.querySelectorAll('.sec-nav-item, .sec-pill').forEach(function (a) {
      var frag = (a.getAttribute('href') || '').split('#')[1] || '';
      var isActive = frag === activeId;
      a.classList.toggle('is-active', isActive);
      if (isActive) { a.setAttribute('aria-current', 'location'); }
      else { a.removeAttribute('aria-current'); }
    });

    /* Settings page: hide main form when a standalone-form section is active. */
    if (mainForm) {
      mainForm.style.display = mainFormIds.has(activeId) ? '' : 'none';
    }
  }

  function syncToHash() {
    activate(location.hash.replace('#', ''));
  }

  window.addEventListener('hashchange', syncToHash);
  syncToHash();
}());
