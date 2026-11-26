/* The ticket page: thread polling, the reply autosave, the composer's large
 * mode and the fetch-backed Send, Retry and state actions.
 *
 * Two rules this file must never break:
 *  - a poll may append entries and replace the action bar, but it must never
 *    write into the reply textarea;
 *  - an entry that scrolls in while the reader is scrolled up is announced
 *    with the .jump control, not scrolled into view. */
(function () {
  "use strict";

  var Sym = window.Sym;
  var main = document.getElementById("ticket");
  if (!Sym || !main) {
    return;
  }

  var POLL_INTERVAL = 3000;
  var DRAFT_INTERVAL = 800;
  /* A page counts as watched for this long after the last interaction, as
   * long as the tab is visible and the window has focus.  The poll then sends
   * active=1, and the server suppresses the Needs Input push for a ticket
   * whose page is already on screen. */
  var ACTIVE_INTERVAL = 10 * 60 * 1000;

  var thread = document.getElementById("thread");
  var titleEl = document.getElementById("ticket-title");
  var actionsEl = document.getElementById("composer-actions");
  var formsEl = document.getElementById("state-forms");
  var composer = document.getElementById("composer");
  var reply = document.getElementById("reply");
  var jump = document.getElementById("jump");
  var sizeButton = document.getElementById("composer-size");
  var compactBar = document.getElementById("thead-compact");
  var realHead = main.querySelector(".thead");

  var pollUrl = main.getAttribute("data-api-poll");
  var seenUrl = main.getAttribute("data-api-seen");
  var replyUrl = main.getAttribute("data-api-reply");
  var retryUrl = main.getAttribute("data-api-retry");
  var draftUrl = main.getAttribute("data-api-draft");
  var uploadBase = main.getAttribute("data-api-upload");

  var baseTitle = document.title.replace(/^\(\d+\)\s*/, "");
  var lastCommentId = thread.getAttribute("data-last-comment-id") || "";
  var seenCommentId = thread.getAttribute("data-seen-comment-id") || "";
  var hasComposer = Boolean(composer);
  var pendingList = document.getElementById("pending-files");

  var uploader = null;
  var uploadBusy = false;
  /* The last Send state the server rendered (disabled while the agent works),
   * kept separately so a busy upload can disable Send without re-enabling it
   * afterwards. */
  var stateSendDisabled = false;
  var baseSendReason = "";

  // Fragments last written to the DOM, so an unchanged poll is a no-op.
  var stateHtml = null;
  var actionsHtml = null;
  var formsHtml = null;
  var workingHtml = null;

  var timer = null;
  var polling = false;
  var pollQueued = false;
  /* Opening the page counts as the first interaction; it then decays with
   * everything else. */
  var lastActivity = Date.now();
  var sending = false;
  var sendInFlight = null;
  /* The first text sent with the current token.  A send whose response was
   * lost is retried with the same token; when the server reports the retry as
   * a duplicate, this is the text that actually committed. */
  var sendAttempt = null;
  /* Set once the user picks a model, so a poll never overwrites a deliberate
   * choice with the model another tab stored. */
  var modelTouched = false;
  var draftTimer = null;
  var draftDirty = false;
  var draftInFlight = null;
  var draftRunning = false;
  var draftQueued = false;

  /* ---------- scroll ---------- */

  /* At 800px and up the app fills the window and <main> is the scroll
   * container; below that the window scrolls.  Read the layout on every call:
   * it changes with the width. */
  function scrollElement() {
    var style = window.getComputedStyle(main);
    if (style.overflowY === "auto" || style.overflowY === "scroll") {
      return main;
    }
    return document.scrollingElement || document.documentElement;
  }

  function nearBottom() {
    var el = scrollElement();
    return el.scrollTop + el.clientHeight >= el.scrollHeight - 120;
  }

  function scrollToBottom() {
    var el = scrollElement();
    el.scrollTop = el.scrollHeight;
    if (jump) {
      jump.hidden = true;
    }
  }

  function showJump(show) {
    if (jump) {
      jump.hidden = !show;
    }
  }

  /* The page opens at the first unread entry, or at the end. */
  function initialScroll() {
    var badge = thread.querySelector(".new");
    var target = badge
      ? badge.closest(".entry, .notice, .alert")
      : null;
    if (target) {
      target.scrollIntoView({ block: "start" });
    } else {
      scrollToBottom();
    }
  }

  /* ---------- compact header bar ---------- */

  /* Below 800px the window scrolls, which is also when this bar has anything
   * to do: at 1240px the layout pins the header already, and between 800px and
   * 1240px it stays in view.  Scrolling up by a gesture's worth reveals the
   * bar; scrolling down hides it.  The reveal threshold accumulates, because
   * a slow scroll reports a couple of pixels at a time.  Programmatic scrolls
   * (the opening position, Jump to latest, appended entries) only move down,
   * so they hide the bar instead of revealing it.
   *
   * A viewport resize is not a gesture either.  Closing the Android keyboard
   * grows the viewport (the page opts into interactive-widget=resizes-content),
   * the browser clamps scrollY upward at the bottom of the page, and the clamp
   * looks exactly like a scroll up.  Its scroll event can arrive before the
   * resize event, so each scroll event compares the viewport against the size
   * recorded at the previous scroll event instead of trusting one reset on
   * resize. */
  var COMPACT_REVEAL_PX = 10;
  var compactLastTop = 0;
  var compactUp = 0;
  var compactViewportWidth = window.innerWidth;
  var compactViewportHeight = window.innerHeight;

  function showCompactBar(show) {
    if (!compactBar) {
      return;
    }
    if (compactBar.getAttribute("data-shown") === (show ? "true" : "false")) {
      return;
    }
    compactBar.setAttribute("data-shown", show ? "true" : "false");
    // Inert is what makes the hidden bar unreachable: visibility alone would
    // still let a script focus the link.
    compactBar.toggleAttribute("inert", !show);
  }

  function onScroll() {
    var top = window.scrollY || 0;
    if (
      window.innerWidth !== compactViewportWidth ||
      window.innerHeight !== compactViewportHeight
    ) {
      // The page moved because the viewport did: adopt the new position and
      // leave the bar as it was.
      compactViewportWidth = window.innerWidth;
      compactViewportHeight = window.innerHeight;
      compactLastTop = top;
      compactUp = 0;
    } else {
      var delta = top - compactLastTop;
      compactLastTop = top;
      if (delta > 0) {
        compactUp = 0;
        showCompactBar(false);
      } else if (delta < 0) {
        compactUp -= delta;
        if (compactUp >= COMPACT_REVEAL_PX) {
          showCompactBar(true);
        }
      }
    }
    // No bar while the real header is still on screen: it would cover it and
    // say the same thing twice.
    if (realHead && realHead.getBoundingClientRect().bottom > 0) {
      showCompactBar(false);
    }
  }

  /* ---------- seen marker ---------- */

  function markSeen(commentId) {
    if (!commentId || commentId === seenCommentId || document.hidden) {
      return;
    }
    Sym.postJSON(seenUrl, { comment_id: commentId })
      .then(function () {
        seenCommentId = commentId;
      })
      .catch(function () {
        /* Retried by the next poll; nothing else depends on it. */
      });
  }

  /* ---------- the reply draft ---------- */

  function scheduleDraft() {
    draftDirty = true;
    window.clearTimeout(draftTimer);
    draftTimer = window.setTimeout(saveDraft, DRAFT_INTERVAL);
  }

  /* One save at a time: a second request would race the first and could
   * store an older text after a newer one.  A request that arrives while one
   * is in flight is queued, and only the latest content is sent.  Resolves
   * true when there was nothing to save or the save succeeded, and false when
   * it failed, which app.js's flush hook uses to stay on the page. */
  function saveDraft() {
    window.clearTimeout(draftTimer);
    if (!reply || !draftDirty) {
      return Promise.resolve(true);
    }
    if (sending) {
      // No autosave while a Send is being submitted; its finally flushes it.
      return Promise.resolve(true);
    }
    if (draftRunning) {
      draftQueued = true;
      return Promise.resolve(true);
    }
    var text = reply.value;
    draftDirty = false;
    draftRunning = true;
    draftInFlight = Sym.postJSON(draftUrl, { text: text })
      .then(function (data) {
        Sym.showSaved("saved", data.saved_at);
        if (reply.value !== text) {
          draftDirty = true;
          scheduleDraft();
        }
        return true;
      })
      .catch(function () {
        draftDirty = true;
        Sym.showNotSaved("saved");
        return false;
      });
    var attempt = draftInFlight;
    // Clear the guard whether the save succeeded or failed, so the next poll
    // or "online" event can retry a failed save, and run one queued save.
    attempt.then(function () {
      if (draftInFlight === attempt) {
        draftInFlight = null;
      }
      draftRunning = false;
      if (draftQueued) {
        draftQueued = false;
        saveDraft();
      }
    });
    return attempt;
  }

  function retryDraft() {
    if (draftDirty && !draftInFlight) {
      saveDraft();
    }
  }

  /* app.js's Ctrl+/ navigates away, so every keystroke since the last save
   * must reach the server first: an in-flight Send is allowed to finish, any
   * in-flight save is awaited, and the latest text is saved until nothing is
   * dirty.  Resolves false when a save fails, which keeps app.js on the page
   * instead of losing the text, and shows the usual page error.  A page
   * without the composer has nothing to flush. */
  Sym.flushDraft = function () {
    if (!reply) {
      return Promise.resolve(true);
    }
    function step() {
      if (sending) {
        return (sendInFlight || Promise.resolve()).then(step);
      }
      if (draftInFlight) {
        return draftInFlight.then(function (saved) {
          return saved ? step() : false;
        });
      }
      if (!draftDirty) {
        return true;
      }
      return saveDraft().then(function (saved) {
        return saved ? step() : false;
      });
    }
    // step() returns a plain true when nothing is dirty, so wrap it.
    return Promise.resolve(step()).then(function (saved) {
      if (!saved) {
        Sym.showError("Could not save your reply. Try again.");
      }
      return saved;
    });
  };

  /* ---------- sending ---------- */

  function sendButton() {
    return document.getElementById("act-send");
  }

  function retryButton() {
    return document.getElementById("act-retry");
  }

  function modelSelect() {
    return document.getElementById("composer-model");
  }

  /* The chosen Model alias, or null when the page has no select (no aliases
   * configured).  null is never sent: the server reads an absent field as
   * "leave the stored model alone" rather than as Default. */
  function chosenModel() {
    var select = modelSelect();
    return select ? select.value : null;
  }

  function reasonElement() {
    return document.getElementById("send-reason");
  }

  function captureServerSendState() {
    var button = sendButton();
    if (button) {
      stateSendDisabled = button.disabled;
    }
    var reason = reasonElement();
    baseSendReason = reason ? reason.textContent : "";
  }

  function applyBusyState() {
    /* Dictation owns the state actions and Retry: Send is what ends a
     * recording and posts the final text, so it keeps its own rule.  The poll
     * replaces the actions fragment, which is why this runs after every
     * replacement and again whenever dictation.js reports a start or an end,
     * reading the buttons currently in the DOM rather than a snapshot. */
    var dictating = Sym.dictation.active;
    var busy = uploadBusy || sending;
    // The state actions are only ever gated by a recording: the server renders
    // them enabled, while Send, the one control with a server-side disabled
    // state, is skipped here and handled below.
    if (actionsEl) {
      actionsEl
        .querySelectorAll("button[type=submit]")
        .forEach(function (control) {
          if (control.id !== "act-send") {
            control.disabled = dictating;
          }
        });
    }
    var button = sendButton();
    if (button) {
      button.disabled = busy || stateSendDisabled;
    }
    // The select follows Send's rule.  Send's server-rendered disabled state
    // lives in the actions fragment the poll replaces; the select sits outside
    // it, so this is what carries a state change over to it.
    var select = modelSelect();
    if (select) {
      select.disabled = busy || stateSendDisabled;
    }
    var retry = retryButton();
    if (retry) {
      retry.disabled = busy || dictating;
    }
    var reason = reasonElement();
    if (!reason) {
      return;
    }
    if (uploadBusy) {
      reason.hidden = false;
      reason.textContent = "Files are still uploading.";
    } else if (baseSendReason) {
      reason.hidden = false;
      reason.textContent = baseSendReason;
    } else {
      reason.hidden = true;
      reason.textContent = "";
    }
  }

  function onUploadBusy(busy) {
    uploadBusy = busy;
    applyBusyState();
  }

  function sendReply(event) {
    if (event) {
      event.preventDefault();
    }
    if (!reply || sending) {
      return;
    }
    var button = sendButton();
    if (button && button.disabled) {
      return;
    }
    /* Send is usable while dictation is recording: it stops the recording and
     * posts once Soniox has delivered the final text, so the send never races
     * the transcription.  `sending` covers the wait as well, which is what
     * makes a second Send, or Ctrl+Enter, during the wait a no-op.  A failed
     * recording rejects: its error is already shown, the confirmed text stays
     * in the box, and no reply is posted. */
    var finishing =
      typeof Sym.finishDictation === "function" ? Sym.finishDictation() : null;
    if (!finishing) {
      submitReply();
      return;
    }
    sending = true;
    applyBusyState();
    sendInFlight = finishing.then(
      function () {
        sending = false;
        sendInFlight = null;
        applyBusyState();
        submitReply();
      },
      function () {
        sending = false;
        sendInFlight = null;
        applyBusyState();
      },
    );
  }

  /* The normal Send path: every rule applies to the text as it is once any
   * dictation has finished. */
  function submitReply() {
    var button = sendButton();
    if (button && button.disabled) {
      return;
    }
    var text = reply.value;
    if (!text.trim()) {
      Sym.showError("Write a reply first.");
      reply.focus();
      return;
    }
    if (uploadBusy) {
      // A second file can still be queued or a remove can still be in flight.
      Sym.showError("Files are still uploading.");
      return;
    }
    sending = true;
    if (uploader) {
      // No attachment may change while the reply that names them is in flight.
      uploader.setLocked(true);
    }
    applyBusyState();
    window.clearTimeout(draftTimer);
    draftDirty = false;
    draftQueued = false;
    var model = chosenModel();
    var tokenField = composer.querySelector('input[name="token"]');
    var token = tokenField ? tokenField.value : "";
    // Keep the first text for this token across retries, so a duplicate
    // response can tell us whether the textarea still holds what committed.
    var firstText =
      sendAttempt && sendAttempt.token === token ? sendAttempt.text : text;
    sendAttempt = { token: token, text: firstText };

    // Let an in-flight autosave finish first: the server clears the draft as
    // part of sending, and a late save must not put the sent text back.
    var wait = draftInFlight || Promise.resolve();
    sendInFlight = wait
      .then(function () {
        var payload = {
          body: text,
          token: tokenField ? tokenField.value : "",
        };
        if (model !== null) {
          payload.model = model;
        }
        return Sym.postJSON(replyUrl, payload);
      })
      .then(function (data) {
        // On a duplicate, the committed reply is the first attempt's text,
        // not necessarily this request's body: clear only when the textarea
        // still holds exactly that.  Otherwise the edited text stays for the
        // user to send again with the fresh token.
        var committed = data && data.duplicate ? firstText : text;
        if (reply.value === committed) {
          reply.value = "";
          Sym.hideSaved("saved");
        } else {
          draftDirty = true;
        }
        if (pendingList) {
          pendingList.innerHTML = "";
          pendingList.hidden = true;
        }
        if (tokenField && data && data.token) {
          // Never reuse an accepted token: the next reply would be dropped as
          // a duplicate of this one.
          tokenField.value = data.token;
        }
        sendAttempt = null;
        Sym.clearError();
        poll();
      })
      .catch(function (error) {
        Sym.showError(error.message);
        draftDirty = true;
      })
      .finally(function () {
        sending = false;
        sendInFlight = null;
        if (uploader) {
          uploader.setLocked(false);
        }
        applyBusyState();
        if (draftDirty) {
          saveDraft();
        }
      });
  }

  /* Retry posts the fixed "Retry." comment instead of the composer text.  It
   * uses its own token field and rotates only that one: if a Retry response
   * is lost, the next Send must not reuse Retry's token (the server would
   * report it as a duplicate and the browser would clear a reply that never
   * posted), and the reverse must not happen either.  Double taps are still
   * blocked by the shared sending flag and the disabled button.  The textarea
   * and pending files are deliberately untouched. */
  function retryReply(event) {
    if (event) {
      event.preventDefault();
    }
    if (!composer || sending) {
      return;
    }
    var button = retryButton();
    if (button && button.disabled) {
      return;
    }
    var tokenField = composer.querySelector('input[name="retry_token"]');
    var payload = { retry_token: tokenField ? tokenField.value : "" };
    var model = chosenModel();
    if (model !== null) {
      payload.model = model;
    }
    sending = true;
    applyBusyState();
    sendInFlight = Sym.postJSON(retryUrl, payload)
      .then(function (data) {
        if (tokenField && data && data.retry_token) {
          tokenField.value = data.retry_token;
        }
        Sym.clearError();
        poll();
      })
      .catch(function (error) {
        Sym.showError(error.message);
      })
      .finally(function () {
        sending = false;
        sendInFlight = null;
        applyBusyState();
        if (draftDirty) {
          saveDraft();
        }
      });
  }

  /* ---------- poll ---------- */

  function noteActivity() {
    lastActivity = Date.now();
  }

  function pageActive() {
    return (
      !document.hidden &&
      document.hasFocus() &&
      Date.now() - lastActivity < ACTIVE_INTERVAL
    );
  }

  function appendEntries(html) {
    var wasAtBottom = nearBottom();
    var working = document.getElementById("working");
    if (working) {
      working.insertAdjacentHTML("beforebegin", html);
    } else {
      thread.insertAdjacentHTML("beforeend", html);
    }
    Sym.formatTimes(thread);
    if (wasAtBottom) {
      scrollToBottom();
    } else {
      showJump(true);
    }
  }

  /* The compact bar holds a copy of the state tag with its own id, so the poll
   * cannot patch it with data.state_html: that fragment carries the real id,
   * and two elements may not share one.  Copying the rendered class and label
   * keeps the two in step without parsing the fragment.  The Needs Input dot
   * is not copied: the poll updates every [data-needs-input-dot] itself. */
  function syncCompactState() {
    var source = document.getElementById("state-tag");
    var copy = document.getElementById("compact-state-tag");
    if (!source || !copy) {
      return;
    }
    copy.className = source.className;
    copy.textContent = source.textContent;
  }

  function apply(data) {
    baseTitle = data.page_title || baseTitle;
    Sym.setTitle(baseTitle, data.needs_input_count || 0);

    if (Boolean(data.has_composer) !== hasComposer) {
      // Done gained or lost its composer: the footer changes shape, and
      // patching it would mean re-rendering the textarea.
      window.location.reload();
      return;
    }

    if (data.title && titleEl && titleEl.textContent !== data.title) {
      titleEl.textContent = data.title;
    }
    var compactTitle = document.getElementById("compact-title");
    if (data.title && compactTitle && compactTitle.textContent !== data.title) {
      compactTitle.textContent = data.title;
    }

    // The Model row is the stored model; a Send that changed it should show
    // here without a reload.
    var modelEl = document.getElementById("ticket-model");
    if (modelEl && data.model && modelEl.textContent !== data.model) {
      modelEl.textContent = data.model;
    }

    // The composer's select follows the stored model too, unless the user has
    // picked one of their own: a deliberate choice is not overwritten.
    var selectEl = modelSelect();
    if (selectEl && !modelTouched) {
      var storedModel = data.composer_model || "";
      if (selectEl.value !== storedModel) {
        selectEl.value = storedModel;
      }
    }

    if (data.state_html !== stateHtml) {
      var tag = document.getElementById("state-tag");
      if (tag) {
        tag.outerHTML = data.state_html;
      }
      stateHtml = data.state_html;
      syncCompactState();
    }

    if (actionsEl && data.actions_html !== actionsHtml) {
      actionsEl.innerHTML = data.actions_html;
      actionsHtml = data.actions_html;
      // Read the server's Send state and reason before busy overrides them;
      // otherwise a busy-disabled button would be remembered as the server's
      // state and stay disabled after the upload settled.
      captureServerSendState();
      applyBusyState();
    }

    if (formsEl && data.state_forms_html !== formsHtml) {
      formsEl.innerHTML = data.state_forms_html;
      formsHtml = data.state_forms_html;
    }

    if (data.working_html !== workingHtml) {
      workingHtml = data.working_html;
      var existing = document.getElementById("working");
      if (data.working_html) {
        if (existing) {
          existing.outerHTML = data.working_html;
        } else {
          thread.insertAdjacentHTML("beforeend", data.working_html);
        }
      } else if (existing) {
        existing.remove();
      }
    }

    // The cursor only moves forward, and only past entries this page has
    // actually rendered.  An empty delta on an unknown anchor must not move
    // it: that would skip or replay comments.
    if (data.entries_html) {
      appendEntries(data.entries_html);
      if (data.last_comment_id) {
        lastCommentId = data.last_comment_id;
      }
    } else if (!lastCommentId && data.last_comment_id) {
      lastCommentId = data.last_comment_id;
    }
    markSeen(lastCommentId);
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay === undefined ? POLL_INTERVAL : delay);
  }

  /* One poll at a time.  A timer tick, a visibility/online event and a
   * finished action all land here; two requests with the same cursor would
   * append the same comments twice.  A refresh requested while one is in
   * flight runs exactly once more when it returns. */
  function poll() {
    if (document.hidden) {
      schedule();
      return;
    }
    if (polling) {
      pollQueued = true;
      return;
    }
    polling = true;
    var url = pollUrl + "?after=" + encodeURIComponent(lastCommentId);
    if (pageActive()) {
      url += "&active=1";
    }
    Sym.getJSON(url)
      .then(function (data) {
        Sym.showConn(false);
        apply(data);
        // The server is reachable again: flush a save that failed offline.
        retryDraft();
      })
      .catch(function () {
        Sym.showConn(true);
      })
      .finally(function () {
        polling = false;
        if (pollQueued) {
          pollQueued = false;
          poll();
        } else {
          schedule();
        }
      });
  }

  /* ---------- actions ---------- */

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    if (form === composer) {
      // Retry is a submit button inside the composer form with its own
      // formaction; the submitter is what tells the two requests apart.
      if (event.submitter && event.submitter.id === "act-retry") {
        retryReply(event);
      } else {
        sendReply(event);
      }
      return;
    }
    var url = form.getAttribute("data-api-state");
    if (!url) {
      return;
    }
    event.preventDefault();
    /* A recording refuses every state change: the actions are disabled, so
     * this only catches a submit that got past them (assistive technology, or
     * a fragment rendered before the gate was applied). */
    if (Sym.dictation.active) {
      return;
    }
    var actionField = form.querySelector('input[name="action"]');
    var action = actionField ? actionField.value : "";
    Sym.postJSON(url, { action: action })
      .then(function () {
        Sym.clearError();
        poll();
      })
      .catch(function (error) {
        Sym.showError(error.message);
      });
  });

  if (reply) {
    reply.addEventListener("input", scheduleDraft);
    reply.addEventListener("keydown", function (event) {
      /* Enter confirms an IME candidate, so it must not send the reply
         mid-composition. */
      if (event.isComposing) {
        return;
      }
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        var button = sendButton();
        if (!button || !button.disabled) {
          sendReply();
        }
      }
    });
  }

  if (Sym.upload && composer && uploadBase) {
    uploader = Sym.upload.init({
      form: composer,
      list: pendingList,
      picker: document.getElementById("composer-file"),
      button: document.getElementById("composer-attach"),
      getTarget: function () {
        return uploadBase + "?target=reply";
      },
      onBusyChange: onUploadBusy,
    });
  }

  var initialModelSelect = modelSelect();
  if (initialModelSelect) {
    initialModelSelect.addEventListener("change", function () {
      modelTouched = true;
    });
  }
  captureServerSendState();
  applyBusyState();
  /* A recording blocks the state actions and Retry; dictation.js calls this
   * when one starts and when it ends, and re-deriving the gate from the live
   * buttons is what keeps a poll's fresh render blocked mid-recording. */
  Sym.dictation.onChange = applyBusyState;

  if (jump) {
    jump.addEventListener("click", scrollToBottom);
  }

  if (sizeButton) {
    sizeButton.addEventListener("click", function () {
      var large = main.getAttribute("data-composer") !== "large";
      // Large mode can make <main> stop scrolling (at 1240 px up it sets
      // overflow: hidden), so pick the container before switching.
      var scroller = scrollElement();
      if (large) {
        main.setAttribute("data-composer", "large");
      } else {
        main.removeAttribute("data-composer");
      }
      sizeButton.setAttribute("aria-pressed", large ? "true" : "false");
      sizeButton.setAttribute(
        "aria-label",
        large ? "Make the composer small" : "Make the composer large"
      );
      showJump(false);
      if (large) {
        scroller.scrollTop = 0;
      }
    });
  }

  /* What makes the page "watched" is interaction.  Only user-driven events
   * count: scroll is deliberately absent because programmatic scrolling
   * (initial positioning, appending entries) would keep renewing the timer on
   * an unattended page, and wheel/touchstart already cover real scrolling. */
  document.addEventListener("keydown", noteActivity);
  document.addEventListener("pointerdown", noteActivity);
  document.addEventListener("touchstart", noteActivity, { passive: true });
  document.addEventListener("wheel", noteActivity, { passive: true });

  window.addEventListener("online", function () {
    retryDraft();
    poll();
  });
  /* Only a real user scroll reaches this listener: window scroll happens only
   * in the phone layout, where <main> is not the scroll container. */
  window.addEventListener("scroll", onScroll, { passive: true });
  /* The baseline is also reset here so a resize that moves nothing still does
   * not leave a stale position behind.  The viewport size is deliberately not
   * recorded: the scroll event a clamped page fires may come next, and its
   * size comparison is what turns that jump into a non-gesture. */
  window.addEventListener("resize", function () {
    compactLastTop = window.scrollY || 0;
    compactUp = 0;
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      markSeen(lastCommentId);
      poll();
    }
  });

  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }
  initialScroll();
  // The opening scroll is downward or instant, never a gesture: start from
  // where the page settled so it cannot pass for one.
  compactLastTop = window.scrollY || 0;
  markSeen(lastCommentId);
  schedule(0);
})();
