/* Bulk-select admin behaviour.
 *
 * Vanilla JS, no framework, no build step. One module shared by the
 * Posts, Pages, and Attachments list pages.
 *
 * Init contract:
 *   window.bragiBulkSelect.init({
 *     wrapperSelector: "#post-list-table",
 *     barSelector: ".bulk-actions-bar[data-wrapper='#post-list-table']",
 *   })
 *
 * The module is event-delegated on the wrapper so htmx swaps that
 * replace the table contents do not strip listeners.
 */
(function () {
  "use strict";

  function init(opts) {
    const wrapper = document.querySelector(opts.wrapperSelector);
    const bar = document.querySelector(opts.barSelector);
    if (!wrapper || !bar) return;

    const enterBtn = bar.querySelector(".bulk-select-enter");
    const controls = bar.querySelector(".bulk-select-controls");
    const cancelBtn = bar.querySelector(".bulk-select-cancel");
    const selectAllBtn = bar.querySelector(".bulk-select-all");
    const deleteBtn = bar.querySelector(".bulk-select-delete");
    const dialog = bar.querySelector(".bulk-confirm-dialog");
    const dialogConfirm = dialog.querySelector(".bulk-confirm-confirm");
    const dialogCancel = dialog.querySelector(".bulk-confirm-cancel");
    const titlesList = dialog.querySelector(".bulk-confirm-titles");
    const moreNote = dialog.querySelector(".bulk-confirm-more");
    const moreCount = dialog.querySelector(".bulk-more-count");
    const submitForm = bar.querySelector(".bulk-submit-form");

    function selected() {
      return Array.from(
        wrapper.querySelectorAll('input.bulk-select[type="checkbox"]:checked')
      );
    }

    function updateCount() {
      const n = selected().length;
      bar.querySelectorAll(".bulk-count").forEach((el) => {
        el.textContent = String(n);
      });
      deleteBtn.disabled = n === 0;
    }

    function enterMode() {
      wrapper.classList.add("select-mode");
      controls.hidden = false;
      enterBtn.hidden = true;
      updateCount();
    }

    function exitMode() {
      wrapper
        .querySelectorAll('input.bulk-select[type="checkbox"]')
        .forEach((cb) => {
          cb.checked = false;
        });
      wrapper.classList.remove("select-mode");
      controls.hidden = true;
      enterBtn.hidden = false;
      updateCount();
    }

    enterBtn.addEventListener("click", enterMode);
    cancelBtn.addEventListener("click", exitMode);

    selectAllBtn.addEventListener("click", function () {
      const boxes = wrapper.querySelectorAll(
        'input.bulk-select[type="checkbox"]'
      );
      const anyUnchecked = Array.from(boxes).some((b) => !b.checked);
      boxes.forEach((b) => {
        b.checked = anyUnchecked;
      });
      updateCount();
    });

    // Event-delegated change tracking so htmx swaps survive.
    wrapper.addEventListener("change", function (evt) {
      if (evt.target.matches('input.bulk-select[type="checkbox"]')) {
        updateCount();
      }
    });

    deleteBtn.addEventListener("click", function () {
      const sel = selected();
      const titles = sel
        .map((cb) => {
          const row = cb.closest("tr");
          const titleEl = row && row.querySelector(".bulk-row-title");
          return (titleEl && titleEl.textContent.trim()) || "(untitled)";
        })
        .slice(0, 3);
      titlesList.innerHTML = "";
      titles.forEach((t) => {
        const li = document.createElement("li");
        li.textContent = t;
        titlesList.appendChild(li);
      });
      if (sel.length > 3) {
        moreCount.textContent = String(sel.length - 3);
        moreNote.hidden = false;
      } else {
        moreNote.hidden = true;
      }
      dialog.showModal();
    });

    dialogCancel.addEventListener("click", function () {
      dialog.close();
    });

    dialogConfirm.addEventListener("click", function () {
      const sel = selected();
      submitForm
        .querySelectorAll('input[name="ids"]')
        .forEach((el) => el.remove());
      sel.forEach((cb) => {
        const inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = "ids";
        inp.value = cb.value;
        submitForm.appendChild(inp);
      });
      dialog.close();
      submitForm.submit();
    });

    // Auto-exit select-mode after an htmx swap of the wrapper, e.g.
    // following a successful bulk-delete that re-renders the partial.
    document.body.addEventListener("htmx:afterSwap", function (evt) {
      if (evt.target === wrapper || wrapper.contains(evt.target)) {
        exitMode();
      }
    });
  }

  window.bragiBulkSelect = { init: init };
})();
