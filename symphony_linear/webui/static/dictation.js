/* Live dictation into a textarea through the Soniox WebSocket API.
 *
 * The long-lived Soniox key never leaves the daemon.  Each recording asks
 * POST /api/soniox-key for a single-use temporary key, then streams the
 * microphone directly to Soniox; the daemon only relays the key request.
 *
 * Everything is optional: the button is rendered hidden and stays hidden when
 * the page is not a secure context, the browser has no microphone API or no
 * supported recording format, or the daemon has no Soniox key (in which case
 * the template does not render the button at all). */
(function () {
  "use strict";

  var Sym = window.Sym;
  if (!Sym) {
    return;
  }

  var KEY_URL = "/api/soniox-key";
  var SOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket";
  /* Small enough to feel live, large enough not to flood the socket with
     tiny binary frames. */
  var TIMESLICE_MS = 250;
  /* The handshake and the final flush are bounded; a recording that never
     connects or never finishes must not leave the microphone captured. */
  var CONNECT_TIMEOUT_MS = 10000;
  var FINISH_TIMEOUT_MS = 10000;

  /* Every entry is a container/codec pair Soniox's audio_format "auto"
     documents (webm and ogg; MP4/AAC is deliberately absent because the
     documented "aac" format is raw ADTS, not the MP4 container MediaRecorder
     produces).  The first supported one wins. */
  var MIME_TYPES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
  ];

  function supported() {
    return (
      window.isSecureContext === true &&
      !!navigator.mediaDevices &&
      typeof navigator.mediaDevices.getUserMedia === "function" &&
      typeof window.MediaRecorder === "function" &&
      typeof window.WebSocket === "function"
    );
  }

  function pickMimeType() {
    if (
      !window.MediaRecorder ||
      typeof window.MediaRecorder.isTypeSupported !== "function"
    ) {
      return null;
    }
    for (var index = 0; index < MIME_TYPES.length; index += 1) {
      if (window.MediaRecorder.isTypeSupported(MIME_TYPES[index])) {
        return MIME_TYPES[index];
      }
    }
    return null;
  }

  function setRecording(button, recording) {
    button.setAttribute("aria-pressed", recording ? "true" : "false");
    button.setAttribute(
      "aria-label",
      recording ? "Stop dictation" : "Start dictation",
    );
    button.title = recording ? "Stop dictation" : "Start dictation";
    /* The composer uses an icon-only button; the editor's also carries a
       visible word, which is what changes. */
    var label = button.querySelector(".dictation-label");
    if (label) {
      label.textContent = recording ? "Stop" : "Dictate";
    }
  }

  function wire(button) {
    var target = document.getElementById(button.getAttribute("data-dictation"));
    var mimeType = pickMimeType();
    if (!target || !mimeType || !supported()) {
      return;
    }
    button.hidden = false;

    /* Exactly one session is live at a time.  Every asynchronous callback
       holds its own session object and does nothing once a different one is
       current, so a late close/stop event cannot tear down a newer
       recording. */
    var active = null;

    function stopTracks(session) {
      if (!session.stream) {
        return;
      }
      session.stream.getTracks().forEach(function (track) {
        track.stop();
      });
      session.stream = null;
    }

    function clearTimers(session) {
      if (session.connectTimer !== null) {
        window.clearTimeout(session.connectTimer);
        session.connectTimer = null;
      }
      if (session.finishTimer !== null) {
        window.clearTimeout(session.finishTimer);
        session.finishTimer = null;
      }
    }

    function restoreForm(session) {
      target.readOnly = session.wasReadOnly;
      session.blocked.forEach(function (entry) {
        entry[0].disabled = entry[1];
      });
      if (session.form) {
        session.form.removeEventListener("submit", session.submitGuard, true);
        session.form.removeEventListener("keydown", session.keyGuard, true);
      }
      if (session.committed === "") {
        /* An empty transcript must leave the original text and selection
           exactly as they were. */
        target.setSelectionRange(session.startIndex, session.endIndex);
      }
    }

    function teardown(session, message) {
      if (active !== session) {
        return;
      }
      active = null;
      clearTimers(session);
      if (session.recorder && session.recorder.state !== "inactive") {
        try {
          session.recorder.stop();
        } catch (error) {
          /* Already stopped; nothing to release. */
        }
      }
      session.recorder = null;
      stopTracks(session);
      if (session.socket) {
        var closing = session.socket;
        session.socket = null;
        try {
          closing.close();
        } catch (error) {
          /* The socket was already closing. */
        }
      }
      restoreForm(session);
      setRecording(button, false);
      button.disabled = false;
      if (message) {
        Sym.showError(message);
      }
    }

    function fail(session, message) {
      if (active !== session) {
        return;
      }
      /* Never keep text the service had not confirmed. */
      session.preview = "";
      render(session);
      teardown(session, message);
    }

    function render(session) {
      var value =
        session.prefix + session.committed + session.preview + session.suffix;
      if (target.value === value) {
        return;
      }
      var cursor =
        session.prefix.length +
        session.committed.length +
        session.preview.length;
      target.value = value;
      target.setSelectionRange(cursor, cursor);
      /* The reply autosave, the editor autosave and Send all read the
         textarea's current value; the input event is what tells them it
         changed. */
      target.dispatchEvent(new Event("input", { bubbles: true }));
    }

    function onMessage(session, event) {
      if (active !== session) {
        return;
      }
      var payload;
      try {
        payload = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (!payload || typeof payload !== "object") {
        return;
      }
      if (payload.error_code || payload.error_message) {
        fail(
          session,
          typeof payload.error_message === "string" && payload.error_message
            ? payload.error_message
            : "Dictation failed.",
        );
        return;
      }
      if (Array.isArray(payload.tokens)) {
        var nonFinal = "";
        payload.tokens.forEach(function (token) {
          if (!token || typeof token.text !== "string") {
            return;
          }
          if (token.is_final) {
            session.committed += token.text;
          } else {
            nonFinal += token.text;
          }
        });
        session.preview = nonFinal;
        render(session);
      }
      if (payload.finished === true) {
        session.finished = true;
        /* After the finished response there is nothing more to confirm; a
           dangling preview would be text the service never finalised. */
        session.preview = "";
        render(session);
        teardown(session);
      }
    }

    function onOpen(session) {
      if (active !== session || !session.socket || !session.stream) {
        return;
      }
      clearTimers(session);
      /* The key travels in the connection's subprotocol list, not here. */
      session.socket.send(
        JSON.stringify({ model: "stt-rt-v6", audio_format: "auto" }),
      );
      try {
        session.recorder = new MediaRecorder(session.stream, {
          mimeType: mimeType,
        });
        session.recorder.addEventListener("dataavailable", function (event) {
          if (
            active === session &&
            session.socket &&
            session.socket.readyState === WebSocket.OPEN &&
            event.data &&
            event.data.size
          ) {
            session.socket.send(event.data);
          }
        });
        session.recorder.addEventListener("error", function () {
          fail(session, "Recording failed.");
        });
        session.recorder.addEventListener("stop", function () {
          if (active !== session) {
            return;
          }
          if (!session.stopping) {
            fail(session, "Recording stopped unexpectedly.");
            return;
          }
          /* The recorder's last dataavailable fires before this event, so the
             final audio chunk is already on its way; the empty text frame is
             how the stream is ended.  The final responses, including
             finished, arrive afterwards. */
          stopTracks(session);
          if (session.socket && session.socket.readyState === WebSocket.OPEN) {
            session.socket.send("");
            session.finishTimer = window.setTimeout(function () {
              fail(session, "Soniox did not finish the transcription.");
            }, FINISH_TIMEOUT_MS);
          } else {
            fail(session, "Dictation stopped before the transcription finished.");
          }
        });
        session.recorder.start(TIMESLICE_MS);
      } catch (error) {
        /* A browser that advertises MediaRecorder can still reject this
           stream; leaving the microphone open would strand it. */
        fail(session, "Could not start recording.");
        return;
      }
      setRecording(button, true);
      button.disabled = false;
    }

    function onClose(session) {
      if (active !== session) {
        return;
      }
      /* Reaching here means the server closed the stream before the finished
         response, while connecting, recording or stopping.  The error frame
         that normally precedes it was handled in onMessage; anything else is
         an unexpected stop. */
      fail(session, "Dictation stopped before the transcription finished.");
    }

    function stop() {
      var session = active;
      if (!session || !session.recorder || session.stopping) {
        return;
      }
      session.stopping = true;
      button.disabled = true;
      try {
        session.recorder.stop();
      } catch (error) {
        fail(session, "Recording stopped unexpectedly.");
      }
    }

    function start() {
      var startIndex = target.selectionStart;
      var endIndex = target.selectionEnd;
      if (typeof startIndex !== "number" || typeof endIndex !== "number") {
        startIndex = target.value.length;
        endIndex = startIndex;
      }

      /* Read-only and an unsubmittable form are what stop the text from
         changing or being sent under the recording, so the insertion needs
         no range tracking beyond the cursor captured here. */
      var blocked = [];
      if (target.form) {
        target.form
          .querySelectorAll("button, input")
          .forEach(function (control) {
            if (control.type === "submit" || control.hasAttribute("formaction")) {
              blocked.push([control, control.disabled]);
              control.disabled = true;
            }
          });
      }
      var session = {
        socket: null,
        recorder: null,
        stream: null,
        stopping: false,
        finished: false,
        prefix: target.value.slice(0, startIndex),
        suffix: target.value.slice(endIndex),
        committed: "",
        preview: "",
        startIndex: startIndex,
        endIndex: endIndex,
        form: target.form,
        blocked: blocked,
        wasReadOnly: target.readOnly,
        connectTimer: null,
        finishTimer: null,
        submitGuard: function (event) {
          event.preventDefault();
          event.stopImmediatePropagation();
        },
        keyGuard: function (event) {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            event.stopImmediatePropagation();
          }
        },
      };
      active = session;
      target.readOnly = true;
      if (session.form) {
        session.form.addEventListener("submit", session.submitGuard, true);
        session.form.addEventListener("keydown", session.keyGuard, true);
      }
      button.disabled = true;
      Sym.clearError();

      /* The microphone is acquired first: the temporary key is good for 60
         seconds, and a slow permission prompt must not consume that. */
      navigator.mediaDevices
        .getUserMedia({ audio: true })
        .then(function (media) {
          if (active !== session) {
            media.getTracks().forEach(function (track) {
              track.stop();
            });
            return;
          }
          session.stream = media;
          return Sym.postJSON(KEY_URL, {}).then(function (payload) {
            if (active !== session) {
              return;
            }
            if (
              !payload ||
              typeof payload.api_key !== "string" ||
              !payload.api_key
            ) {
              throw new Error("No temporary key was returned.");
            }
            var socket;
            try {
              socket = new WebSocket(SOCKET_URL, [
                "soniox-api-key",
                payload.api_key,
              ]);
            } catch (error) {
              throw new Error("Could not connect to Soniox.");
            }
            session.socket = socket;
            session.connectTimer = window.setTimeout(function () {
              fail(session, "Could not connect to Soniox in time.");
            }, CONNECT_TIMEOUT_MS);
            socket.addEventListener("open", function () {
              onOpen(session);
            });
            socket.addEventListener("message", function (event) {
              onMessage(session, event);
            });
            socket.addEventListener("error", function () {
              fail(session, "Could not connect to Soniox.");
            });
            socket.addEventListener("close", function () {
              onClose(session);
            });
          });
        })
        .catch(function (error) {
          if (active !== session) {
            return;
          }
          var name = error && error.name;
          if (name === "NotAllowedError") {
            fail(session, "Microphone access was denied.");
          } else if (name === "NotFoundError") {
            fail(session, "No microphone was found.");
          } else {
            fail(
              session,
              error && error.message
                ? error.message
                : "Could not start dictation.",
            );
          }
        });
    }

    button.addEventListener("click", function () {
      if (!active) {
        start();
      } else if (active.recorder && !active.stopping && !active.finished) {
        stop();
      }
    });
  }

  document.querySelectorAll("[data-dictation]").forEach(wire);
})();
