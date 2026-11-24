/* Home and archive: replace the list when it changed.
 *
 * Polling pauses while the tab is hidden and resumes immediately when it is
 * shown or the browser comes back online.  A failed poll shows the .conn
 * indicator and keeps the current list on screen. */
(function () {
  "use strict";

  var Sym = window.Sym;
  var body = document.getElementById("home-body");
  if (!Sym || !body) {
    return;
  }

  var api = body.getAttribute("data-api");
  var version = body.getAttribute("data-version") || "";
  var baseTitle = document.title.replace(/^\(\d+\)\s*/, "");
  var timer = null;
  var polling = false;
  var pollQueued = false;
  var INTERVAL = 3000;

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay === undefined ? INTERVAL : delay);
  }

  function apply(data) {
    Sym.showConn(false);
    Sym.setTitle(baseTitle, data.needs_input_count || 0);
    if (data.version !== version) {
      // The swap detaches the focused row, so remember which ticket it was and
      // focus the new row with the same href: j/k and Enter keep working
      // across a poll the user did not ask for.
      var focused = document.activeElement;
      var focusedHref =
        focused && focused.matches("a.row") ? focused.getAttribute("href") : null;
      body.innerHTML = data.html;
      version = data.version;
      if (focusedHref) {
        body.querySelectorAll("a.row").forEach(function (row) {
          if (row.getAttribute("href") === focusedHref) {
            row.focus();
          }
        });
      }
    }
    // Relative row times advance with the clock, not only when a row changes.
    Sym.formatTimes(body);
  }

  /* One request at a time: a timer tick, a visibility change and an online
   * event can otherwise overlap and apply an older list after a newer one. */
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
    Sym.getJSON(api)
      .then(function (data) {
        apply(data);
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

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      poll();
    }
  });
  window.addEventListener("online", poll);

  schedule(0);
})();
