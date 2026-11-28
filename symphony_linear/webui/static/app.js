/* Progressive enhancement for the server-rendered pages.
 *
 * Everything here is optional: the forms work as plain posts, thread sections
 * toggle natively, and the server writes a UTC fallback for every timestamp.
 * The Content-Security-Policy only allows this file (and the page's own
 * script), never inline script.  No framework, no build step. */
(function () {
  "use strict";

  var Sym = (window.Sym = window.Sym || {});

  /* Whether a dictation recording is live, and the page's chance to react.  It
   * lives here rather than in dictation.js because that file only loads when
   * the daemon has a Soniox key, while the ticket and editor pages gate their
   * own submit controls on it.  Each page replaces ``onChange`` with its gating
   * function: the ticket page's poll replaces its action buttons, so only the
   * page can keep a fresh render blocked and then return the buttons to their
   * current state when the recording ends. */
  Sym.dictation = {
    active: false,
    onChange: function () {},
  };

  var dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  var timeFormatter = new Intl.DateTimeFormat(undefined, { timeStyle: "short" });
  var weekdayFormatter = new Intl.DateTimeFormat(undefined, { weekday: "short" });
  var dayMonthFormatter = new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
  });

  function parseDate(element) {
    var date = new Date(element.getAttribute("datetime"));
    return Number.isNaN(date.valueOf()) ? null : date;
  }

  function midnight(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  }

  /* "Now", "6 min", "2 h", "Yesterday", "Mon", "19 Nov" — the mock's row time.
     Day boundaries are the reader's local ones, which is why this cannot be
     computed on the server. */
  function relativeText(date, now) {
    var seconds = Math.floor((now.valueOf() - date.valueOf()) / 1000);
    if (seconds < 60) {
      return "Now";
    }
    var dayDiff = Math.round((midnight(now) - midnight(date)) / 86400000);
    if (dayDiff === 0) {
      if (seconds < 3600) {
        return Math.floor(seconds / 60) + " min";
      }
      return Math.floor(seconds / 3600) + " h";
    }
    if (dayDiff === 1) {
      return "Yesterday";
    }
    if (dayDiff < 7) {
      return weekdayFormatter.format(date);
    }
    return dayMonthFormatter.format(date);
  }

  Sym.formatTimes = function (root) {
    var scope = root || document;
    var now = new Date();
    scope.querySelectorAll("time[datetime]").forEach(function (element) {
      var date = parseDate(element);
      if (!date) {
        return;
      }
      var format = element.getAttribute("data-format");
      if (format === "relative") {
        element.textContent = relativeText(date, now);
      } else if (format === "time") {
        element.textContent = timeFormatter.format(date);
      } else {
        element.textContent = dateTimeFormatter.format(date);
      }
      element.title = dateTimeFormatter.format(date);
    });
  };

  /* The tab title is "(2) Tickets · symphony" when two tickets wait on the
     user.  Polls hand in the page's own title, so a changed ticket title is
     picked up too.  Every Needs Input badge follows the same count: the
     dock's and, on the ticket page, the one on the back link, so the existing
     polls keep them right without a second endpoint. */
  Sym.setTitle = function (baseTitle, needsInputCount) {
    document.title =
      needsInputCount > 0 ? "(" + needsInputCount + ") " + baseTitle : baseTitle;
    document.querySelectorAll("[data-needs-input-dot]").forEach(function (dot) {
      dot.hidden = !(needsInputCount > 0);
      var number = dot.querySelector("[data-needs-input-count]");
      if (number) {
        number.textContent = String(needsInputCount);
      }
    });
  };

  Sym.showConn = function (lost) {
    var conn = document.getElementById("conn");
    if (conn) {
      conn.hidden = !lost;
    }
  };

  Sym.showError = function (message) {
    var box = document.getElementById("page-error");
    var text = document.getElementById("page-error-text");
    if (!box || !text) {
      return;
    }
    text.textContent = message;
    box.hidden = false;
  };

  Sym.clearError = function () {
    var box = document.getElementById("page-error");
    if (box) {
      box.hidden = true;
    }
  };

  /* Every JSON request has a deadline.  A request that never settles would
   * otherwise hold the single-flight polling, autosave or Send forever; on
   * timeout it fails like any network error and the retry / "Connection
   * lost" paths take over.  File uploads do not use this helper: a 50 MB
   * upload on a phone may legitimately take longer. */
  var JSON_DEADLINE_MS = 20000;

  function fetchJSON(url, options) {
    var controller = new AbortController();
    options.signal = controller.signal;
    var timer = window.setTimeout(function () {
      controller.abort();
    }, JSON_DEADLINE_MS);
    return fetch(url, options)
      .catch(function (error) {
        if (error && error.name === "AbortError") {
          throw new Error("The request timed out.");
        }
        throw error;
      })
      .finally(function () {
        window.clearTimeout(timer);
      });
  }

  function describeError(response) {
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (payload) {
        if (payload && typeof payload.error === "string" && payload.error) {
          throw new Error(payload.error);
        }
        throw new Error("Request failed (" + response.status + ").");
      });
  }

  Sym.getJSON = function (url) {
    return fetchJSON(url, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    }).then(function (response) {
      if (!response.ok) {
        return describeError(response);
      }
      return response.json();
    });
  };

  Sym.postJSON = function (url, payload) {
    return fetchJSON(url, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      cache: "no-store",
      body: JSON.stringify(payload),
    }).then(function (response) {
      if (!response.ok) {
        return describeError(response);
      }
      return response.json();
    });
  };

  /* ---------- one-off behaviours, present only on their own pages ---------- */

  /* Copy buttons: the Details workspace path and session id, and each thread
   * entry's stored Markdown.  Delegated, so the entries the ticket page's poll
   * appends get a working button too; a per-button listener would miss them.
   * The button's original icon is captured once and restored after the check
   * mark, so clicking again during the feedback does not replace the check
   * with itself. */
  var copyRestore = new WeakMap();
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    var text = button && button.getAttribute("data-copy");
    if (!text || !navigator.clipboard) {
      return;
    }
    navigator.clipboard
      .writeText(text)
      .then(function () {
        var state = copyRestore.get(button);
        if (!state) {
          state = { icon: button.innerHTML, timer: 0 };
          copyRestore.set(button, state);
        }
        window.clearTimeout(state.timer);
        button.innerHTML =
          '<svg class="ic ic--sm" viewBox="0 0 20 20" aria-hidden="true">' +
          '<path d="M4 10.5l4 4 8-9"></path></svg>';
        state.timer = window.setTimeout(function () {
          button.innerHTML = state.icon;
        }, 1500);
      })
      .catch(function () {});
  });

  // A question-list link opens the section that holds its card.  Sections with
  // cards render open, but "Close all" or a tap can close them, and not every
  // browser opens a closed <details> for a fragment link.  This runs for every
  // reply, not only the live one that questions.js handles.
  document.addEventListener("click", function (event) {
    var link = event.target.closest(".qlist a");
    if (!link) {
      return;
    }
    var card = document.getElementById(link.getAttribute("href").slice(1));
    var section = card ? card.closest("details.sec") : null;
    if (section) {
      section.open = true;
    }
  });

  // "Open all" / "Close all" for one agent reply's collapsible sections.
  // Delegated, so replies appended by the poll get working buttons too.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-open-secs], [data-close-secs]");
    if (!button) {
      return;
    }
    var open = button.hasAttribute("data-open-secs");
    var target =
      button.getAttribute("data-open-secs") ||
      button.getAttribute("data-close-secs");
    var root = document.getElementById(target);
    if (!root) {
      return;
    }
    root.querySelectorAll("details.sec").forEach(function (section) {
      section.open = open;
    });
  });

  // The editor's Discard button opens the confirmation dialog instead of
  // submitting the request that renders the no-JavaScript confirmation page.
  document.querySelectorAll("[data-open-dialog]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      var dialog = document.getElementById(button.getAttribute("data-open-dialog"));
      if (!dialog) {
        return;
      }
      event.preventDefault();
      if (dialog.hasAttribute("open")) {
        dialog.removeAttribute("open");
      }
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  });

  // A dialog the server rendered open is the no-JS confirmation.  Upgrade it
  // to a modal so it gets the backdrop and focus handling.
  document.querySelectorAll("dialog[data-auto-open]").forEach(function (dialog) {
    if (typeof dialog.showModal !== "function") {
      return;
    }
    if (dialog.hasAttribute("open")) {
      dialog.removeAttribute("open");
    }
    dialog.showModal();
  });

  // The shortcuts help is in base.html, so every page has it.  It is a native
  // <dialog>: Esc closes it and its form is method="dialog", so only the
  // backdrop needs script.  A click on the backdrop is the one whose target is
  // the dialog itself, but so is a click on the dialog's own padding, hence
  // the position check.
  var helpDialog = document.getElementById("help-dialog");
  if (helpDialog) {
    helpDialog.addEventListener("click", function (event) {
      if (event.target !== helpDialog) {
        return;
      }
      var rect = helpDialog.getBoundingClientRect();
      if (
        event.clientX >= rect.left &&
        event.clientX <= rect.right &&
        event.clientY >= rect.top &&
        event.clientY <= rect.bottom
      ) {
        return;
      }
      helpDialog.close();
    });
  }

  // The editor's repository select reveals the two "new repository" fields
  // only while "Add a repository…" is chosen.  Without JavaScript both fields
  // stay visible, which is why they are never hidden on the server.
  var repoSelect = document.getElementById("repo");
  var newRepoFields = document.getElementById("repo-new-fields");
  if (repoSelect && repoSelect.tagName === "SELECT" && newRepoFields) {
    var repoNewUrl = document.getElementById("repo_new_url");
    var repoNewName = document.getElementById("repo_new_name");
    var newRepoValue = "";
    var syncNewRepoFields = function () {
      var adding = repoSelect.value === newRepoValue;
      newRepoFields.hidden = !adding;
      if (!adding) {
        // A stale URL must not win over the repository the user picked.
        if (repoNewUrl) {
          repoNewUrl.value = "";
        }
        if (repoNewName) {
          repoNewName.value = "";
        }
      }
    };
    repoSelect.addEventListener("change", syncNewRepoFields);
    syncNewRepoFields();
  }

  // Image thumbnails and Markdown images in the thread open in a native
  // dialog.  Delegated, so entries appended by the poll work without
  // rebinding.  A Markdown image has no data-lightbox, so its own src and alt
  // feed the dialog; preventDefault stops a linked image from navigating as
  // well.  Only a same-origin source can go in the dialog: the CSP would
  // block an external image anyway, and swallowing the click would break the
  // link wrapped around it, so an off-site image is left entirely alone.
  var lightbox = document.getElementById("lightbox");
  if (lightbox) {
    var lightboxImage = lightbox.querySelector("img");
    document.addEventListener("click", function (event) {
      if (event.target === lightbox) {
        lightbox.close();
        return;
      }
      var trigger = event.target.closest("[data-lightbox]");
      var source;
      var alt;
      if (trigger) {
        source = trigger.getAttribute("data-lightbox") || "";
        alt = trigger.getAttribute("aria-label") || "";
      } else if (event.target.matches(".md img")) {
        var resolved = new URL(
          event.target.getAttribute("src") || "",
          window.location.href
        );
        if (resolved.origin !== window.location.origin) {
          return;
        }
        source = resolved.href;
        alt = event.target.getAttribute("alt") || "";
        event.preventDefault();
      } else {
        return;
      }
      if (lightboxImage) {
        lightboxImage.src = source;
        lightboxImage.alt = alt;
      }
      if (typeof lightbox.showModal === "function") {
        lightbox.showModal();
      }
    });
  }

  /* ---------- keyboard shortcuts ---------- */

  /* One handler for every page.  A shortcut clicks the real control instead of
     reimplementing it, because the state buttons submit hidden forms and the
     row links navigate: a click is the only path that cannot diverge from the
     mouse.  Targets are looked up at key time, never bound here, because the
     ticket poll replaces the composer's actions and the home poll replaces the
     whole list. */
  function isTyping(target) {
    if (!target || !target.closest) {
      return false;
    }
    if (target.closest("input, textarea, select")) {
      return true;
    }
    var editable = target.closest("[contenteditable]");
    return Boolean(editable && editable.isContentEditable);
  }

  /* ``visible`` is false for the dock's links on the ticket and editor pages,
     whose layout hides the dock: "g" sequences must still work there.  The
     state buttons are checked because a hidden button is how the server
     declines an action.  The state menu's items are exempt: the list is
     collapsed, not declined, and the server only renders the actions the
     current state allows. */
  function activate(name, mustBeVisible) {
    var element = document.querySelector('[data-shortcut="' + name + '"]');
    if (!element || element.disabled) {
      return false;
    }
    if (element.getAttribute("aria-disabled") === "true") {
      return false;
    }
    if (mustBeVisible && element.getClientRects().length === 0) {
      var menu = element.closest("[data-shortcut-menu]");
      // A menu item has no boxes while its menu is collapsed; that is not a
      // declined action.  A hidden button inside an open (or collapsed) menu
      // still is, so the item's own hidden state is checked too.
      if (!menu || !menu.hidden || element.hidden) {
        return false;
      }
    }
    element.click();
    return true;
  }

  /* The home and archive lists are flat, all groups together; the row links
     are the focus targets, so Enter opens one natively. */
  function moveRow(step) {
    var rows = document.querySelectorAll("a.row");
    if (!rows.length) {
      return;
    }
    var current = Array.prototype.indexOf.call(rows, document.activeElement);
    var next = current === -1 ? (step > 0 ? 0 : rows.length - 1) : current + step;
    if (next < 0 || next >= rows.length) {
      return;
    }
    rows[next].focus();
  }

  /* "?" needs Shift, which is why the handler above ignores the other
     modifiers but never shiftKey.  The dialog is in base.html. */
  function openHelp() {
    if (!helpDialog || typeof helpDialog.showModal !== "function") {
      return false;
    }
    helpDialog.showModal();
    return true;
  }

  /* The reply box is the only text target a prefix sequence can replay
     into.  A list, the editor and a Done ticket have none, so a prefix key
     there has no pending text to protect. */
  function replyBox() {
    var reply = document.getElementById("reply");
    if (!reply || reply.disabled || reply.readOnly) {
      return null;
    }
    return reply;
  }

  /* On the ticket page a key that is not a shortcut starts a reply, so the
     user can just type.  Focusing during keydown, without preventDefault, makes
     the browser insert the key into the newly focused textarea.  The caret goes
     to the end so a saved draft is continued, not prefixed.  ``pending`` replays
     the prefix presses that armed a sequence, so "sure" does not become
     "ure".  Inserting it here fires no "input"; the keystroke that follows
     does, and the autosave then writes the whole value. */
  function typeIntoReply(pending) {
    var reply = replyBox();
    if (!reply) {
      return;
    }
    var end = reply.value.length;
    reply.focus();
    reply.setSelectionRange(end, end);
    if (pending) {
      reply.setRangeText(pending, end, end, "end");
    }
  }

  /* One "prefix key, then a second key" mechanism for both state actions
     ("s") and navigation ("g").  The pending buffer holds every prefix key
     that armed a sequence, separate from the timer so the text survives an
     expired window; the timer only decides whether the next key is still a
     shortcut.  A state shortcut must be visible because a hidden state button
     is how the server declines that action; the phone layout hides the dock on
     the ticket and editor pages, so on a phone its links are activated
     regardless of visibility.
     Only the lowercase prefix key arms a sequence: the check is against
     event.key, never its lowercased form, so a Shift-held "S" that starts
     "So" or "SQL" types normally.  The second key is still matched
     case-insensitively, as it always was. */
  var PREFIX_SHORTCUTS = {
    s: { n: "sn", q: "sq", d: "sd" },
    g: { t: "gt", a: "ga", r: "gr", n: "gn" },
  };
  var PREFIX_SEQUENCE_MS = 1500;
  var pendingReplyText = "";
  var pendingPrefix = "";
  var prefixSequenceStart = 0;
  var prefixExpiryTimer = 0;
  /* The key a question-card shortcut claimed, held until its keyup.  Opening
   * the Other field moves focus into its input, so without this a held digit
   * would auto-repeat into that field (or the reply box) instead of staying
   * one answer. */
  var claimedRepeatKey = "";

  /* The armed prefix is one attribute on <body>; a control's badge letter
     comes from its own data-shortcut-key, and the CSS decides which controls
     answer which prefix.  Nothing is bound or created per element, so the
     ticket poll can replace the composer's buttons while a sequence is armed
     and the next render still badges correctly.  Expiry hides the badge on its
     own timer: waiting for the next keydown would leave it up on an idle
     page. */
  function setArmedPrefix(prefix) {
    if (prefix) {
      document.body.setAttribute("data-shortcut-prefix", prefix);
    } else {
      document.body.removeAttribute("data-shortcut-prefix");
    }
  }

  function armPrefix(prefix) {
    pendingPrefix = prefix;
    prefixSequenceStart = Date.now();
    setArmedPrefix(prefix);
    window.clearTimeout(prefixExpiryTimer);
    prefixExpiryTimer = window.setTimeout(function () {
      setArmedPrefix("");
    }, PREFIX_SEQUENCE_MS);
  }

  /* A key ended the sequence.  pendingReplyText deliberately survives, because
     "s", a pause, "ure" must still type "sure". */
  function disarmPrefix() {
    pendingPrefix = "";
    window.clearTimeout(prefixExpiryTimer);
    prefixExpiryTimer = 0;
    setArmedPrefix("");
  }

  function prefixShortcut(prefix, key) {
    var shortcuts = PREFIX_SHORTCUTS[prefix];
    return Object.prototype.hasOwnProperty.call(shortcuts, key)
      ? shortcuts[key]
      : null;
  }

  /* Ctrl+/ must work on every page, and on the ticket page it must not lose
     reply text.  ticket.js's flushDraft waits for an in-flight Send and for
     in-flight saves, then saves the latest text; it resolves false when that
     save failed, in which case the page stays put with the error already
     shown.  A page without the composer navigates immediately. */
  function goToNextTicket() {
    var main = document.getElementById("ticket");
    var ticketId = main ? main.getAttribute("data-ticket-id") : "";
    var url = "/next";
    if (ticketId) {
      url += "?after=" + encodeURIComponent(ticketId);
    }
    var flush = Sym.flushDraft;
    var wait = typeof flush === "function" ? flush() : Promise.resolve(true);
    wait
      .then(function (saved) {
        if (saved) {
          window.location.assign(url);
        }
      })
      .catch(function () {});
  }

  document.addEventListener("keydown", function (event) {
    /* A repeat of a key a question-card shortcut already claimed must not
       reach whatever that claim focused (Other opens and focuses its input,
       and a pick can hide a card under the pointer).  This check is before
       the ignored / focused-field branch for exactly that reason. */
    if (
      event.repeat &&
      claimedRepeatKey !== "" &&
      event.key === claimedRepeatKey
    ) {
      event.preventDefault();
      return;
    }
    /* A modifier's own keydown types nothing.  Shift must not disarm: it is
       what "s", Shift+U relies on to type "sU", and Shift plus the second key
       of a sequence still fires it.  CapsLock does end the sequence (the user
       is leaving the keyboard's letter case alone), and since it types
       nothing the pending reply text survives. */
    if (event.key === "Shift") {
      return;
    }
    if (event.key === "CapsLock") {
      disarmPrefix();
      return;
    }
    /* Checked before the ignored branch below, which returns for every
       ctrlKey and isTyping event: this is the one shortcut that must work
       while a field has focus, and Chrome and Firefox leave Ctrl+/ unbound.
       Other keys were rejected: browsers do not let a page take Ctrl+N, Ctrl+U
       is view source, Ctrl+I is Firefox's Page Info, Ctrl+M mutes the tab in
       Firefox, and GNOME's emoji picker can take Ctrl+. first.
       Alt is excluded because AltGr reports ctrlKey on some layouts; meta is
       excluded because this is Ctrl-only; defaultPrevented is excluded
       because another handler already claimed the key.  A modal dialog still
       swallows it, like every other page shortcut. */
    if (
      event.key === "/" &&
      event.ctrlKey &&
      !event.altKey &&
      !event.metaKey &&
      !event.defaultPrevented &&
      document.querySelector("dialog[open]") === null
    ) {
      event.preventDefault();
      pendingReplyText = "";
      disarmPrefix();
      goToNextTicket();
      return;
    }
    /* An ignored key still cancels an armed prefix: otherwise "s", Ctrl+C, "d"
       would mark the ticket Done.  The event's own job is left alone (no
       preventDefault), and a modal dialog swallows every page shortcut so the
       discard confirmation and the lightbox behave like plain dialogs. */
    var ignored =
      event.defaultPrevented ||
      event.ctrlKey ||
      event.altKey ||
      event.metaKey ||
      isTyping(event.target) ||
      document.querySelector("dialog[open]") !== null;
    if (ignored) {
      pendingReplyText = "";
      disarmPrefix();
      return;
    }
    var key = event.key.toLowerCase();
    var now = Date.now();
    var awaitingPrefix =
      pendingPrefix !== "" && now - prefixSequenceStart <= PREFIX_SEQUENCE_MS;
    /* Only a control the page actually offers is clicked: "s","q" on a ticket
       with no Send to QA falls through to the typing branch, so the reply gets
       the "sq" the user meant to type. */
    if (awaitingPrefix) {
      var shortcut = prefixShortcut(pendingPrefix, key);
      if (shortcut && activate(shortcut, pendingPrefix === "s")) {
        pendingReplyText = "";
        disarmPrefix();
        event.preventDefault();
        return;
      }
    }
    /* A prefix key starts a sequence, or extends the one already armed, so
       "s","s","h" types "ssh"; the case is kept because that is what the user
       pressed.  The key is matched exactly and Shift must be up, so only the
       unshifted prefix arms: "So" and "SQL" start with an ordinary letter.
       The shiftKey check also covers Caps Lock, under which Shift+S reports
       the lowercase "s" while still typing an ordinary letter.  A *different*
       prefix key cannot be a second key of the armed sequence: with a reply
       box it falls through to typing below, which keeps "sg" together in the
       reply, but with nothing to type into it must not block the new sequence,
       or "s", a pause, then "g" "t" would do nothing on the home page. */
    if (
      !event.shiftKey &&
      Object.prototype.hasOwnProperty.call(PREFIX_SHORTCUTS, event.key)
    ) {
      if (pendingPrefix === "" || pendingPrefix === event.key) {
        pendingReplyText += event.key;
        armPrefix(event.key);
        return;
      }
      if (!replyBox()) {
        pendingReplyText = event.key;
        armPrefix(event.key);
        return;
      }
    }
    /* Question cards claim digits and the tab arrows.  app.js stays the single
       arbiter so the "s"/"g" prefix sequences and type-to-reply keep priority:
       while a sequence is awaiting its second key, or prefix text is waiting
       to be replayed into the reply box, the cards get no say.  The handler
       itself applies only the card lock; app.js's guards above still decide
       every other exclusion. */
    if (
      !awaitingPrefix &&
      pendingReplyText === "" &&
      window.SymQuestions &&
      typeof window.SymQuestions.handleKey === "function" &&
      window.SymQuestions.handleKey(event)
    ) {
      claimedRepeatKey = event.key;
      event.preventDefault();
      return;
    }
    // Any other key cancels the armed sequence but still does its own job, so
    // a stray "s" before "j" does not swallow the "j".
    disarmPrefix();
    if (key === "?") {
      pendingReplyText = "";
      if (openHelp()) {
        event.preventDefault();
      }
    } else if ((key === "j" || key === "k") && document.querySelector("a.row")) {
      pendingReplyText = "";
      moveRow(key === "j" ? 1 : -1);
    } else if (Array.from(event.key).length === 1 && event.key !== " ") {
      /* Space is left alone so it still scrolls the page or presses a button,
         which is why it falls into the clearing branch below instead of here.
         Array.from counts code points: an astral character has a length of 2. */
      var replay = pendingReplyText;
      pendingReplyText = "";
      typeIntoReply(replay);
    } else {
      pendingReplyText = "";
    }
  });

  document.addEventListener("keyup", function (event) {
    if (event.key === claimedRepeatKey) {
      claimedRepeatKey = "";
    }
  });
  /* A keyup can be lost when the window loses focus mid-hold; forget the
   * claim rather than swallow a later, unrelated key. */
  window.addEventListener("blur", function () {
    claimedRepeatKey = "";
  });

  /* ---------- the "Saved 14:38" indicator ---------- */

  Sym.showSaved = function (id, savedAt) {
    var indicator = document.getElementById(id);
    if (!indicator) {
      return;
    }
    var text = indicator.querySelector("[data-saved-text]");
    if (!text) {
      return;
    }
    var date = savedAt ? new Date(savedAt) : null;
    indicator.hidden = false;
    if (!date || Number.isNaN(date.valueOf())) {
      indicator.dataset.state = "saved";
      text.textContent = "Saved";
      return;
    }
    indicator.dataset.state = "saved";
    text.textContent = "Saved " + timeFormatter.format(date);
    indicator.title = dateTimeFormatter.format(date);
  };

  Sym.showNotSaved = function (id) {
    var indicator = document.getElementById(id);
    if (!indicator) {
      return;
    }
    var text = indicator.querySelector("[data-saved-text]");
    indicator.hidden = false;
    indicator.dataset.state = "error";
    if (text) {
      text.textContent = "Not saved";
    }
  };

  Sym.hideSaved = function (id) {
    var indicator = document.getElementById(id);
    if (indicator) {
      indicator.hidden = true;
    }
  };

  Sym.formatTimes(document);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      Sym.formatTimes(document);
    }
  });
})();
