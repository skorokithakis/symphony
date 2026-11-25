/* The Web Push opt-in bell.
 *
 * The button is rendered only when the daemon has a push store, and it starts
 * hidden.  This file reveals it only where the browser can actually subscribe
 * and this browser has not subscribed before, and it registers the worker on
 * every page load so an update is picked up without waiting for the bell.
 * There is no inline script: the Content-Security-Policy only allows this file
 * and the page's own script. */
(function () {
  "use strict";

  var bell = document.getElementById("push-bell");
  if (
    !bell ||
    !("serviceWorker" in navigator) ||
    !("PushManager" in window) ||
    !("Notification" in window)
  ) {
    return;
  }

  /* pushManager.subscribe wants the VAPID key as bytes; the server writes the
   * base64url spelling that is stored in webpush.json into the data
   * attribute. */
  function applicationServerKey(value) {
    var padded = value + "=".repeat((4 - (value.length % 4)) % 4);
    var binary = window.atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  function send(subscription) {
    return window.Sym.postJSON(
      "/api/push-subscriptions",
      subscription.toJSON()
    );
  }

  /* An existing subscription does not prove the daemon still has it: the store
   * lives in the workspace and may have been reset, or the POST that created
   * it may have failed on a previous visit.  Re-sending costs one request, the
   * store dedupes by endpoint, and it repairs that state without asking the
   * user to subscribe again. */
  function syncSubscription(registration) {
    return registration.pushManager.getSubscription().then(function (subscription) {
      if (subscription === null) {
        bell.hidden = false;
        return undefined;
      }
      return send(subscription).catch(function (error) {
        console.error("Symphony: re-sending the push subscription failed", error);
        bell.hidden = false;
      });
    });
  }

  navigator.serviceWorker
    .register("/sw.js")
    .then(syncSubscription)
    .catch(function (error) {
      console.error("Symphony: registering the push service worker failed", error);
    });

  bell.addEventListener("click", function () {
    bell.disabled = true;
    Notification.requestPermission()
      .then(function (permission) {
        if (permission !== "granted") {
          throw new Error("Notifications are blocked in this browser.");
        }
        return navigator.serviceWorker.ready;
      })
      .then(function (registration) {
        return registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: applicationServerKey(
            bell.getAttribute("data-push-public-key")
          ),
        });
      })
      .then(send)
      .then(function () {
        bell.hidden = true;
      })
      .catch(function (error) {
        /* Leave the bell in place so a permission the user grants later, or a
         * transient failure, can be retried by clicking again. */
        bell.disabled = false;
        console.error("Symphony: subscribing to push notifications failed", error);
        /* Sym.showError needs the alert box only the editor and ticket pages
         * render; elsewhere the console entry is the whole report. */
        if (window.Sym && document.getElementById("page-error")) {
          window.Sym.showError(error.message);
        }
      });
  });
})();
