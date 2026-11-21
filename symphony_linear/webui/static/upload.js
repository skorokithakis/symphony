/* Upload files to the composer and the editor.
 *
 * The upload endpoint takes the raw file as the request body and the original
 * name as a query parameter, so there is no multipart parsing on the server.
 * XMLHttpRequest is used instead of fetch because only it reports upload
 * progress.  Everything the server renders is escaped by Jinja; every string
 * built here goes in through textContent, never innerHTML.
 *
 * The module is opt-in: a page calls Sym.upload.init() with its elements. */
(function () {
  "use strict";

  var Sym = (window.Sym = window.Sym || {});

  var MAX_BYTES = 50 * 1024 * 1024;

  var FILE_ICON =
    '<svg class="ic ic--sm" viewBox="0 0 20 20" aria-hidden="true"><path d="M5 3h7l3 3v11H5zM12 3v3h3"></path></svg>';
  var CLOSE_ICON =
    '<svg class="ic ic--sm" viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15"></path></svg>';

  function formatSize(bytes) {
    if (bytes < 1024) {
      return bytes + " B";
    }
    if (bytes < 1024 * 1024) {
      return (bytes / 1024).toFixed(1) + " KB";
    }
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function iconButton(attribute, label) {
    var button = document.createElement("button");
    button.className = "icon-btn";
    button.type = "button";
    button.setAttribute(attribute, "");
    button.setAttribute("aria-label", label);
    button.innerHTML = CLOSE_ICON;
    return button;
  }

  function hiddenInput(list, hidden) {
    if (list) {
      list.hidden = hidden;
    }
  }

  function withName(url, name) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") +
      "name=" + encodeURIComponent(name);
  }

  function init(options) {
    var form = options.form;
    var zone = options.zone || form;
    var list = options.list;
    var picker = options.picker;
    var button = options.button;
    var getTarget = options.getTarget;
    var onUploaded = options.onUploaded || function () {};
    var onSettled = options.onSettled || function () {};
    var onBusyChange = options.onBusyChange || function () {};
    if (!form || !list || !picker || typeof getTarget !== "function") {
      return;
    }

    var queue = Promise.resolve();
    var dropDepth = 0;
    /* Counts queued uploads plus in-flight uploads and deletes.  A page can
     * use the busy callback to disable Send/Start, and setLocked() to stop
     * new attachment changes while a Send/Start request is in flight. */
    var active = 0;
    var busy = false;
    var locked = false;

    function refreshBusy() {
      var next = active > 0;
      if (next !== busy) {
        busy = next;
        try {
          onBusyChange(busy);
        } catch (error) {
          /* A page callback must never break the uploader. */
        }
      }
    }

    function setLocked(value) {
      locked = Boolean(value);
      if (picker) {
        picker.disabled = locked;
      }
      if (button) {
        button.disabled = locked;
      }
      list
        .querySelectorAll("[data-file-remove], [data-file-retry]")
        .forEach(function (control) {
          control.disabled = locked;
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
          throw new Error("Upload failed (" + response.status + ").");
        });
    }

    function successChip(data) {
      var file = data.file || {};
      var li = document.createElement("li");
      li.className = "file";
      li.setAttribute("data-stored-name", file.name || "");
      if (file.delete_url) {
        li.setAttribute("data-delete-url", file.delete_url);
      }
      li.innerHTML = FILE_ICON;
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.original_name || "";
      var size = document.createElement("span");
      size.className = "file-size";
      size.textContent = formatSize(file.size || 0);
      li.appendChild(name);
      li.appendChild(size);
      li.appendChild(iconButton("data-file-remove", "Remove " + name.textContent));
      return li;
    }

    function failedChip(file, message, retryable) {
      var li = document.createElement("li");
      li.className = "file file--err";
      li.innerHTML = FILE_ICON;
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.name;
      var msg = document.createElement("span");
      msg.className = "file-msg";
      msg.textContent = message;
      li.appendChild(name);
      li.appendChild(msg);
      if (retryable) {
        var retry = document.createElement("button");
        retry.className = "link-btn";
        retry.type = "button";
        retry.setAttribute("data-file-retry", "");
        retry.textContent = "Retry";
        li.appendChild(retry);
        li._retryFile = file;
      }
      li.appendChild(iconButton("data-file-remove", "Remove " + file.name));
      return li;
    }

    function uploadChip(file) {
      var li = document.createElement("li");
      li.className = "file file--up";
      li.innerHTML = FILE_ICON;
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = file.name;
      var size = document.createElement("span");
      size.className = "file-size";
      size.textContent = formatSize(file.size);
      var bar = document.createElement("progress");
      bar.className = "file-bar";
      bar.max = 100;
      bar.value = 0;
      bar.setAttribute("aria-label", "Uploading " + file.name);
      li.appendChild(name);
      li.appendChild(size);
      li.appendChild(iconButton("data-file-remove", "Cancel " + file.name));
      li.appendChild(bar);
      return li;
    }

    function settle(success) {
      try {
        onSettled(success);
      } catch (error) {
        /* A page callback must never break the uploader. */
      }
    }

    function uploadOne(file) {
      if (file.size > MAX_BYTES) {
        list.appendChild(failedChip(file, "Too large", false));
        hiddenInput(list, false);
        return Promise.resolve();
      }
      return Promise.resolve(getTarget()).then(function (url) {
        if (!url) {
          list.appendChild(failedChip(file, "Upload failed", true));
          hiddenInput(list, false);
          settle(false);
          return;
        }
        var li = uploadChip(file);
        list.appendChild(li);
        hiddenInput(list, false);
        return new Promise(function (resolve) {
          var xhr = new XMLHttpRequest();
          li._xhr = xhr;
          xhr.open("POST", withName(url, file.name), true);
          xhr.setRequestHeader("Content-Type", "application/octet-stream");
          xhr.setRequestHeader("Accept", "application/json");
          xhr.upload.addEventListener("progress", function (event) {
            if (event.lengthComputable && event.total > 0) {
              li.querySelector(".file-bar").value = Math.round(
                (event.loaded / event.total) * 100
              );
            }
          });
          xhr.addEventListener("load", function () {
            li._xhr = null;
            if (xhr.status === 200) {
              var data = null;
              try {
                data = JSON.parse(xhr.responseText);
              } catch (error) {
                data = null;
              }
              if (data && data.file) {
                li.replaceWith(successChip(data));
                try {
                  onUploaded(data);
                } catch (error) {
                  /* The file is stored; a page callback cannot undo that. */
                }
                settle(true);
                resolve();
                return;
              }
            }
            var message =
              xhr.status === 413 ? "Too large" : "Upload failed";
            li.replaceWith(failedChip(file, message, xhr.status !== 413));
            if (xhr.status === 413) {
              settle(true);
            } else {
              settle(false);
            }
            resolve();
          });
          xhr.addEventListener("error", function () {
            li._xhr = null;
            li.replaceWith(failedChip(file, "Upload failed", true));
            settle(false);
            resolve();
          });
          xhr.addEventListener("abort", function () {
            li._xhr = null;
            settle(false);
            resolve();
          });
          xhr.send(file);
        });
      });
    }

    function enqueue(file) {
      if (locked) {
        return;
      }
      active += 1;
      refreshBusy();
      queue = queue
        .then(function () {
          return uploadOne(file);
        })
        .catch(function () {
          /* uploadOne handles its own errors; keep the queue alive. */
        })
        .then(function () {
          active -= 1;
          refreshBusy();
        });
    }

    function uploadFiles(files) {
      Array.prototype.forEach.call(files, enqueue);
    }

    function pick() {
      if (locked) {
        return;
      }
      picker.click();
    }

    if (button) {
      button.addEventListener("click", pick);
    }
    picker.addEventListener("change", function () {
      if (!locked && picker.files && picker.files.length) {
        uploadFiles(picker.files);
      }
      picker.value = "";
    });

    // Paste of a screenshot or copied file while the text field has focus.
    form.addEventListener("paste", function (event) {
      if (locked) {
        return;
      }
      var files = event.clipboardData && event.clipboardData.files;
      if (files && files.length) {
        event.preventDefault();
        uploadFiles(files);
      }
    });

    // Drag and drop over the whole composer/editor field.
    form.addEventListener("dragenter", function (event) {
      event.preventDefault();
      if (locked) {
        return;
      }
      dropDepth += 1;
      zone.classList.add("is-drop");
    });
    form.addEventListener("dragover", function (event) {
      event.preventDefault();
      if (locked) {
        return;
      }
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
    });
    form.addEventListener("dragleave", function (event) {
      event.preventDefault();
      dropDepth = Math.max(0, dropDepth - 1);
      if (dropDepth === 0) {
        zone.classList.remove("is-drop");
      }
    });
    form.addEventListener("drop", function (event) {
      event.preventDefault();
      dropDepth = 0;
      zone.classList.remove("is-drop");
      if (locked) {
        return;
      }
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) {
        uploadFiles(files);
      }
    });

    list.addEventListener("click", function (event) {
      if (locked) {
        return;
      }
      var retry = event.target.closest("[data-file-retry]");
      if (retry) {
        var failed = retry.closest("li");
        var file = failed && failed._retryFile;
        if (file) {
          failed.remove();
          enqueue(file);
        }
        return;
      }
      var remove = event.target.closest("[data-file-remove]");
      if (!remove) {
        return;
      }
      var li = remove.closest("li");
      if (!li) {
        return;
      }
      if (li._xhr) {
        li._xhr.abort();
      }
      var deleteUrl = li.getAttribute("data-delete-url");
      if (!deleteUrl) {
        li.remove();
        hiddenInput(list, list.children.length > 0);
        return;
      }
      remove.disabled = true;
      active += 1;
      refreshBusy();
      fetch(deleteUrl, {
        method: "DELETE",
        headers: { Accept: "application/json" },
        cache: "no-store",
      })
        .then(function (response) {
          if (!response.ok && response.status !== 404) {
            return describeError(response);
          }
          li.remove();
          hiddenInput(list, list.children.length > 0);
        })
        .catch(function (error) {
          remove.disabled = locked;
          Sym.showError(error.message || "Could not remove the file.");
        })
        .then(function () {
          active -= 1;
          refreshBusy();
        });
    });

    return {
      /* A page that is about to navigate away (Start/Discard) can wait for
       * every queued upload to settle, so a file picked a moment ago is
       * either registered first or reported as failed first. */
      whenIdle: function () {
        return queue;
      },
      /* While a Send/Start request is in flight, no attachment may be added
       * or removed: otherwise the request and the server state could disagree
       * about which files belong to the reply. */
      setLocked: setLocked,
    };
  }

  Sym.upload = { init: init, formatSize: formatSize };
})();
