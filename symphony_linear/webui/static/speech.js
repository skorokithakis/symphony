/* Reading an agent reply aloud: one shared audio element for the whole page.
 *
 * iOS Safari only allows play() from a user gesture.  The POST that asks the
 * daemon to make the audio takes a few seconds, so Safari refuses a play()
 * that runs after that await.  The click handler therefore starts the element
 * synchronously with a very short silent clip; the same element stays unlocked
 * for the rest of the page's life, so pointing it at the audio address and
 * playing it after the POST works.  One element, not one per button, because
 * the unlock belongs to the element.
 *
 * The audio plays while it downloads: the GET streams the mp3 as Soniox makes
 * it, so the listener hears the first sentence instead of waiting for the
 * whole clip.  A browser that will not send the page's Basic credentials on a
 * media request, or will not play audio of unknown length, fails that attempt
 * before any sound; the element is then pointed at a blob: URL built from a
 * fetch of the same address, which is the whole-file path this script used
 * before.  That fallback runs once, never in a loop.
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
  var FAILED_LABEL = "The audio could not be played. Try again.";

  /* iPhone Safari may accept the streamed source and then never play it: no
     error event and no rejected play(), so the button would stay busy for
     good and the listener, who is driving, would have no way out.  The
     fallback must therefore run on a deadline too.  Eight seconds is well
     past a working browser: the daemon already has the first bytes before the
     POST answers, and playback then starts in about a second. */
  var STREAM_START_TIMEOUT_MS = 8000;

  var audio = new window.Audio();
  var blobUrl = null;
  /* The button whose audio is audible, or null. */
  var playing = null;
  /* The preparation in flight, or null when none is.  Every click that starts
     one creates a fresh object, so an answer or a deadline from a cancelled
     attempt cannot be mistaken for the current one even when the same button
     started both: every continuation compares the object it captured with
     this variable.  Its fields are the button and the AbortController whose
     request (POST, or the fallback's download) a cancel aborts. */
  var attempt = null;
  /* Fires if the streamed source has not started playing; see the constant.
     It belongs to the current attempt and every way out clears it, but its
     callback still checks the attempt identity for an already-queued one. */
  var startTimer = null;
  /* What the shared element is doing at this moment, which decides what a
     media event means:
       "idle"      no source of ours, or a finished one;
       "unlock"    the silent clip that carries the gesture, before the POST;
       "stream"    the GET address, waiting for sound;
       "fallback"  fetching the whole mp3 after the stream failed;
       "blob"      the downloaded mp3, waiting for sound;
       "playing"   audible. */
  var stage = "idle";

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

  function clearStartTimer() {
    if (startTimer !== null) {
      window.clearTimeout(startTimer);
      startTimer = null;
    }
  }

  /* Drop the element's source.  Removing the attribute and calling load() is
     what closes a streamed GET: pausing alone leaves its connection reading,
     and pointing the element at another address would start a second request
     beside the first.  The start deadline belongs to the source being
     dropped, so every caller clears it by detaching. */
  function detachSource() {
    clearStartTimer();
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    release();
  }

  function stop() {
    if (!playing) {
      return;
    }
    var button = playing;
    // Clear the button before detaching, so the pause this causes is not read
    // as an outside pause and reset twice.
    playing = null;
    stage = "idle";
    detachSource();
    setIdle(button);
  }

  function failPending(message) {
    var current = attempt;
    attempt = null;
    stage = "idle";
    detachSource();
    fail(current.button, message);
  }

  /* Cancel the preparation in flight.  Clicking the busy button itself leaves
     that button idle; clicking another reply cancels this one and the click
     handler starts the other.  Aborting the request is what stops a late
     answer from being acted on; the attempt identity checks in every
     continuation cover an answer that was already on its way.  The pause and
     source removal produce no error state: the human asked to stop. */
  function cancel() {
    var current = attempt;
    attempt = null;
    stage = "idle";
    current.controller.abort();
    detachSource();
    setIdle(current.button);
  }

  /* Start the element on whatever source it holds.  Current browsers reject
     the returned promise; old ones throw, and both mean the same here. */
  function playSource(onFailure) {
    var started;
    try {
      started = audio.play();
    } catch (error) {
      onFailure();
      return;
    }
    if (started && typeof started.then === "function") {
      started.then(null, onFailure);
    }
  }

  /* Fetch the whole mp3 from the same address and play it from a blob: URL.
     This covers a browser that will not send Basic credentials on a media
     request and one that will not play audio of unknown length.  It is the
     last attempt: a failure here is the error state, and the next click
     starts again. */
  function fallback(current) {
    if (attempt !== current || stage !== "stream") {
      return;
    }
    stage = "fallback";
    // Drop the failed source so its GET is closed instead of running beside
    // the download.
    detachSource();

    window
      .fetch(current.button.getAttribute("data-speech"), {
        method: "GET",
        headers: { Accept: "audio/mpeg" },
        cache: "no-store",
        // The default is "same-origin", stated here because the whole reason
        // this path exists is a media request that did not carry the page's
        // authentication; this one must.
        credentials: "same-origin",
        // One controller covers the POST and this download: whichever is in
        // flight when the human cancels is the one that gets aborted.
        signal: current.controller.signal,
      })
      .then(function (response) {
        if (response.ok) {
          return response.blob();
        }
        throw new Error("The audio could not be fetched.");
      })
      .then(function (blob) {
        // A cancelled attempt can still deliver what it fetched; a newer
        // click, if any, owns the element now and must not be touched.
        if (attempt !== current) {
          return;
        }
        release();
        blobUrl = window.URL.createObjectURL(blob);
        stage = "blob";
        audio.src = blobUrl;
        playSource(function () {
          if (attempt === current && stage === "blob") {
            failPending(FAILED_LABEL);
          }
        });
      })
      .catch(function () {
        // Anything that went wrong before sound started, including a failed
        // createObjectURL or the abort of a cancelled download, is the same
        // failure to the listener - unless this attempt is over.
        if (attempt === current) {
          failPending(FAILED_LABEL);
        }
      });
  }

  function start(button) {
    var url = button.getAttribute("data-speech");
    if (!url) {
      return;
    }
    stop();
    attempt = { button: button, controller: new AbortController() };
    var current = attempt;
    stage = "unlock";
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
        signal: current.controller.signal,
      })
      .then(function (response) {
        if (response.ok) {
          return;
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
      .then(function () {
        // A cancelled attempt may still deliver its answer; it must not touch
        // the element, which now belongs to whatever came after.
        if (attempt !== current) {
          return;
        }
        stage = "stream";
        // The element starts fetching the stream at once.  Busy lasts until
        // the "playing" event, which is when sound actually began, not when
        // play() was called.
        audio.src = url;
        // A stream that never fires "playing" must still reach the fallback,
        // because a browser may accept it silently and leave the listener
        // with no way out; see STREAM_START_TIMEOUT_MS.
        startTimer = window.setTimeout(function () {
          startTimer = null;
          if (attempt === current && stage === "stream") {
            fallback(current);
          }
        }, STREAM_START_TIMEOUT_MS);
        playSource(function () {
          fallback(current);
        });
      })
      .catch(function (error) {
        if (attempt !== current) {
          return;
        }
        failPending((error && error.message) || FAILED_LABEL);
      });
  }

  /* Delegated, so a reply the ticket page's poll appends gets a working button
     too.  A click during a preparation cancels it: on the same button that
     just returns to idle, on another reply the same click then starts that
     one. */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-speech]");
    if (!button) {
      return;
    }
    event.preventDefault();
    if (attempt) {
      var sameButton = attempt.button === button;
      cancel();
      if (sameButton) {
        return;
      }
    }
    if (button === playing) {
      stop();
      return;
    }
    start(button);
  });

  /* Fires when playback has actually begun, including after a stall it
     recovered from.  Only the wait for sound after a source swap counts: the
     unlock clip fires it too, and a recovering stream fires it again while
     "playing". */
  audio.addEventListener("playing", function () {
    if (stage !== "stream" && stage !== "blob") {
      return;
    }
    if (!attempt) {
      return;
    }
    var button = attempt.button;
    attempt = null;
    stage = "playing";
    playing = button;
    // The source has started; neither its deadline nor an abort applies now.
    clearStartTimer();
    // Only now is the reply audibly playing, so the stop square appears when
    // it can actually stop something.
    button.setAttribute("data-state", "playing");
    button.setAttribute("aria-pressed", "true");
    setLabel(button, PLAYING_LABEL);
  });

  audio.addEventListener("ended", function () {
    if (stage !== "playing" || !playing) {
      return;
    }
    var button = playing;
    playing = null;
    stage = "idle";
    detachSource();
    setIdle(button);
  });

  /* A pause this script did not ask for: system media controls, a phone call,
     a Bluetooth device.  stop() clears ``playing`` before it pauses, and the
     unlock clip, both source swaps and the fallback download all run with no
     playing button, so only an outside pause reaches this with a button to
     reset.  "emptied" and "abort" are deliberately not listened to: every
     source change and every detach fires them, so treating either as the end
     of playback would reset a reply that is only being replaced. */
  audio.addEventListener("pause", function () {
    if (stage !== "playing" || !playing) {
      return;
    }
    var button = playing;
    playing = null;
    stage = "idle";
    detachSource();
    setIdle(button);
  });

  /* An error means different things at different stages.  During the silent
     clip and while the fallback downloads, it is not ours to act on; a failure
     of the streamed address is the one signal the fallback exists for; and an
     error while the blob plays is final. */
  audio.addEventListener("error", function () {
    if (stage === "stream") {
      if (attempt) {
        fallback(attempt);
      }
      return;
    }
    if (stage === "blob") {
      failPending(FAILED_LABEL);
      return;
    }
    if (stage !== "playing" || !playing) {
      return;
    }
    var button = playing;
    playing = null;
    stage = "idle";
    detachSource();
    fail(button, FAILED_LABEL);
  });
})();
