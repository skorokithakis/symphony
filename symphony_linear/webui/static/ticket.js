/* The ticket page: thread polling, the reply autosave, the composer's large
 * mode and the fetch-backed Send, Retry and state actions.
 *
 * Two rules this file must never break:
 *  - a poll may append entries and replace the action bar, but it must never
 *    write into the reply textarea;
 *  - an entry that scrolls in while the reader is scrolled up is announced
 *    with the .jump control, not scrolled into view.  A successful Send or
 *    Retry is the exception: the user acted, so the page goes to the bottom. */
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
  var stateMenuWrap = document.getElementById("state-menu-wrap");
  var formsEl = document.getElementById("state-forms");
  var composer = document.getElementById("composer");
  var reply = document.getElementById("reply");
  var jump = document.getElementById("jump");
  var sizeButton = document.getElementById("composer-size");
  var compactBar = document.getElementById("thead-compact");
  var realHead = main.querySelector(".thead");
  var questionCards = window.SymQuestions;

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
   * afterwards.  There is no Send at all on an In Progress ticket. */
  var stateSendDisabled = false;
  var retryFlag = main.getAttribute("data-retry-flag") === "1";

  /* Fragments compared across the initial DOM and the poll's raw template
     markup must be serialized the same way: the browser turns a bare
     attribute such as `hidden` into `hidden=""`, so comparing raw markup with
     innerHTML would re-render an unchanged fragment on the first poll. */
  var fragmentTemplate = document.createElement("template");
  function normalizedFragment(html) {
    fragmentTemplate.innerHTML = html;
    return fragmentTemplate.innerHTML;
  }

  // Fragments last written to the DOM, so an unchanged poll is a no-op.
  var stateHtml = null;
  var actionsHtml = null;
  var stateMenuHtml = stateMenuWrap
    ? normalizedFragment(stateMenuWrap.innerHTML)
    : null;
  var formsHtml = null;
  var workingHtml = null;

  /* The steps block's cursor and display state.  The turn identifier changes
   * when a new turn starts, and the block then starts its list over.  A turn
   * that ends clears all of it, so the next turn never shows stale steps. */
  var stepsTurn = "";
  var stepsLast = 0;
  var stepsExpanded = false;

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
    var payload = { text: text };
    /* The answers travel in the same request so the server never sees one
     * without the other.  An absent key means the text-only autosave, which
     * leaves stored answers alone; a card page always sends its snapshot, so
     * clearing the last answer also reaches the server. */
    var answers = questionCards.answers();
    if (answers) {
      payload.answers = answers;
    }
    draftDirty = false;
    draftRunning = true;
    draftInFlight = Sym.postJSON(draftUrl, payload)
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
    /* No Send means a turn is running: the server renders Stop alone on an In
     * Progress ticket.  Deriving the state from absence keeps the Model select
     * disabled for the turn and re-enables it when the poll swaps in a Needs
     * Input or QA action bar. */
    stateSendDisabled = button ? button.disabled : true;
  }

  function applyBusyState() {
    /* Dictation owns the state actions and Retry: Send is what ends a
     * recording and posts the final text, so it keeps its own rule.  The poll
     * replaces the action and menu fragments, which is why this runs after
     * every replacement and again whenever dictation.js reports a start or an
     * end, reading the buttons currently in the DOM rather than a snapshot. */
    var dictating = Sym.dictation.active;
    var busy = uploadBusy || sending;
    // The state actions are only ever gated by a recording: the server renders
    // them enabled, while Send, the one control with a server-side disabled
    // state, is skipped here and handled below.
    document
      .querySelectorAll(
        "#composer-actions button[type=submit], #state-menu button[type=submit]"
      )
      .forEach(function (control) {
        if (control.id !== "act-send") {
          control.disabled = dictating;
        }
      });
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
    // Retry is the error alert's own action now; every copy follows the same
    // rule whether or not the page currently shows it.
    document.querySelectorAll("[data-retry]").forEach(function (control) {
      control.disabled = busy || dictating;
    });
    var reason = reasonElement();
    if (!reason) {
      return;
    }
    /* The upload warning is the slot's only content now: the server renders it
     * empty and hidden in every state, and this is the one writer. */
    if (uploadBusy) {
      reason.hidden = false;
      reason.textContent = "Files are still uploading.";
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
    /* A missing Send means the ticket is In Progress: the daemon would
     * discard a reply posted now, so no entry point may start one. */
    var button = sendButton();
    if (!button || button.disabled) {
      return;
    }
    /* Send is usable while dictation is recording: it stops the recording and
     * posts once Soniox has delivered the final text, so the send never races
     * the transcription.  `sending` covers the wait as well, which is what
     * makes a second Send, or Ctrl+Enter, during the wait a no-op.  A failed
     * recording rejects: its error is already shown, the confirmed text stays
     * in the box, and no reply is posted.  submitReply rechecks Send after the
     * wait, because a poll can swap the action bar for Stop in between. */
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
    if (!button || button.disabled) {
      return;
    }
    var text = reply.value;
    var body = questionCards.comment(text);
    if (!body.trim()) {
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
    // The cards whose answers are in this body must not change mid-send: an
    // edit made now would be discarded when the commit retires them.
    questionCards.setLocked(true);
    applyBusyState();
    window.clearTimeout(draftTimer);
    draftDirty = false;
    draftQueued = false;
    var model = chosenModel();
    var tokenField = composer.querySelector('input[name="token"]');
    var token = tokenField ? tokenField.value : "";
    // Keep the first text and the reply it answered for this token across
    // retries, so a duplicate response retires the reply that actually
    // committed rather than whatever reply is on screen now.
    var currentReply = questionCards.activeReplyId();
    var firstText =
      sendAttempt && sendAttempt.token === token ? sendAttempt.text : text;
    var attemptReply =
      sendAttempt && sendAttempt.token === token
        ? sendAttempt.replyId
        : currentReply;
    sendAttempt = { token: token, text: firstText, replyId: attemptReply };

    // Let an in-flight autosave finish first: the server clears the draft as
    // part of sending, and a late save must not put the sent text back.
    var wait = draftInFlight || Promise.resolve();
    sendInFlight = wait
      .then(function () {
        /* A poll can swap Send for Stop while the autosave or a dictation
         * finishes.  Posting then would be a reply the daemon discards, so
         * check that Send still exists right before the request.  It is
         * disabled by this very send, so only its presence is meaningful. */
        if (!sendButton()) {
          sendAttempt = null;
          draftDirty = true;
          return null;
        }
        var payload = {
          body: body,
          token: tokenField ? tokenField.value : "",
        };
        if (model !== null) {
          payload.model = model;
        }
        return Sym.postJSON(replyUrl, payload);
      })
      .then(function (data) {
        if (data === null) {
          return;
        }
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
        // Retire only the answers that committed: a duplicate may name an
        // earlier reply, and a newer reply's unsent answers must survive.
        var committedReply = data && data.duplicate ? attemptReply : currentReply;
        questionCards.sent(committedReply);
        if (committedReply !== currentReply) {
          // This Send cancelled the autosave but consumed an older reply's
          // answers, so the visible reply's answers were never posted.  Save
          // them from the finally for the next visit.
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
        // The sender wants to see what they sent, even from far up the
        // thread.  The poll then appends it at the bottom and, finding the
        // page there, keeps it in view.
        scrollToBottom();
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
        questionCards.setLocked(false);
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
  function retryReply(event, button) {
    if (event) {
      event.preventDefault();
    }
    if (!composer || sending) {
      return;
    }
    /* The button may be a fresh one the poll appended: its disabled state can
     * be stale, so the live gates are checked here too. */
    if (uploadBusy || Sym.dictation.active) {
      return;
    }
    var alert = button ? button.closest(".alert--error") : null;
    if (alert && alert.hasAttribute("data-retried")) {
      return;
    }
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
        // This alert has had its retry; a poll that still reports the failed
        // turn must not offer it again.  A newer error alert is unmarked and
        // will offer it when the flag is true.
        if (alert) {
          alert.setAttribute("data-retried", "");
        }
        syncRetry();
        // Same as Send: the user should see the Retry comment they posted.
        scrollToBottom();
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
    // Entries land above the steps block, which sits above the working line:
    // what the agent said, then what it is doing now.
    var anchor =
      document.getElementById("steps") || document.getElementById("working");
    if (anchor) {
      anchor.insertAdjacentHTML("beforebegin", html);
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

  /* ---------- the running turn's steps ---------- */

  /* Last five unless the reader asked for all; the stylesheet does the
   * hiding, so a step arriving while the list is open needs no re-render. */
  var STEPS_PREVIEW = 5;

  function renderStepsControl() {
    var more = document.getElementById("steps-more");
    if (!more) {
      return;
    }
    // The control counts the lines still on the page, not the server's step
    // count: a step a later tool call replaced is not there to reveal.
    var count = stepsItemCount();
    // With five or fewer there is nothing to reveal, and once the reader asked
    // for everything the control has no other job: it is not a toggle, because
    // the open list stays open until the turn ends.
    more.hidden = stepsExpanded || count <= STEPS_PREVIEW;
    var label = "Show all " + count + " steps";
    if (more.textContent !== label) {
      more.textContent = label;
    }
  }

  function stepsItemCount() {
    var list = document.getElementById("steps-list");
    return list ? list.children.length : 0;
  }

  /* A run of consecutive tool calls shows as its newest call: every tool item
   * directly followed by another tool item is removed.  Scanned backwards so
   * an earlier call sees the surviving end of its run, not a removed sibling.
   * Text ends a run, and a replace starts from freshly rendered items. */
  function collapseToolRuns(list) {
    for (var index = list.children.length - 2; index >= 0; index -= 1) {
      var item = list.children[index];
      var next = list.children[index + 1];
      if (
        item.classList.contains("step--tool") &&
        next.classList.contains("step--tool")
      ) {
        item.remove();
      }
    }
  }

  function resetSteps() {
    var block = document.getElementById("steps");
    var list = document.getElementById("steps-list");
    if (list) {
      list.innerHTML = "";
    }
    if (block) {
      block.classList.remove("steps--all");
      block.hidden = true;
    }
    stepsTurn = "";
    stepsLast = 0;
    stepsExpanded = false;
    renderStepsControl();
  }

  function applySteps(data) {
    if (!data.working_html) {
      // The turn ended or the ticket left In Progress: its steps go with it.
      resetSteps();
      return;
    }
    var block = document.getElementById("steps");
    var list = document.getElementById("steps-list");
    if (!block || !list) {
      return;
    }
    // Same rule as the working line: keep a reader at the bottom there, never
    // move one who scrolled up.  Steps have no "Jump to latest" of their own.
    var wasAtBottom = nearBottom();
    if (data.steps_replace) {
      // A new turn, or a cursor too old to fill by appending.  A replace for
      // the same turn is a tab that slept past the window, so the reader's
      // "show all" choice still holds; a new turn starts collapsed.
      var sameTurn = Boolean(stepsTurn) && data.steps_turn === stepsTurn;
      list.innerHTML = data.steps_html;
      if (!sameTurn) {
        stepsExpanded = false;
        block.classList.remove("steps--all");
      }
    } else if (data.steps_html) {
      list.insertAdjacentHTML("beforeend", data.steps_html);
    } else {
      return;
    }
    collapseToolRuns(list);
    // The cursor follows the server's step numbers even when an item was just
    // removed: the page's own count is only for the control and the block.
    stepsTurn = data.steps_turn || "";
    stepsLast = data.steps_last || 0;
    block.hidden = stepsItemCount() === 0;
    renderStepsControl();
    if (wasAtBottom) {
      scrollToBottom();
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

  /* The state menu is a disclosure: one icon button toggles a small list of
   * the actions the server offers for the current state.  The list is
   * replaced by the poll, so its open state never outlives the state it
   * described.  The items stay out of the visibility check in app.js: a closed
   * menu still means the server offers that action, and the shortcut must not
   * depend on whether the list is painted. */
  function closeStateMenu(focusButton) {
    var menu = document.getElementById("state-menu");
    var button = document.getElementById("state-menu-button");
    if (!menu || menu.hidden) {
      return;
    }
    menu.hidden = true;
    if (button) {
      button.setAttribute("aria-expanded", "false");
      if (focusButton) {
        button.focus();
      }
    }
  }

  function toggleStateMenu() {
    var menu = document.getElementById("state-menu");
    var button = document.getElementById("state-menu-button");
    if (!menu || !button) {
      return;
    }
    var open = menu.hidden;
    menu.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = menu.querySelector("button");
      if (first) {
        first.focus();
      }
    }
  }

  /* Retry is offered by the newest error alert only.  Entries are appended,
   * never re-rendered, so an alert that used to be the newest keeps its
   * button; this runs after every poll and shows exactly one.  An alert that
   * has already been retried keeps its marker and never offers it again, even
   * if the daemon has not transitioned yet and the flag is still true; a
   * newer error appended later is unmarked and gets the button again. */
  function syncRetry() {
    var alerts = thread.querySelectorAll(".alert--error");
    var newest = alerts.length ? alerts[alerts.length - 1] : null;
    document.querySelectorAll("[data-retry]").forEach(function (button) {
      var offered =
        retryFlag &&
        newest &&
        button.closest(".alert--error") === newest &&
        !newest.hasAttribute("data-retried");
      button.hidden = !offered;
    });
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

    retryFlag = Boolean(data.retry);
    main.setAttribute("data-retry-flag", retryFlag ? "1" : "0");

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

    if (stateMenuWrap) {
      var nextMenu = normalizedFragment(data.state_menu_html);
      if (nextMenu !== stateMenuHtml) {
        // Replacing the wrapper closes any open menu and drops the old
        // state's items in one go; the new items are disabled again below if
        // a recording is running.
        stateMenuWrap.innerHTML = data.state_menu_html;
        stateMenuHtml = nextMenu;
        applyBusyState();
      }
    }

    if (data.working_html !== workingHtml) {
      workingHtml = data.working_html;
      // The indicator follows the same rule as appended entries: it keeps a
      // reader at the bottom there, and never moves one who scrolled up.
      var workingWasAtBottom = nearBottom();
      var existing = document.getElementById("working");
      if (data.working_html) {
        if (existing) {
          existing.outerHTML = data.working_html;
        } else {
          thread.insertAdjacentHTML("beforeend", data.working_html);
        }
        if (workingWasAtBottom) {
          scrollToBottom();
        }
      } else if (existing) {
        existing.remove();
      }
    }

    // After the working line, so a turn that just ended has already dropped
    // its steps from the page.
    applySteps(data);

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
    /* The poll can append an agent reply or a human entry, and it replaces the
     * action and menu bars.  Re-evaluate the active cards, the one Retry and
     * the busy/dictation gates after any of it, without a reload: an appended
     * error alert carries a fresh Retry button that has not been gated. */
    questionCards.refresh();
    syncRetry();
    applyBusyState();
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
    if (stepsTurn) {
      url +=
        "&turn=" + encodeURIComponent(stepsTurn) + "&step=" + stepsLast;
    }
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
      // Retry is a submit button in an error alert, associated with the
      // composer form; the submitter is what tells the two requests apart.
      if (event.submitter && event.submitter.hasAttribute("data-retry")) {
        retryReply(event, event.submitter);
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
    // The menu item that fired this is about to be replaced with the new
    // state's items; leaving it open would flash the old choices.
    closeStateMenu(false);
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

  /* The menu opens on its button and closes on an outside click or Escape.
     Type-to-reply is untouched: with the menu open a plain key still reaches
     app.js, which focuses the reply box as it does on any page. */
  document.addEventListener("click", function (event) {
    if (event.target.closest("#state-menu-button")) {
      event.preventDefault();
      toggleStateMenu();
      return;
    }
    if (!event.target.closest("#state-menu-wrap")) {
      closeStateMenu(false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var menu = document.getElementById("state-menu");
    if (menu && !menu.hidden) {
      event.preventDefault();
      closeStateMenu(true);
    }
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
        /* In Progress has no Send, and a reply there would be discarded by the
         * daemon: Ctrl+Enter is a no-op until Send exists and is enabled. */
        var button = sendButton();
        if (button && !button.disabled) {
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
  /* A card change is a draft change: the same debounce, queue and "Saved"
   * indicator cover the answers, which ride with the text in one request. */
  questionCards.onChange = scheduleDraft;
  questionCards.refresh();
  // The server already marked the initial newest error alert; this also
  // normalizes a page whose flag is false.
  syncRetry();
  /* A recording blocks the state actions and Retry; dictation.js calls this
   * when one starts and when it ends, and re-deriving the gate from the live
   * buttons is what keeps a poll's fresh render blocked mid-recording. */
  Sym.dictation.onChange = applyBusyState;

  if (jump) {
    jump.addEventListener("click", scrollToBottom);
  }

  /* "Show all N steps": reveal the whole list for the rest of the turn.  The
   * block is always in the page, so one listener at load is enough. */
  var stepsMore = document.getElementById("steps-more");
  if (stepsMore) {
    stepsMore.addEventListener("click", function () {
      var block = document.getElementById("steps");
      stepsExpanded = true;
      if (block) {
        block.classList.add("steps--all");
      }
      renderStepsControl();
    });
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

  /* A window that sw.js opened from a notification has only this ticket in
   * its history, so Android Back would leave the app.  A CloseWatcher catches
   * that first Back and replaces the page with the list, so the next Back
   * leaves the app as if it had opened on the list.  A history trick
   * (replaceState to "/" and pushState the ticket) was rejected: Chrome skips
   * entries that a page adds without a user tap, so Back would still exit.
   * The marker is removed at once so a reload does not arm this again.  On
   * desktop the watcher also takes the first unhandled Esc; that is accepted
   * because only notification windows get it.  Browsers without CloseWatcher
   * keep the default Back. */
  var pageParams = new URLSearchParams(window.location.search);
  if (pageParams.get("from") === "notification") {
    pageParams.delete("from");
    var remainingQuery = pageParams.toString();
    history.replaceState(
      history.state,
      "",
      window.location.pathname + (remainingQuery ? "?" + remainingQuery : "") + window.location.hash
    );
    if (typeof window.CloseWatcher === "function") {
      var backToList = new window.CloseWatcher();
      // Back can fire inside the autosave delay, so save the reply first, as
      // goToNextTicket in app.js does; a failed save stays on the ticket with
      // the error already shown.
      backToList.addEventListener("close", function () {
        Sym.flushDraft()
          .then(function (saved) {
            if (saved) {
              window.location.replace("/");
            }
          })
          .catch(function () {});
      });
    }
  }

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
