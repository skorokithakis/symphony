/* List continuation in the Markdown text boxes (the reply composer and the
 * editor's ticket text).
 *
 * Enter continues a bullet, ordered or task item; Enter on an item that has no
 * text clears the marker instead of starting another empty item, so Enter
 * twice leaves the list; and a Backspace immediately after the inserted marker
 * removes it again.  Only a collapsed caret at or after the marker is
 * handled, because pressing Enter before a marker must stay a plain newline.
 *
 * Text goes in through execCommand('insertText') so the browser keeps its undo
 * stack and Ctrl+Z works.  Browsers that refuse the command get the
 * setRangeText fallback, which does not fire 'input' by itself; we dispatch it
 * because the reply draft autosave (ticket.js) and the editor autosave both
 * listen for it.  Renumbering the items that follow, indentation helpers and
 * anything else Markdown-editor-like are deliberately out of scope. */
(function () {
  "use strict";

  /* The indent is kept verbatim; the marker is normalised to one space in the
   * continuation rather than preserving however many spaces the user typed.
   * The marker may stand alone at the end of the line ("-" or "12."), because
   * Enter on that empty item removes it; a leading "[-*+]" or digits followed
   * by other text ("-foo", "1.5") is not a marker and stays a plain
   * newline. */
  var LIST = /^([ \t]*)([-*+]|\d+[.)])(?:[ \t]+|$)/;
  var TASK = /^\[[ xX]\](?:[ \t]+|$)/;

  /* The list marker at the start of the line the caret sits on.  offsets are
   * absolute indexes into the value, and markerEnd is just past everything a
   * continuation re-emits (indent, marker and a task's checkbox). */
  function itemAt(text, caret) {
    var lineStart = text.lastIndexOf("\n", caret - 1) + 1;
    var lineEnd = text.indexOf("\n", caret);
    if (lineEnd === -1) {
      lineEnd = text.length;
    }
    var match = LIST.exec(text.slice(lineStart, lineEnd));
    if (!match) {
      return null;
    }
    var indent = match[1];
    var token = match[2];
    var markerEnd = match[0].length;
    var task = null;
    if (token === "-" || token === "*" || token === "+") {
      task = TASK.exec(text.slice(lineStart + markerEnd, lineEnd));
      if (task) {
        markerEnd += task[0].length;
      }
    }
    var next;
    var ordered = /^(\d+)([.)])$/.exec(token);
    if (ordered) {
      next = (parseInt(ordered[1], 10) + 1) + ordered[2] + " ";
    } else if (task) {
      /* A checked item continues as an unchecked one, which is what a new
       * item means. */
      next = token + " [ ] ";
    } else {
      next = token + " ";
    }
    return {
      lineStart: lineStart,
      markerEnd: lineStart + markerEnd,
      lineEnd: lineEnd,
      prefix: indent + next,
    };
  }

  function replace(field, start, end, text) {
    field.setSelectionRange(start, end);
    var inserted = document.execCommand("insertText", false, text);
    if (!inserted) {
      field.setRangeText(text, start, end, "end");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    }
    /* Chromium pulls the caret back to the previous line when a deleted range
     * ends the value, which would leave the line Backspace just emptied
     * behind the caret.  The text commands stay on the undo stack either
     * way. */
    field.setSelectionRange(start + text.length, start + text.length);
  }

  /* The marker this script inserted last, while the caret has not moved off
   * its end and the value has not changed.  Any input, cursor move, click or
   * blur drops it, after which Backspace is the browser's own again. */
  var pending = null;

  function clearPending() {
    pending = null;
  }

  function onEnter(field, event) {
    if (field.selectionStart !== field.selectionEnd) {
      return;
    }
    var caret = field.selectionStart;
    var item = itemAt(field.value, caret);
    if (!item || caret < item.markerEnd) {
      return;
    }
    event.preventDefault();
    if (item.lineEnd === item.markerEnd) {
      /* The line holds nothing but the marker: drop it, leave the caret on a
       * blank line, and insert no newline. */
      replace(field, item.lineStart, item.markerEnd, "");
      clearPending();
      return;
    }
    replace(field, caret, caret, "\n" + item.prefix);
    pending = {
      field: field,
      start: caret + 1,
      end: caret + 1 + item.prefix.length,
      text: item.prefix,
    };
  }

  function onBackspace(event) {
    if (!pending || event.target !== pending.field) {
      return;
    }
    var field = pending.field;
    if (
      field.selectionStart !== pending.end ||
      field.selectionEnd !== pending.end ||
      field.value.slice(pending.start, pending.end) !== pending.text
    ) {
      clearPending();
      return;
    }
    event.preventDefault();
    replace(field, pending.start, pending.end, "");
    clearPending();
  }

  function bind(field) {
    field.addEventListener("keydown", function (event) {
      /* Shift+Enter is a plain newline, Ctrl/Cmd+Enter sends (ticket.js and
         editor.js), and Enter during an IME composition confirms a candidate. */
      if (
        event.isComposing ||
        event.shiftKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey
      ) {
        return;
      }
      if (event.key === "Enter") {
        onEnter(field, event);
      } else if (event.key === "Backspace") {
        onBackspace(event);
      }
    });
    ["input", "click", "blur"].forEach(function (name) {
      field.addEventListener(name, clearPending);
    });
  }

  ["reply", "description"].forEach(function (id) {
    var field = document.getElementById(id);
    if (field) {
      bind(field);
    }
  });

  document.addEventListener("selectionchange", function () {
    if (!pending) {
      return;
    }
    var field = pending.field;
    if (
      document.activeElement !== field ||
      field.selectionStart !== pending.end ||
      field.selectionEnd !== pending.end
    ) {
      clearPending();
    }
  });
})();
