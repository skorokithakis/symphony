/* Progressive enhancement for the server-rendered pages.
 *
 * Everything here is optional: the forms work as plain posts, thread sections
 * toggle natively, and the server writes a UTC fallback for every timestamp.
 * The Content-Security-Policy only allows this file (and the page's own
 * script), never inline script.  No framework, no build step. */
(function () {
  "use strict";

  var Sym = (window.Sym = window.Sym || {});

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
     picked up too. */
  Sym.setTitle = function (baseTitle, needsInputCount) {
    document.title =
      needsInputCount > 0 ? "(" + needsInputCount + ") " + baseTitle : baseTitle;
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

  // Copy buttons (the workspace path and the session id).
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy");
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text).catch(function () {});
      }
    });
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

  // Recent repository chips fill the input without a page load.
  var repoInput = document.getElementById("repo");
  document.querySelectorAll("[data-repo-chip]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      if (!repoInput) {
        return;
      }
      repoInput.value = chip.getAttribute("data-repo-chip") || "";
      repoInput.focus();
    });
  });

  // Image thumbnails in the thread open in a native dialog.  Delegated, so
  // entries appended by the poll work without rebinding.
  var lightbox = document.getElementById("lightbox");
  if (lightbox) {
    var lightboxImage = lightbox.querySelector("img");
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-lightbox]");
      if (trigger) {
        if (lightboxImage) {
          lightboxImage.src = trigger.getAttribute("data-lightbox") || "";
          lightboxImage.alt = trigger.getAttribute("aria-label") || "";
        }
        if (typeof lightbox.showModal === "function") {
          lightbox.showModal();
        }
        return;
      }
      if (event.target === lightbox) {
        lightbox.close();
      }
    });
  }

  // Keep the model labels' aria-pressed in sync with their radio inputs.
  var modelRadios = document.querySelectorAll('input[type="radio"][name="model"]');
  modelRadios.forEach(function (radio) {
    radio.addEventListener("change", function () {
      modelRadios.forEach(function (peer) {
        var label = document.querySelector('label[for="' + peer.id + '"]');
        if (label) {
          label.setAttribute("aria-pressed", peer.checked ? "true" : "false");
        }
      });
    });
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
