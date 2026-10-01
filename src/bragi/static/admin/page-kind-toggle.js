/* Kind toggle: show the body editor OR the "profile leads" notice
   instantly when the Kind select changes, so switching to/from Profile
   doesn't need a save+reload. Both fieldsets are always in the DOM;
   initial visibility is server-set to match the persisted kind. The
   server blanks a profile page's body regardless (see
   `_form_from_request`), so the hidden textarea can't persist a body. */
(function () {
  'use strict';
  var kind = document.getElementById('kind');
  var bodyFs = document.getElementById('page-body-fieldset');
  var noticeFs = document.getElementById('page-profile-notice');
  if (!kind || !bodyFs || !noticeFs) return;
  function updateKind() {
    var resume = document.getElementById('page-resume-fieldset');
    if (resume) {
      resume.hidden = kind.value !== 'resume';
      resume.querySelector('fieldset').disabled = resume.hidden;
    }
    var permalinks = document.getElementById('page-permalink-fieldset');
    if (permalinks) {
      permalinks.hidden = kind.value !== 'post_index';
      permalinks.disabled = permalinks.hidden;
    }
    var isProfile = kind.value === 'profile';
    bodyFs.hidden = isProfile;
    noticeFs.hidden = !isProfile;
  }
  kind.addEventListener('change', updateKind);
  updateKind();
})();
