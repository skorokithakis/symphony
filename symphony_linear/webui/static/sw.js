/* The service worker for Web Push.
 *
 * It is served from /sw.js, not /static/, because a worker only controls pages
 * at or below its own URL, and a notification click opens /tickets/<id>.
 * There is deliberately no fetch handler and no cache: the pages are the
 * server's, and a caching worker would show stale tickets.
 *
 * The icon is the public GitHub copy of static/icon.png (PUBLIC_ICON_URL in
 * symphony_linear/webui/push.py), because a notification's icon is fetched by
 * the browser's notification service without the page's Basic credentials, and
 * /static/icon.png would answer 401.  The response's CSP allows that one origin
 * (webui/push.py), and tests/test_webui_server.py asserts the spellings stay
 * equal. */
"use strict";

var ICON_URL = "https://raw.githubusercontent.com/skorokithakis/symphony/master/symphony_linear/webui/static/icon.png";

/* Without this a new version of this file waits until every window of the app
 * is closed, and an installed PWA kept in Android's recent apps can keep the
 * old worker (and its old notification click) for days.  Taking over at once
 * is safe because the worker has no fetch handler and no cache: the only
 * thing that changes hands is the push and notification click handling. */
self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("push", function (event) {
  var payload = {};
  if (event.data) {
    try {
      payload = event.data.json();
    } catch (error) {
      console.error("Symphony: the push payload was not JSON", error);
      payload = {};
    }
    if (payload === null || typeof payload !== "object") {
      console.error("Symphony: the push payload was not an object", payload);
      payload = {};
    }
  }
  /* Chrome penalises a subscription that receives a push and shows no
   * notification (userVisibleOnly), so a malformed payload still produces the
   * generic one rather than nothing. */
  event.waitUntil(
    self.registration
      .showNotification(payload.title || "Needs Input", {
        body: payload.body || "",
        /* The daemon sends the ticket id as the tag, so a newer notification
         * for the same ticket replaces the old one instead of stacking. */
        tag: payload.tag,
        data: {
          url: typeof payload.url === "string" ? payload.url : "/",
        },
        icon: ICON_URL,
      })
      .catch(function (error) {
        console.error("Symphony: showing the push notification failed", error);
      })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(function (appWindows) {
        var target = new URL(url, self.location.origin).href;
        for (var i = 0; i < appWindows.length; i += 1) {
          if (appWindows[i].url === target) {
            return appWindows[i].focus();
          }
        }
        /* Any other window is deliberately left alone: an editor or a reply
         * box there may hold unsaved text, and navigating it would lose it.
         *
         * The marker tells ticket.js that this window starts on the ticket
         * with no history, so it can send the first Android Back to the list
         * instead of leaving the app.  An already open window is matched on
         * the plain URL above, because ticket.js strips the marker on load. */
        var opened = new URL(target);
        opened.searchParams.set("from", "notification");
        return self.clients.openWindow(opened.href);
      })
      .catch(function (error) {
        console.error("Symphony: opening the ticket from a notification failed", error);
      })
  );
});
