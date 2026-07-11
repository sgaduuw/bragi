// Unsplash tab: click a result card -> POST to select it -> synthesise a
// hidden .picker-card with the attachment data-* shape the host picker
// expects, and click it so the host picker's dialog handler runs.
//
// This markup is swapped into the image dialog via htmx; scan for panels and
// wire each once (idempotent via `data-unsplash-wired`). Was previously an
// inline script using `document.currentScript` — that breaks once external,
// so it scopes to `.unsplash-panel` by class instead.
(function () {
  document.querySelectorAll(".unsplash-panel").forEach(function (panel) {
    if (panel.dataset.unsplashWired === "1") return;
    panel.dataset.unsplashWired = "1";

    panel.addEventListener("click", function (e) {
      var card = e.target.closest(".unsplash-card");
      if (!card) return;
      e.preventDefault();

      var siteSlug = panel.dataset.siteSlug;
      var photoId = card.dataset.photoId;
      card.disabled = true;
      card.style.opacity = "0.5";

      var formData = new FormData();
      formData.append("photo_id", photoId);
      // Include the session CSRF token so the CSRF guard passes.
      var csrfMeta = document.querySelector('meta[name="csrf-token"]');
      if (csrfMeta) formData.append("_csrf_token", csrfMeta.content);

      fetch("/admin/sites/" + siteSlug + "/unsplash/select", {
        method: "POST",
        body: formData,
      })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.text().then(function (t) {
              throw new Error("Unsplash select failed (" + resp.status + "): " + t);
            });
          }
          return resp.json();
        })
        .then(function (body) {
          var synthetic = document.createElement("button");
          synthetic.type = "button";
          synthetic.className = "picker-card";
          synthetic.dataset.attachmentId = String(body.attachment_id);
          synthetic.dataset.storageKey = body.storage_key || "";
          synthetic.dataset.filename = body.filename || "";
          synthetic.dataset.altText = body.alt_text || "";
          synthetic.dataset.renditionSmall = "";
          synthetic.dataset.renditionMedium = "";
          synthetic.dataset.renditionFull = "";
          synthetic.style.display = "none";
          panel.appendChild(synthetic);
          synthetic.click();
          panel.removeChild(synthetic);
        })
        .catch(function (err) {
          alert(err.message);
        })
        .finally(function () {
          card.disabled = false;
          card.style.opacity = "";
        });
    });
  });
})();
