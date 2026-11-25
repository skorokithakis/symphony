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
  var repoNewUrl = document.getElementById("repo_new_url");
  var repoNewName = document.getElementById("repo_new_name");
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
    var repoSelect = form.querySelector('select[name="repo"]');
    var newRepoUrl = form.querySelector('input[name="repo_new_url"]');
    var newRepoName = form.querySelector('input[name="repo_new_name"]');
    var modelSelect = form.querySelector('select[name="model"]');
    var selectedRepo = repoSelect ? repoSelect.value : "";
    var newUrl = newRepoUrl ? newRepoUrl.value.trim() : "";
    return {
      title: title ? title.value : "",
      description: description ? description.value : "",
      // "Add a repository…" has an empty value: the typed value is the repo.
      repo: selectedRepo === "" ? newUrl : selectedRepo,
      repo_new_url: newUrl,
      repo_new_name: newRepoName ? newRepoName.value.trim() : "",
      model: modelSelect ? modelSelect.value : "",
    };
  }

  /* The repository and model do not count as content: /new preselects a
   * repository, so counting it would create a textless Draft on the first
   * autosave.  Files create the Draft through the upload path instead. */
  function empty(payload) {
    return !payload.title.trim() && !payload.description.trim();
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
    /* A recording blocks Start and Discard: the submit guard dictation.js puts
     * on the form would swallow their submits anyway, and a disabled button
     * says so.  This runs again when a recording starts and ends. */
    var blocked = uploadBusy || submitting || Sym.dictation.active;
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
          // Replace, not assign: the editor URL of a started ticket redirects
          // to the ticket page, so leaving it in history made Back look like
          // it did nothing.  Back now goes to the page before the editor.
          window.location.replace(data.redirect);
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
        // Replace, not assign: the discarded Draft's editor URL is a 404 now,
        // so it must not stay in history for Back to reach.
        window.location.replace((data && data.redirect) || "/");
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

  [title, description, repo, repoNewUrl, repoNewName].forEach(function (field) {
    if (field) {
      field.addEventListener("input", schedule);
    }
  });
  form.querySelectorAll('select[name="repo"], select[name="model"]').forEach(
    function (select) {
      select.addEventListener("change", schedule);
    }
  );

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

  /* Ctrl+Enter (Cmd+Enter on a Mac) in the description starts the ticket, the
   * same as the reply composer on the ticket page.  Clicking the button rather
   * than calling doStart keeps this on the one submit path: formaction, the
   * upload-busy guard and the disabled state all behave as they do for a real
   * click. */
  if (description) {
    description.addEventListener("keydown", function (event) {
      /* Enter confirms an IME candidate, so it must not start the ticket
         mid-composition. */
      if (event.isComposing) {
        return;
      }
      if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) {
        return;
      }
      event.preventDefault();
      if (startButton && !startButton.disabled) {
        startButton.click();
      }
    });
  }

  if (dialog) {
    var confirmForm = dialog.querySelector("form");
    if (confirmForm) {
      confirmForm.addEventListener("submit", discard);
    }
  }

  /* ---------- Ctrl+K: the repository picker ----------
   *
   * A filtered view over the existing #repo <select>; the select stays the
   * source of truth, so the no-JavaScript form and the phone's native picker
   * are untouched.  Picking sets the option and dispatches a real change
   * event, which is what makes the autosave and the "new repository" field
   * toggle run exactly as they do for a pick in the select itself. */
  var picker = document.getElementById("repo-picker");
  var pickerInput = document.getElementById("repo-picker-input");
  var pickerList = document.getElementById("repo-picker-list");

  if (picker && pickerInput && pickerList && repo && repo.tagName === "SELECT") {
    var pickerEntries = [];
    var pickerVisible = [];
    var pickerActive = -1;
    var pickerReturnTo = null;

    /* In-order subsequence match, so "acb" matches "a-c-b".  Caller folds the
       case: both sides are lowercased once, not per character. */
    function isSubsequence(text, query) {
      var from = 0;
      for (var i = 0; i < query.length; i += 1) {
        from = text.indexOf(query.charAt(i), from);
        if (from === -1) {
          return false;
        }
        from += 1;
      }
      return true;
    }

    /* 0 prefix, 1 contiguous substring, 2 other subsequence, -1 no match.
       The name and the URL score the same; only the display order differs. */
    function matchRank(name, url, query) {
      if (name.indexOf(query) === 0 || url.indexOf(query) === 0) {
        return 0;
      }
      if (name.indexOf(query) !== -1 || url.indexOf(query) !== -1) {
        return 1;
      }
      if (isSubsequence(name, query) || isSubsequence(url, query)) {
        return 2;
      }
      return -1;
    }

    function pickerMatches() {
      var query = pickerInput.value.trim().toLowerCase();
      var add = null;
      var ranked = [];
      pickerEntries.forEach(function (entry, index) {
        if (entry.isAdd) {
          add = entry;
          return;
        }
        if (!query) {
          ranked.push({ entry: entry, rank: 0, index: index });
          return;
        }
        var rank = matchRank(
          entry.name.toLowerCase(),
          entry.url.toLowerCase(),
          query
        );
        if (rank !== -1) {
          ranked.push({ entry: entry, rank: rank, index: index });
        }
      });
      if (query) {
        ranked.sort(function (left, right) {
          /* Select order within a rank, so recently used repositories keep
             their place ahead of the A-to-Z list. */
          return left.rank - right.rank || left.index - right.index;
        });
      }
      var matches = ranked.map(function (item) {
        return item.entry;
      });
      /* "Add a repository…" is the escape hatch, so it stays reachable no
         matter what the filter says. */
      if (add) {
        matches.push(add);
      }
      return matches;
    }

    function setPickerActive(index) {
      var rows = pickerList.children;
      if (pickerActive >= 0 && rows[pickerActive]) {
        rows[pickerActive].setAttribute("aria-selected", "false");
      }
      pickerActive = index;
      if (index >= 0 && rows[index]) {
        rows[index].setAttribute("aria-selected", "true");
        pickerInput.setAttribute("aria-activedescendant", rows[index].id);
        rows[index].scrollIntoView({ block: "nearest" });
      } else {
        pickerInput.removeAttribute("aria-activedescendant");
      }
    }

    function renderPicker() {
      pickerVisible = pickerMatches();
      pickerList.textContent = "";
      pickerVisible.forEach(function (entry, index) {
        var row = document.createElement("li");
        row.className = "repo-picker-option";
        row.id = "repo-picker-option-" + index;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", "false");
        var name = document.createElement("span");
        name.className = "repo-picker-name";
        /* The server's option text, not a second label that could drift. */
        name.textContent = entry.name;
        row.appendChild(name);
        if (!entry.isAdd && entry.url !== entry.name) {
          var url = document.createElement("span");
          url.className = "repo-picker-url";
          url.textContent = entry.url;
          row.appendChild(url);
        }
        row.addEventListener("click", function () {
          choosePickerEntry(index);
        });
        pickerList.appendChild(row);
      });
      setPickerActive(pickerVisible.length ? 0 : -1);
    }

    function closePicker() {
      if (typeof picker.close === "function") {
        picker.close();
      } else {
        picker.removeAttribute("open");
      }
    }

    function choosePickerEntry(index) {
      var entry = pickerVisible[index];
      if (!entry) {
        return;
      }
      /* Setting the option does not fire a change event, so both the
         autosave listener and app.js's new-repository toggle need this one
         dispatched event to run. */
      entry.option.selected = true;
      repo.dispatchEvent(new Event("change", { bubbles: true }));
      closePicker();
      if (entry.isAdd) {
        /* The URL is what the user must type next; the name is optional. */
        repoNewUrl.focus();
      } else {
        if (pickerReturnTo && typeof pickerReturnTo.focus === "function") {
          pickerReturnTo.focus();
        }
        /* Picking an existing repository hides the new-repository fields, so
           an opener that was one of them has nowhere to take focus.  The
           select is what the picker acted on, so it is the fallback. */
        if (document.activeElement !== pickerReturnTo) {
          repo.focus();
        }
      }
    }

    function openPicker() {
      pickerReturnTo = document.activeElement;
      /* A recent repository is listed twice in the select (in "Recent" and in
         "All repositories").  Keep the first copy only, so the picker shows
         each repository once, with the recent ones first. */
      var seenUrls = new Set();
      pickerEntries = Array.prototype.filter
        .call(repo.options, function (option) {
          if (seenUrls.has(option.value)) {
            return false;
          }
          seenUrls.add(option.value);
          return true;
        })
        .map(function (option) {
          return {
            option: option,
            name: option.textContent.trim(),
            url: option.value,
            isAdd: option.value === "",
          };
        });
      pickerInput.value = "";
      renderPicker();
      if (typeof picker.showModal === "function") {
        picker.showModal();
      } else {
        picker.setAttribute("open", "");
      }
      pickerInput.focus();
    }

    document.addEventListener("keydown", function (event) {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) {
        return;
      }
      if (event.key.toLowerCase() !== "k") {
        return;
      }
      /* Even with another dialog open, swallow the browser's own Ctrl+K (the
         search bar in Firefox) instead of letting it pull focus out of a
         modal.  The help, discard and picker dialogs all count. */
      event.preventDefault();
      if (document.querySelector("dialog[open]")) {
        return;
      }
      openPicker();
    });

    pickerInput.addEventListener("input", renderPicker);

    pickerInput.addEventListener("keydown", function (event) {
      /* During an IME composition Enter confirms the candidate and the arrows
         walk the candidate list, so handling them here would pick a repository
         while the user is still composing the query. */
      if (event.isComposing) {
        return;
      }
      var count = pickerVisible.length;
      if (count === 0) {
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        var step = event.key === "ArrowDown" ? 1 : -1;
        setPickerActive((pickerActive + step + count) % count);
      } else if (event.key === "Enter") {
        event.preventDefault();
        choosePickerEntry(pickerActive);
      }
    });

    /* The same backdrop click the help dialog has: a click on the backdrop is
       the one whose target is the dialog itself, but so is a click on the
       dialog's own padding, hence the position check. */
    picker.addEventListener("click", function (event) {
      if (event.target !== picker) {
        return;
      }
      var rect = picker.getBoundingClientRect();
      if (
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom
      ) {
        return;
      }
      closePicker();
    });
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
  /* dictation.js reports a start and an end; Start and Discard follow it. */
  Sym.dictation.onChange = applyUploadState;

  window.addEventListener("online", retry);

  // A Draft whose fields changed in another tab is loaded fresh on the next
  // open; last save wins while this page is open.
})();
