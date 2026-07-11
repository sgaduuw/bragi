// Image-picker field wiring. The `image_picker_field` macro renders one or
// more `.image-picker-field` fieldsets carrying their config in data-*
// attributes; wire each once. Idempotent (a `data-imgpicker-wired` flag), so
// a page with two pickers or an htmx re-render can load this more than once
// without double-binding.
(function () {
  document.querySelectorAll(".image-picker-field").forEach(function (fieldRoot) {
    if (fieldRoot.dataset.imgpickerWired === "1") return;
    fieldRoot.dataset.imgpickerWired = "1";

    var fieldName = fieldRoot.dataset.field;
    var dialogId = fieldRoot.dataset.dialog;
    var pickerUrl = fieldRoot.dataset.pickerUrl;
    var attachmentPrefix = fieldRoot.dataset.attachmentPrefix;

    var dialog = document.getElementById(dialogId);
    var hidden = document.getElementById(fieldName);
    if (!dialog || !hidden) return;

    var target = dialog.querySelector(".image-picker-grid-target");
    var previewEl = fieldRoot.querySelector(".image-picker-preview");
    var pickBtn = fieldRoot.querySelector(".image-picker-pick");
    var clearBtn = fieldRoot.querySelector(".image-picker-clear");

    function openPicker() {
      if (window.htmx) {
        window.htmx.ajax("GET", pickerUrl, { target: target, swap: "innerHTML" });
      } else {
        target.innerHTML = "<p>htmx unavailable; close and try again.</p>";
      }
      dialog.showModal();
    }

    function setSelection(card) {
      var key = card.dataset.storageKey || "";
      var altText = card.dataset.altText || "";
      var filename = card.dataset.filename || "";
      var attachmentId = card.dataset.attachmentId || "";
      if (!attachmentId) return;
      hidden.value = attachmentId;
      previewEl.style.display = "flex";
      previewEl.innerHTML =
        '<img class="image-picker-thumb" src="' + attachmentPrefix + key + '"' +
        ' alt="' + altText.replace(/"/g, "&quot;") + '"' +
        ' style="max-width: 160px; max-height: 120px; object-fit: cover;' +
        ' border: 1px solid #ddd; border-radius: 4px;">' +
        '<span class="image-picker-filename" style="font-family: monospace; font-size: 0.875em;">' +
        filename + '</span>';
      pickBtn.textContent = "Change image";
      clearBtn.style.display = "inline-block";
      dialog.close();
    }

    function clearSelection() {
      hidden.value = "";
      previewEl.style.display = "none";
      previewEl.innerHTML = "";
      pickBtn.textContent = "Pick image";
      clearBtn.style.display = "none";
    }

    pickBtn.addEventListener("click", openPicker);
    clearBtn.addEventListener("click", clearSelection);

    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) {
        dialog.close();
        return;
      }
      if (e.target.closest("[data-action='close-picker']")) {
        dialog.close();
        return;
      }
      var card = e.target.closest(".picker-card");
      if (!card) return;
      e.preventDefault();
      setSelection(card);
    });
  });
})();
