/* The editor: autosave the Draft's fields, and Start / Discard via fetch.
 *
 * The first autosave that carries content creates the Draft and the URL is
 * replaced with the Draft's without a reload.  There is no merge between two
 * open tabs: the last save wins and the saved text is loaded when the page
 * opens. */
(function () {
  "use strict";

  var Sym = window.Sym;
  var form = document.getElementById("editor");
  if (!Sym || !form) {
    return;
  }

  var SAVE_INTERVAL = 800;

  var title = document.getElementById("title");
  var description = document.getElementById("description");
  var repo = document.getElementById("repo");
  var tid = document.getElementById("editor-tid");
  var dialog = document.getElementById("discard-dialog");
  var startButton = document.getElementById("act-start");
  var discardButton = document.getElementById("act-discard");
  var uploadReason = document.getElementById("editor-upload-reason");

  var saveUrl = form.getAttribute("data-api-save");
  var startUrl = form.getAttribute("data-api-start");
  var discardUrl = form.getAttribute("data-api-discard");
  var uploadNewUrl = form.getAttribute("data-api-upload-new") || "/api/new/files";

  var timer = null;
  var dirty = false;
  var inFlight = null;
  var saveRunning = false;
  var saveQueued = false;
  var created = Boolean(form.getAttribute("data-ticket-id"));
  /* The one "a Draft is being created" gate.  The first path that needs a
   * Draft -- the first autosave with content, or the first upload on /new --
   * claims it; the other waits for the id instead of creating a second Draft.
   * `draftCreating` is held by either path, `uploadClaimedDraft` says which
   * one must release it when its request settles. */
  var draftCreating = false;
  var uploadClaimedDraft = false;
  var saveDeferred = false;
  var uploader = null;
  var uploadBusy = false;
  var submitting = false;

  function fields() {
    var checked = form.querySelector('input[name="model"]:checked');
    return {
      title: title ? title.value : "",
      description: description ? description.value : "",
      repo: repo ? repo.value : "",
      model: checked ? checked.value : "",
    };
  }

  function empty(payload) {
    return (
      !payload.title.trim() &&
      !payload.description.trim() &&
      !payload.repo.trim() &&
      !payload.model
    );
  }

  function applyActions(actions) {
    if (!actions) {
      return;
    }
    form.setAttribute("action", actions.save);
    form.setAttribute("data-api-save", actions.api_save);
    form.setAttribute("data-api-start", actions.api_start);
    form.setAttribute("data-api-discard", actions.api_discard);
    if (actions.api_upload) {
      form.setAttribute("data-api-upload", actions.api_upload);
    }
    var start = document.getElementById("act-start");
    if (start) {
      start.setAttribute("formaction", actions.start);
    }
    var request = document.getElementById("discard-request");
    if (request) {
      request.setAttribute("action", actions.discard);
    }
    if (actions.keep) {
      var keep = document.getElementById("act-keep");
      if (keep) {
        keep.setAttribute("href", actions.keep);
      }
    }
    if (dialog) {
      dialog.setAttribute("data-api-discard", actions.api_discard);
      var confirmForm = dialog.querySelector("form");
      if (confirmForm) {
        confirmForm.setAttribute("action", actions.discard);
      }
    }
  }

  function adoptDraft(data) {
    if (!data || !data.id || created) {
      return;
    }
    created = true;
    saveUrl = data.actions ? data.actions.api_save : saveUrl;
    startUrl = data.actions ? data.actions.api_start : startUrl;
    discardUrl = data.actions ? data.actions.api_discard : discardUrl;
    applyActions(data.actions);
    if (tid) {
      tid.textContent = data.id;
    }
    form.setAttribute("data-ticket-id", data.id);
    if (data.url && window.history && window.history.replaceState) {
      window.history.replaceState(null, "", data.url);
    }
  }

  function schedule() {
    dirty = true;
    window.clearTimeout(timer);
    timer = window.setTimeout(save, SAVE_INTERVAL);
  }

  /* One save at a time, with at most one queued.  Two overlapping first
   * saves on /new would create two Drafts, and two overlapping edits could
   * store an older text after a newer one. */
  function save() {
    window.clearTimeout(timer);
    if (draftCreating) {
      // Someone else is creating the Draft; save once it has an id.
      saveDeferred = true;
      return Promise.resolve();
    }
    if (submitting) {
      // No autosave while Start or Discard is being submitted; their failure
      // path reschedules one.
      return Promise.resolve();
    }
    if (!dirty) {
      return Promise.resolve();
    }
    if (saveRunning) {
      saveQueued = true;
      return Promise.resolve();
    }
    var payload = fields();
    if (!created && empty(payload)) {
      // Nothing to store yet; an abandoned empty editor leaves no ticket.
      dirty = false;
      return Promise.resolve();
    }
    var claimsDraft = !created;
    if (claimsDraft) {
      draftCreating = true;
    }
    dirty = false;
    saveRunning = true;
    var sent = JSON.stringify(payload);
    inFlight = Sym.postJSON(saveUrl, payload)
      .then(function (data) {
        adoptDraft(data);
        if (data && data.saved_at) {
          Sym.showSaved("saved", data.saved_at);
        }
        if (JSON.stringify(fields()) !== sent) {
          dirty = true;
          schedule();
        }
      })
      .catch(function (error) {
        dirty = true;
        Sym.showNotSaved("saved");
        Sym.showError(error.message);
      });
    var attempt = inFlight;
    attempt.then(function () {
      if (inFlight === attempt) {
        inFlight = null;
      }
      saveRunning = false;
      if (claimsDraft) {
        draftCreating = false;
      }
      var again = saveDeferred || saveQueued;
      saveDeferred = false;
      saveQueued = false;
      if (again) {
        save();
      }
    });
    return attempt;
  }

  function retry() {
    if (dirty && !inFlight) {
      save();
    }
  }

  function applyUploadState() {
    var blocked = uploadBusy || submitting;
    if (startButton) {
      startButton.disabled = blocked;
    }
    if (discardButton) {
      discardButton.disabled = blocked;
    }
    if (uploadReason) {
      uploadReason.hidden = !uploadBusy;
      uploadReason.textContent = uploadBusy ? "Files are still uploading." : "";
    }
  }

  function onUploadBusy(busy) {
    uploadBusy = busy;
    applyUploadState();
  }

  function doStart() {
    window.clearTimeout(timer);
    dirty = false;
    saveQueued = false;
    var payload = fields();
    Sym.clearError();
    submitting = true;
    applyUploadState();
    if (uploader) {
      uploader.setLocked(true);
    }
    var waits = [inFlight || Promise.resolve()];
    if (uploader) {
      waits.push(uploader.whenIdle());
    }
    Promise.all(waits)
      .then(function () {
        return Sym.postJSON(startUrl, payload);
      })
      .then(function (data) {
        if (data && data.redirect) {
          window.location.assign(data.redirect);
        }
      })
      .catch(function (error) {
        Sym.showError(error.message);
        dirty = true;
      })
      .finally(function () {
        submitting = false;
        if (uploader) {
          uploader.setLocked(false);
        }
        applyUploadState();
        if (dirty) {
          // Start failed after the input changed: autosave it normally.
          schedule();
        }
      });
  }

  function start(event) {
    event.preventDefault();
    if (uploadBusy) {
      Sym.showError("Files are still uploading.");
      return;
    }
    if (submitting) {
      return;
    }
    doStart();
  }

  function doDiscard() {
    window.clearTimeout(timer);
    dirty = false;
    saveQueued = false;
    Sym.clearError();
    submitting = true;
    applyUploadState();
    if (uploader) {
      uploader.setLocked(true);
    }
    var waits = [inFlight || Promise.resolve()];
    if (uploader) {
      waits.push(uploader.whenIdle());
    }
    Promise.all(waits)
      .then(function () {
        return Sym.postJSON(discardUrl, {});
      })
      .then(function (data) {
        window.location.assign((data && data.redirect) || "/");
      })
      .catch(function (error) {
        Sym.showError(error.message);
        // The Draft still exists; do not lose the edits made before Discard.
        dirty = true;
      })
      .finally(function () {
        submitting = false;
        if (uploader) {
          uploader.setLocked(false);
        }
        applyUploadState();
        if (dirty) {
          schedule();
        }
      });
  }

  function discard(event) {
    if (event) {
      event.preventDefault();
    }
    if (uploadBusy) {
      Sym.showError("Files are still uploading.");
      return;
    }
    if (submitting) {
      return;
    }
    doDiscard();
  }

  [title, description, repo].forEach(function (field) {
    if (field) {
      field.addEventListener("input", schedule);
    }
  });
  form.querySelectorAll('input[name="model"]').forEach(function (radio) {
    radio.addEventListener("change", schedule);
  });

  form.addEventListener("submit", function (event) {
    if (event.submitter && event.submitter.id === "act-start") {
      start(event);
      return;
    }
    // Enter in a single-line field picks the first submit button, which is
    // Start.  Without the script the form still posts normally.
    if (!event.submitter) {
      start(event);
    }
  });

  if (dialog) {
    var confirmForm = dialog.querySelector("form");
    if (confirmForm) {
      confirmForm.addEventListener("submit", discard);
    }
  }

  /* Resolve once no autosave is running.  A queued save starts when the
   * previous one settles, so the check is repeated after each wait: returning
   * early would let the upload and a queued create race for a second Draft. */
  function waitForAutosave() {
    if (created || !inFlight) {
      return Promise.resolve();
    }
    return inFlight.then(waitForAutosave);
  }

  function uploadTarget() {
    if (created) {
      return Promise.resolve(ticketUploadUrl());
    }
    if (!draftCreating && !inFlight && dirty) {
      // Prefer the autosave's create: it carries the text too.
      save();
    }
    return waitForAutosave().then(function () {
      if (created) {
        return ticketUploadUrl();
      }
      // Nothing is saving and no Draft exists (an empty editor with only a
      // file): the upload endpoint creates it, and autosaves wait on the gate.
      draftCreating = true;
      uploadClaimedDraft = true;
      return uploadNewUrl;
    });
  }

  function ticketUploadUrl() {
    var url = form.getAttribute("data-api-upload");
    if (url) {
      return url;
    }
    var id = form.getAttribute("data-ticket-id");
    return id ? "/api/tickets/" + id + "/files" : "";
  }

  function uploadSettled() {
    if (!uploadClaimedDraft) {
      return;
    }
    uploadClaimedDraft = false;
    draftCreating = false;
    if (saveDeferred) {
      saveDeferred = false;
      save();
    }
  }

  if (Sym.upload) {
    uploader = Sym.upload.init({
      form: form,
      zone: description ? description.closest(".field") : form,
      list: document.getElementById("editor-files"),
      picker: document.getElementById("description-file"),
      button: document.getElementById("description-attach"),
      getTarget: uploadTarget,
      onUploaded: function (data) {
        if (data && data.id && !created) {
          adoptDraft(data);
        }
      },
      onSettled: uploadSettled,
      onBusyChange: onUploadBusy,
    });
  }

  applyUploadState();

  window.addEventListener("online", retry);

  // A Draft whose fields changed in another tab is loaded fresh on the next
  // open; last save wins while this page is open.
})();
