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
  /* One deadline covers the key request plus the handshake, and another the
     final flush; a recording that never connects or never finishes must not
     leave the microphone captured. */
  var CONNECT_TIMEOUT_MS = 10000;
  var FINISH_TIMEOUT_MS = 10000;
  /* The ticket page's Send is the one submit control a recording leaves
     enabled: pressing it stops the recording and posts the final text.  Every
     other submit control (Stop, Done, Retry, Send to QA, the editor's Start)
     stays disabled while recording. */
  var SEND_ID = "act-send";

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
      /* A session being torn down never sends its audio, so the queue goes
         with it instead of holding the recorded blobs until the page
         collects them. */
      session.pending = [];
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
      if (session.done) {
        /* A Send pressed during the recording waits here: the final text is
           already in the textarea and the form is usable again.  A failed
           recording rejects instead, so the caller can tell the two apart
           without looking at the page error. */
        var done = session.done;
        session.done = null;
        if (message) {
          done.reject(new Error(message));
        } else {
          done.resolve();
        }
      }
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

    /* The stream may only be ended after the socket has the config and every
       queued chunk, so this runs both when the recorder stops and when a
       socket that was still connecting finally opens. */
    function finishIfReady(session) {
      if (
        !session.recorderStopped ||
        session.endSent ||
        !session.socket ||
        session.socket.readyState !== WebSocket.OPEN
      ) {
        return;
      }
      session.endSent = true;
      /* The final audio chunk is already on its way: the recorder's last
         dataavailable fires before its stop event, and the flush in onOpen
         keeps the order.  The empty text frame is how the stream is ended;
         the final responses, including finished, arrive afterwards. */
      session.socket.send("");
      session.finishTimer = window.setTimeout(function () {
        fail(session, "Soniox did not finish the transcription.");
      }, FINISH_TIMEOUT_MS);
    }

    function onOpen(session) {
      if (active !== session || !session.socket) {
        return;
      }
      clearTimers(session);
      /* The key travels in the connection's subprotocol list, not here. */
      session.socket.send(
        JSON.stringify({ model: "stt-rt-v6", audio_format: "auto" }),
      );
      /* Audio recorded while the key was fetched and the handshake ran waits
         here in order; it has to reach the service before anything the
         recorder produces from now on. */
      var pending = session.pending;
      session.pending = [];
      pending.forEach(function (chunk) {
        session.socket.send(chunk);
      });
      finishIfReady(session);
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
      if (!session || session.stopping) {
        return;
      }
      if (!session.recorder) {
        /* The microphone is still being acquired, which only a Send does
           while it is happening (the button is disabled until then).  Ending
           the recording the moment it exists keeps the microphone from
           running on after the reply was requested. */
        session.stopRequested = true;
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

      /* Read-only is what stops the text from changing under the recording;
         the disabled controls and the submit guard keep every submission but
         Send out, so the insertion needs no range tracking beyond the cursor
         captured here. */
      var blocked = [];
      if (target.form) {
        target.form
          .querySelectorAll("button, input")
          .forEach(function (control) {
            if (control.id === SEND_ID) {
              return;
            }
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
        stopRequested: false,
        recorderStopped: false,
        endSent: false,
        pending: [],
        finished: false,
        done: null,
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
          /* Send is the one submission a recording lets through: the ticket
             page stops the recording and sends the final text.  Everything
             else, including an implicit Enter submission, is refused. */
          var submitter = event.submitter;
          if (submitter && submitter.id === SEND_ID) {
            return;
          }
          event.preventDefault();
          event.stopImmediatePropagation();
        },
      };
      active = session;
      target.readOnly = true;
      if (session.form) {
        session.form.addEventListener("submit", session.submitGuard, true);
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
          /* Recording starts before the key request and the WSS handshake
             (~2s together) so the button turns into Stop as soon as the
             microphone is live; those chunks wait in session.pending until
             the service can take them. */
          try {
            session.recorder = new MediaRecorder(session.stream, {
              mimeType: mimeType,
            });
          } catch (error) {
            /* A browser that advertises MediaRecorder can still reject this
               stream; leaving the microphone open would strand it. */
            fail(session, "Could not start recording.");
            return;
          }
          session.recorder.addEventListener("dataavailable", function (event) {
            if (active !== session || !event.data || !event.data.size) {
              return;
            }
            if (session.socket && session.socket.readyState === WebSocket.OPEN) {
              session.socket.send(event.data);
              return;
            }
            /* Dropping a chunk here loses audio, and the first one carries
               the container header Soniox needs to decode the stream, so the
               queue is flushed in order before anything is sent live. */
            session.pending.push(event.data);
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
            session.recorderStopped = true;
            stopTracks(session);
            /* A stop while the socket is still connecting is not a failure:
               the end frame follows the queued audio once it opens. */
            finishIfReady(session);
          });
          try {
            session.recorder.start(TIMESLICE_MS);
          } catch (error) {
            fail(session, "Could not start recording.");
            return;
          }
          /* The connect deadline starts with the recording, not with the
             socket: it has to cover the key request as well as the handshake,
             because a key request that never answers would otherwise keep the
             microphone live and a waiting Send unresolved forever.  Expiry
             goes through fail() and its teardown like every other failure. */
          session.connectTimer = window.setTimeout(function () {
            fail(session, "Could not connect to Soniox in time.");
          }, CONNECT_TIMEOUT_MS);
          setRecording(button, true);
          button.disabled = false;
          /* A Send during the permission prompt asked for the recording to
             end, so acquire the microphone and close the stream straight
             away instead of recording after the reply was requested. */
          if (session.stopRequested) {
            stop();
          }
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

    /* Ends an active recording from outside the dictation button.  The ticket
       page's Send and Ctrl+Enter call this instead of being blocked: they wait
       for Soniox's final text and then post it, so the transcription is never
       cut off by a send that raced it.  null means nothing is recording, which
       lets the caller keep its normal synchronous path.  The promise resolves
       once the final text is in the textarea and the form is restored, and
       rejects when the recording failed; the caller must not send then, and
       the error is already on the page.  A stop during the microphone
       permission prompt is remembered and applied as soon as the recorder
       exists, because the dictation button is disabled until then. */
    function finishActive() {
      var session = active;
      if (!session || session.finished) {
        return null;
      }
      if (!session.done) {
        session.done = {};
        session.done.promise = new Promise(function (resolve, reject) {
          session.done.resolve = resolve;
          session.done.reject = reject;
        });
      }
      var promise = session.done.promise;
      if (!session.stopping && !session.stopRequested) {
        stop();
      }
      return promise;
    }

    Sym.finishDictation = finishActive;

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
