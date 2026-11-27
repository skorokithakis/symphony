/* Question cards: make the newest agent reply's cards answerable and build
 * the reply comment from them.
 *
 * The server renders the cards as static Markdown.  Controls exist only on the
 * newest agent entry and only while no human message follows it, so a page
 * without a script (and every older reply) keeps a readable card.  The
 * controls are mounted once per card and updated in place, so a field closing
 * never removes the control the user is tabbing or clicking to.  ticket.js
 * calls the interface at the end of this file and posts the assembled body.
 */
(function () {
  "use strict";

  var thread = document.getElementById("thread");
  var main = document.getElementById("ticket");
  var composer = document.getElementById("composer");
  if (!thread || !main) {
    return;
  }

  /* The plain forms are copied verbatim into the comment: they were escaped
   * by the server to stay literal Markdown, so they are never rebuilt from
   * the rendered label and never unescaped.  The answer text a human types is
   * sent exactly as typed. */
  var savedJson = main.getAttribute("data-reply-answers") || "";
  var saved = savedJson ? JSON.parse(savedJson) : null;

  var activeEntry = null;
  var activeCommentId = "";
  var cards = [];
  /* Question index -> raw state: selected option indexes, edits by option
   * index and the free/Other text.  The answer is derived from this by
   * answerOf(), which is the only place that decides what is answered. */
  var state = {};
  /* Replies whose answers a committed Send has consumed.  Keeping them here
   * keeps the cards static until the poll appends the human entry. */
  var sentReplies = {};
  var locked = false;
  /* A pointer press that began inside a card may still be waiting for its
   * release; a close that shrinks a row must wait for the click first. */
  var pointerDown = false;
  var pendingClose = null;

  var PENCIL_SVG =
    '<svg class="ic ic--sm" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 16l.9-3.4L13.6 3.9a1.4 1.4 0 012 0l.5.5a1.4 1.4 0 010 2l-8.7 8.7L4 16zM12 5l3 3"></path></svg>';

  /* ---------- the answer ---------- */

  function answerState(card) {
    var index = card.__qcardIndex;
    if (!state[index]) {
      state[index] = { selected: [], edits: {}, other: "" };
    }
    return state[index];
  }

  function cardByIndex(index) {
    return (
      cards.find(function (card) {
        return card.__qcardIndex === index;
      }) || null
    );
  }

  /* The one derivation of a question's answer from the stored state.  Live
   * rendering, hydration, the count and the comment all go through it, so a
   * kept "Other" text cannot answer a choose-one question whose listed option
   * is selected. */
  function answerOf(card) {
    var answer = answerState(card);
    var selected = answer.selected.slice().sort(function (a, b) {
      return a - b;
    });
    var other = "";
    if (card.__qcardKind === "free-text") {
      selected = [];
    }
    if (
      (card.__qcardKind === "choose-many" ||
        card.__qcardKind === "free-text" ||
        !selected.length) &&
      answer.other.trim()
    ) {
      other = answer.other;
    }
    return { selected: selected, other: other };
  }

  /* The answers of one card in row order.  Edited text replaces the label,
   * and a non-empty Other is one more answer at the end. */
  function answersFor(card) {
    var canonical = answerOf(card);
    var answer = answerState(card);
    if (card.__qcardKind === "free-text") {
      return canonical.other ? [canonical.other] : [];
    }
    var answers = [];
    canonical.selected.forEach(function (index) {
      var row = card.__qcardRows[index];
      if (!row) {
        return;
      }
      answers.push(
        Object.prototype.hasOwnProperty.call(answer.edits, index)
          ? answer.edits[index]
          : row.plain
      );
    });
    if (canonical.other) {
      answers.push(canonical.other);
    }
    return answers;
  }

  function answeredCount() {
    return cards.filter(function (card) {
      return answersFor(card).length > 0;
    }).length;
  }

  /* ---------- the card controls ---------- */

  function questionText(card) {
    var question = card.querySelector(".qcard-q");
    return question ? question.getAttribute("data-text") || "" : "";
  }

  function marker() {
    var mark = document.createElement("span");
    mark.className = "qcard-mark";
    mark.setAttribute("aria-hidden", "true");
    return mark;
  }

  function pencilButton(ariaLabel) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "qcard-edit";
    button.setAttribute("aria-label", ariaLabel);
    button.innerHTML = PENCIL_SVG;
    return button;
  }

  function pickButton() {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "qcard-pick";
    button.setAttribute("aria-pressed", "false");
    return button;
  }

  /* Mount the controls on one row and keep references to its parts.  The
   * row's own label and detail nodes are reused for the life of the card, so
   * closing a field only swaps which of them is visible. */
  function wireRow(
    row,
    textElement,
    labelElement,
    editedElement,
    plain,
    index
  ) {
    var pick = pickButton();
    row.insertBefore(marker(), row.firstChild);
    row.insertBefore(pick, textElement);
    pick.appendChild(textElement);
    var pencil = pencilButton(
      index === null ? "Write another answer" : "Edit the answer: " + plain
    );
    row.appendChild(pencil);
    row.classList.add("is-live");
    var descriptor = {
      row: row,
      plain: plain,
      index: index,
      textElement: textElement,
      labelElement: labelElement,
      editedElement: editedElement,
      pick: pick,
      pencil: pencil,
      field: null,
      editing: false,
    };
    row.__qcardDescriptor = descriptor;
    return descriptor;
  }

  function decorateOption(card, row, index) {
    var textElement = row.querySelector(".qcard-t");
    var labelElement = row.querySelector(".qcard-l");
    row.setAttribute("data-option-index", String(index));
    var editedElement = document.createElement("span");
    editedElement.className = "qcard-l qcard-l-edited";
    editedElement.hidden = true;
    textElement.insertBefore(editedElement, row.querySelector(".qcard-d"));
    card.__qcardRows[index] = wireRow(
      row,
      textElement,
      labelElement,
      editedElement,
      row.getAttribute("data-text") || "",
      index
    );
  }

  function buildOther(card) {
    var list = card.querySelector(".qcard-opts");
    var row = document.createElement("li");
    row.className = "qcard-opt qcard-other is-live";
    row.setAttribute("data-other", "true");
    var textElement = document.createElement("span");
    textElement.className = "qcard-t";
    var labelElement = document.createElement("span");
    labelElement.className = "qcard-l";
    labelElement.textContent = "Other";
    textElement.appendChild(labelElement);
    row.appendChild(textElement);
    list.appendChild(row);
    card.__qcardOther = wireRow(
      row,
      textElement,
      labelElement,
      null,
      "",
      null
    );
  }

  function buildFreeText(card) {
    var wrap = document.createElement("div");
    wrap.className = "qcard-answer";
    var field = document.createElement("textarea");
    field.className = "qcard-field input";
    field.rows = 2;
    field.placeholder = "Your answer";
    field.setAttribute("aria-label", "Your answer to: " + questionText(card));
    wrap.appendChild(field);
    card.appendChild(wrap);
    card.__qcardFree = field;
  }

  function showLabel(card, descriptor) {
    var answer = answerState(card);
    if (descriptor === card.__qcardOther) {
      descriptor.labelElement.textContent = answer.other.trim()
        ? answer.other
        : "Other";
      return;
    }
    var edited = Object.prototype.hasOwnProperty.call(
      answer.edits,
      descriptor.index
    );
    descriptor.editedElement.textContent = edited
      ? answer.edits[descriptor.index]
      : "";
    descriptor.editedElement.hidden = !edited;
    descriptor.labelElement.hidden = edited;
    descriptor.row.classList.toggle("is-edited", edited);
  }

  function syncSelection(card) {
    var canonical = answerOf(card);
    (card.__qcardRows || []).forEach(function (descriptor) {
      var picked = canonical.selected.indexOf(descriptor.index) !== -1;
      descriptor.row.classList.toggle("is-picked", picked);
      descriptor.pick.setAttribute("aria-pressed", picked ? "true" : "false");
    });
    if (card.__qcardOther) {
      var pickedOther = canonical.other !== "";
      card.__qcardOther.row.classList.toggle("is-picked", pickedOther);
      card.__qcardOther.pick.setAttribute(
        "aria-pressed",
        pickedOther ? "true" : "false"
      );
    }
  }

  function syncCard(card) {
    var answer = answerState(card);
    if (card.__qcardKind === "free-text") {
      card.__qcardFree.value = answer.other;
      return;
    }
    syncSelection(card);
    card.__qcardRows.forEach(function (descriptor) {
      showLabel(card, descriptor);
    });
    showLabel(card, card.__qcardOther);
  }

  /* ---------- editing ---------- */

  function closeEdit(card, descriptor, focusPencil) {
    if (!descriptor || !descriptor.editing) {
      return;
    }
    descriptor.editing = false;
    descriptor.row.classList.remove("is-editing");
    if (descriptor.field) {
      descriptor.field.remove();
      descriptor.field = null;
    }
    showLabel(card, descriptor);
    if (descriptor === card.__qcardOther) {
      descriptor.labelElement.hidden = false;
    }
    descriptor.pick.appendChild(descriptor.textElement);
    descriptor.pick.hidden = false;
    if (focusPencil) {
      descriptor.pencil.focus();
    }
  }

  function closeOpenEdit(card) {
    if (card.__qcardOther) {
      closeEdit(card, card.__qcardOther, false);
    }
    (card.__qcardRows || []).forEach(function (descriptor) {
      closeEdit(card, descriptor, false);
    });
  }

  function startEdit(card, descriptor) {
    closeOpenEdit(card);
    var answer = answerState(card);
    if (descriptor !== card.__qcardOther) {
      /* The pencil selects the option it belongs to, so the answer cannot be
       * edited while the option it replaces is not the answer. */
      if (card.__qcardKind === "choose-many") {
        if (answer.selected.indexOf(descriptor.index) === -1) {
          answer.selected.push(descriptor.index);
        }
      } else {
        answer.selected = [descriptor.index];
      }
      syncSelection(card);
    }
    descriptor.editing = true;
    descriptor.row.classList.add("is-editing");
    if (descriptor === card.__qcardOther) {
      descriptor.labelElement.hidden = true;
    } else {
      descriptor.labelElement.hidden = true;
      descriptor.editedElement.hidden = true;
    }
    descriptor.row.insertBefore(descriptor.textElement, descriptor.pencil);
    descriptor.pick.hidden = true;
    var field = document.createElement("input");
    field.type = "text";
    field.className =
      descriptor === card.__qcardOther
        ? "qcard-field qcard-other-field input"
        : "qcard-field qcard-edit-field input";
    field.value =
      descriptor === card.__qcardOther
        ? answer.other
        : Object.prototype.hasOwnProperty.call(answer.edits, descriptor.index)
          ? answer.edits[descriptor.index]
          : descriptor.plain;
    field.setAttribute(
      "aria-label",
      descriptor === card.__qcardOther
        ? "Write another answer"
        : "Answer: " + descriptor.plain
    );
    descriptor.textElement.insertBefore(field, descriptor.labelElement);
    descriptor.field = field;
    field.focus();
    field.select();
    reveal(field);
    updateCount();
    syncFormValidation();
    notifyChange();
  }

  function pick(card, descriptor) {
    var answer = answerState(card);
    if (card.__qcardKind === "choose-many") {
      var position = answer.selected.indexOf(descriptor.index);
      if (position === -1) {
        answer.selected.push(descriptor.index);
      } else {
        answer.selected.splice(position, 1);
      }
    } else {
      /* The listed option replaces the answer; kept Other text stays for the
       * pencil but no longer answers the question. */
      answer.selected = [descriptor.index];
    }
    syncSelection(card);
    updateCount();
    syncFormValidation();
    notifyChange();
  }

  function onInput(event) {
    var field = event.target;
    if (!field.classList || !field.classList.contains("qcard-field")) {
      return;
    }
    var card = field.closest(".qcard");
    var row = field.closest(".qcard-opt");
    var descriptor = row ? row.__qcardDescriptor : null;
    if (!card) {
      return;
    }
    var answer = answerState(card);
    if (!descriptor) {
      /* Free text: its field is the whole answer. */
      answer.other = field.value;
    } else if (descriptor === card.__qcardOther) {
      answer.other = field.value;
      if (
        field.value.trim() &&
        card.__qcardKind === "choose-one" &&
        answer.selected.length
      ) {
        answer.selected = [];
      }
      syncSelection(card);
    } else if (field.value === "") {
      delete answer.edits[descriptor.index];
      descriptor.row.classList.remove("is-edited");
    } else {
      answer.edits[descriptor.index] = field.value;
      descriptor.row.classList.add("is-edited");
    }
    updateCount();
    syncFormValidation();
    notifyChange();
  }

  function runPendingClose() {
    var close = pendingClose;
    pendingClose = null;
    if (close) {
      close();
    }
  }

  function onFocusOut(event) {
    var field = event.target;
    if (!field.classList || !field.classList.contains("qcard-field")) {
      return;
    }
    var row = field.closest(".qcard-opt");
    var card = field.closest(".qcard");
    if (!card || !row) {
      return;
    }
    var descriptor = row.__qcardDescriptor;
    function close() {
      if (!field.isConnected || field === document.activeElement) {
        return;
      }
      closeEdit(card, descriptor, false);
    }
    if (pointerDown) {
      /* The press has not been released yet.  Closing now would shrink this
       * row and could move the pressed control out from under the pointer. */
      pendingClose = close;
      return;
    }
    /* A keyboard focus change can close after the event, unless focus came
     * back to the field in the meantime. */
    window.setTimeout(function () {
      if (field !== document.activeElement) {
        close();
      }
    }, 0);
  }

  /* ---------- the active reply ---------- */

  function commentIdOf(entry) {
    return entry.id.indexOf("reply-") === 0 ? entry.id.slice(6) : "";
  }

  function activeReply() {
    if (!composer) {
      return null;
    }
    var agents = thread.querySelectorAll("article.entry--agent");
    if (!agents.length) {
      return null;
    }
    var newest = agents[agents.length - 1];
    /* Notices and error alerts do not end the question: only a human message
     * after the reply means the cards are part of the past. */
    for (
      var node = newest.nextElementSibling;
      node;
      node = node.nextElementSibling
    ) {
      if (node.classList.contains("entry--user")) {
        return null;
      }
    }
    return newest;
  }

  function hydrate() {
    if (!saved || saved.comment_id !== activeCommentId) {
      return;
    }
    (saved.questions || []).forEach(function (question) {
      var card = cardByIndex(question.question_index);
      if (!card) {
        return;
      }
      var answer = answerState(card);
      (question.selected || []).forEach(function (index) {
        if (
          Number.isInteger(index) &&
          index >= 0 &&
          index < card.__qcardRows.length &&
          answer.selected.indexOf(index) === -1
        ) {
          answer.selected.push(index);
        }
      });
      if (card.__qcardKind === "choose-one" && answer.selected.length > 1) {
        answer.selected = answer.selected.slice(0, 1);
      }
      Object.keys(question.edits || {}).forEach(function (key) {
        var index = Number(key);
        if (
          Number.isInteger(index) &&
          index >= 0 &&
          index < card.__qcardRows.length &&
          typeof question.edits[key] === "string"
        ) {
          answer.edits[index] = question.edits[key];
        }
      });
      if (typeof question.other === "string") {
        answer.other = question.other;
      }
    });
  }

  function activate(entry) {
    activeEntry = entry;
    activeCommentId = commentIdOf(entry);
    cards = [];
    state = {};
    Array.prototype.forEach.call(
      entry.querySelectorAll(".qcard"),
      function (card) {
        /* The clone is the static card the server rendered; it is what the
         * page shows again once these cards stop being active. */
        card.__qcardStatic = card.cloneNode(true);
        card.__qcardKind = card.getAttribute("data-kind") || "";
        card.__qcardIndex = Number(card.getAttribute("data-question-index"));
        card.__qcardRows = [];
        if (card.__qcardKind === "free-text") {
          buildFreeText(card);
        } else {
          Array.prototype.forEach.call(
            card.querySelectorAll(".qcard-opt"),
            function (row, index) {
              decorateOption(card, row, index);
            }
          );
          buildOther(card);
        }
        card.classList.add("is-live");
        if (locked) {
          card.inert = true;
          card.classList.add("is-locked");
        }
        cards.push(card);
      }
    );
    hydrate();
    cards.forEach(syncCard);
  }

  function deactivate() {
    cards.forEach(function (card) {
      card.replaceWith(card.__qcardStatic);
    });
    cards = [];
    state = {};
    activeEntry = null;
    activeCommentId = "";
  }

  function refresh() {
    var newest = activeReply();
    if (newest && sentReplies[commentIdOf(newest)]) {
      newest = null;
    }
    if (newest === activeEntry) {
      updateCount();
      syncFormValidation();
      return;
    }
    deactivate();
    if (newest) {
      activate(newest);
    }
    updateCount();
    syncFormValidation();
  }

  /* ---------- count and Send ---------- */

  function updateCount() {
    var actions = document.getElementById("composer-actions");
    var send = document.getElementById("act-send");
    var count = document.getElementById("answers-count");
    var group = document.getElementById("answers-group");
    if (!actions || !send || !activeEntry || !cards.length) {
      if (count) {
        count.remove();
      }
      /* Do not leave Send boxed in a group that no longer has a count. */
      if (group && group.parentElement === actions) {
        actions.insertBefore(send, group);
        group.remove();
      }
      return;
    }
    /* The count travels with Send as one flex item, so on a phone it wraps
     * onto the same row as the button instead of ending the previous row. */
    if (!group) {
      group = document.createElement("span");
      group.id = "answers-group";
      group.className = "answers-group";
      actions.appendChild(group);
      group.appendChild(send);
    }
    if (!count) {
      count = document.createElement("span");
      count.id = "answers-count";
      count.className = "answers-count";
    }
    if (count.parentElement !== group) {
      group.insertBefore(count, send);
    }
    count.textContent = answeredCount() + " of " + cards.length + " answered";
  }

  /* An answers-only Send must reach ticket.js even though the composer's
   * textarea is required: formnovalidate applies to this Send only, so the
   * no-script form keeps its required field. */
  function syncFormValidation() {
    var send = document.getElementById("act-send");
    if (send) {
      send.toggleAttribute(
        "formnovalidate",
        Boolean(activeEntry) && answeredCount() > 0
      );
    }
  }

  function notifyChange() {
    window.SymQuestions.onChange();
  }

  function setLocked(value) {
    locked = value;
    cards.forEach(function (card) {
      card.inert = value;
      card.classList.toggle("is-locked", value);
    });
  }

  /* ---------- scrolling ---------- */

  function scrollContainer() {
    var style = window.getComputedStyle(main);
    if (style.overflowY === "auto" || style.overflowY === "scroll") {
      return main;
    }
    return document.scrollingElement || document.documentElement;
  }

  /* The composer is sticky over the bottom of the thread, and on a phone the
   * keyboard shrinks the page a moment after focus.  Scroll the field above
   * the composer now and again once the keyboard has settled. */
  function reveal(field) {
    if (!composer) {
      return;
    }
    var box = field.getBoundingClientRect();
    var limit = composer.getBoundingClientRect().top - 16;
    if (box.bottom <= limit && box.top >= 0) {
      return;
    }
    var scroller = scrollContainer();
    scroller.scrollTop += box.bottom > limit ? box.bottom - limit : box.top;
  }

  /* ---------- events ---------- */

  thread.addEventListener("click", function (event) {
    var button = event.target.closest(".qcard-pick, .qcard-edit");
    if (!button) {
      return;
    }
    var card = button.closest(".qcard");
    var row = button.closest(".qcard-opt");
    if (!card || !row || !card.classList.contains("is-live")) {
      return;
    }
    event.preventDefault();
    var descriptor = row.__qcardDescriptor;
    if (button.classList.contains("qcard-edit") || row === card.__qcardOther.row) {
      startEdit(card, descriptor);
    } else {
      pick(card, descriptor);
    }
  });

  thread.addEventListener("input", onInput);

  thread.addEventListener("focusout", onFocusOut);

  thread.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.isComposing) {
      return;
    }
    var field = event.target;
    if (!field.classList || !field.classList.contains("qcard-field")) {
      return;
    }
    var row = field.closest(".qcard-opt");
    var card = field.closest(".qcard");
    var descriptor = row ? row.__qcardDescriptor : null;
    if (!card || !descriptor) {
      /* The free-text textarea keeps Enter for its own newline. */
      return;
    }
    event.preventDefault();
    closeEdit(card, descriptor, true);
  });

  thread.addEventListener("focusin", function (event) {
    var field = event.target;
    if (!field.classList || !field.classList.contains("qcard-field")) {
      return;
    }
    reveal(field);
    window.setTimeout(function () {
      reveal(field);
    }, 300);
  });

  document.addEventListener("pointerdown", function () {
    pointerDown = true;
  });

  document.addEventListener("pointerup", function () {
    pointerDown = false;
    /* The click is dispatched after pointerup, so let it land before a close
     * changes the row's height. */
    window.setTimeout(runPendingClose, 0);
  });

  document.addEventListener("pointercancel", function () {
    pointerDown = false;
    runPendingClose();
  });

  /* ---------- the interface ticket.js calls ---------- */

  function answers() {
    if (!activeEntry) {
      return null;
    }
    var questions = [];
    cards.forEach(function (card) {
      var answer = answerState(card);
      var selected = answer.selected.slice().sort(function (a, b) {
        return a - b;
      });
      var edits = Object.assign({}, answer.edits);
      if (!selected.length && !Object.keys(edits).length && !answer.other) {
        return;
      }
      questions.push({
        question_index: card.__qcardIndex,
        selected: selected,
        edits: edits,
        other: answer.other,
      });
    });
    /* An active reply always returns the snapshot, even when every answer was
     * cleared: the empty list is what removes them from the server. */
    return { comment_id: activeCommentId, questions: questions };
  }

  function comment(text) {
    var blocks = [];
    cards.forEach(function (card) {
      var answers = answersFor(card);
      if (!answers.length) {
        return;
      }
      blocks.push("> " + questionText(card));
      blocks.push(
        answers.length === 1
          ? answers[0]
          : answers
              .map(function (answer) {
                return "- " + answer;
              })
              .join("\n")
      );
    });
    if (text.trim()) {
      blocks.push(text);
    }
    return blocks.join("\n\n");
  }

  /* Retire the answers of the reply a committed Send carried.  A duplicate
   * response names the first attempt's reply, which may not be the active one;
   * only that reply is marked, so another reply's unsent answers survive. */
  function sent(replyId) {
    if (!replyId) {
      return;
    }
    sentReplies[replyId] = true;
    if (activeCommentId === replyId) {
      deactivate();
    }
    updateCount();
    syncFormValidation();
  }

  window.SymQuestions = {
    refresh: refresh,
    answers: answers,
    comment: comment,
    activeReplyId: function () {
      return activeCommentId;
    },
    sent: sent,
    setLocked: setLocked,
    onChange: null,
  };

  refresh();
})();
