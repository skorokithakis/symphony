/* Reading an agent reply aloud: one shared audio element for the whole page.
 *
 * iOS Safari only allows play() from a user gesture.  The audio has to be
 * fetched first, and Safari refuses a play() that runs after that await.  The
 * click handler therefore starts the element synchronously with a very short
 * silent clip; the same element stays unlocked for the rest of the page's
 * life, so replacing its source with the fetched mp3 and playing it then
 * works.  One element, not one per button, because the unlock belongs to the
 * element.
 *
 * A media element rather than Web Audio: Web Audio stays silent while the
 * iPhone ringer switch is off.  A click on another reply stops the current
 * one, so only one voice is ever heard.
 */
(function () {
  "use strict";

  /* 0.1 seconds of 8-bit silence at 8 kHz, as a WAV.  It is never meant to be
     heard; it exists only to carry the user's gesture into the element. */
  var SILENT_SOURCE =
    "data:audio/wav;base64,UklGRkQDAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YSADAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgA==";

  var IDLE_LABEL = "Read this reply aloud";
  var BUSY_LABEL = "Loading the audio";
  var PLAYING_LABEL = "Stop reading this reply";

  var audio = new window.Audio();
  var blobUrl = null;
  /* The button whose audio is playing, and whether a request is in flight.
     A click while busy is ignored, so only one request can exist at a time. */
  var playing = null;
  var busy = false;

  function setLabel(button, label) {
    button.setAttribute("aria-label", label);
    button.title = label;
  }

  function setIdle(button) {
    button.setAttribute("data-state", "idle");
    button.setAttribute("aria-pressed", "false");
    setLabel(button, IDLE_LABEL);
  }

  function fail(button, message) {
    button.setAttribute("data-state", "error");
    button.setAttribute("aria-pressed", "false");
    setLabel(button, message);
  }

  function release() {
    if (blobUrl) {
      window.URL.revokeObjectURL(blobUrl);
      blobUrl = null;
    }
  }

  function stop() {
    if (!playing) {
      return;
    }
    var button = playing;
    playing = null;
    audio.pause();
    // Replace the source before revoking it, so the paused element is not left
    // pointing at a URL that no longer resolves.
    audio.src = SILENT_SOURCE;
    release();
    setIdle(button);
  }

  function start(button) {
    var url = button.getAttribute("data-speech");
    if (!url) {
      return;
    }
    stop();
    busy = true;
    button.setAttribute("data-state", "busy");
    setLabel(button, BUSY_LABEL);

    // Synchronously, while the click is still a gesture: Safari's unlock.
    audio.src = SILENT_SOURCE;
    var unlock = audio.play();
    if (unlock && typeof unlock.catch === "function") {
      unlock.catch(function () {
        /* The real audio gets its own play(); this clip is only the unlock. */
      });
    }

    window
      .fetch(url, {
        method: "POST",
        headers: { Accept: "audio/mpeg" },
        cache: "no-store",
        body: "",
      })
      .then(function (response) {
        if (response.ok) {
          return response.blob();
        }
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (payload) {
            var message =
              payload && typeof payload.error === "string" && payload.error
                ? payload.error
                : "The audio could not be made (" + response.status + ").";
            throw new Error(message);
          });
      })
      .then(function (blob) {
        release();
        blobUrl = window.URL.createObjectURL(blob);
        audio.src = blobUrl;
        return audio.play();
      })
      .then(function () {
        busy = false;
        // A very short clip can finish before play() reports success; ended
        // would then have found no playing button to reset.
        if (audio.ended) {
          release();
          setIdle(button);
          return;
        }
        // Only now is the reply audibly playing, so the stop square appears
        // when it can actually stop something.
        playing = button;
        button.setAttribute("data-state", "playing");
        button.setAttribute("aria-pressed", "true");
        setLabel(button, PLAYING_LABEL);
      })
      .catch(function (error) {
        busy = false;
        playing = null;
        audio.pause();
        release();
        fail(
          button,
          (error && error.message) || "The audio could not be played."
        );
      });
  }

  /* Delegated, so a reply the ticket page's poll appends gets a working button
     too.  A click while a request runs is ignored: the button in flight is
     the only one that may settle. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-speech]");
    if (!button) {
      return;
    }
    event.preventDefault();
    if (busy) {
      return;
    }
    if (button === playing) {
      stop();
      return;
    }
    start(button);
  });

  audio.addEventListener("ended", function () {
    if (!playing) {
      return;
    }
    var button = playing;
    playing = null;
    release();
    setIdle(button);
  });

  /* A pause this script did not ask for: system media controls, a phone call,
     a Bluetooth device.  stop() clears ``playing`` before it pauses, and the
     unlock clip and the source swap both run while ``playing`` is null, so
     only an outside pause reaches this with a button to reset. */
  audio.addEventListener("pause", function () {
    if (!playing) {
      return;
    }
    var button = playing;
    playing = null;
    release();
    setIdle(button);
  });

  audio.addEventListener("error", function () {
    if (!playing) {
      return;
    }
    var button = playing;
    playing = null;
    release();
    fail(button, "The audio could not be played. Try again.");
  });
})();
