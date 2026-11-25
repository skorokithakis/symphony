/* The ticket page: thread polling, the reply autosave, the composer's large
 * mode and the fetch-backed Send and state actions.
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

  var thread = document.getElementById("thread");
  var titleEl = document.getElementById("ticket-title");
  var actionsEl = document.getElementById("composer-actions");
  var formsEl = document.getElementById("state-forms");
  var composer = document.getElementById("composer");
  var reply = document.getElementById("reply");
  var jump = document.getElementById("jump");
  var sizeButton = document.getElementById("composer-size");

  var pollUrl = main.getAttribute("data-api-poll");
  var seenUrl = main.getAttribute("data-api-seen");
  var replyUrl = main.getAttribute("data-api-reply");
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
  var sending = false;
  var sendInFlight = null;
  /* The first text sent with the current token.  A send whose response was
   * lost is retried with the same token; when the server reports the retry as
   * a duplicate, this is the text that actually committed. */
  var sendAttempt = null;
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
    var button = sendButton();
    if (button) {
      button.disabled = uploadBusy || sending || stateSendDisabled;
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
        return Sym.postJSON(replyUrl, {
          body: text,
          token: tokenField ? tokenField.value : "",
        });
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

  /* ---------- poll ---------- */

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

    if (data.state_html !== stateHtml) {
      var tag = document.getElementById("state-tag");
      if (tag) {
        tag.outerHTML = data.state_html;
      }
      stateHtml = data.state_html;
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
      sendReply(event);
      return;
    }
    var url = form.getAttribute("data-api-state");
    if (!url) {
      return;
    }
    event.preventDefault();
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

  captureServerSendState();
  applyBusyState();

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

  window.addEventListener("online", function () {
    retryDraft();
    poll();
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
  markSeen(lastCommentId);
  schedule(0);
})();
