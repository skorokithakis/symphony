/* Question cards: make the newest agent reply's cards answerable and build
 * the reply comment from them.
 *
 * The server renders the cards as static Markdown.  Controls exist only on the
 * newest agent entry and only while no human message follows it, so a page
 * without a script (and every older reply) keeps a readable card.  The
 * controls are mounted once per card and updated in place, so a field closing
 * never removes the control the user is tabbing or clicking to.  Adjacent live
 * cards become one tabbed panel; app.js arbitrates the number and arrow keys
 * through handleKey() so the prefix sequences and type-to-reply keep priority.
 * ticket.js calls the interface at the end of this file and posts the
 * assembled body.
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
  /* Runs of adjacent live cards.  A run of two or more is mounted in a panel
   * with a tab strip; a lone card stays where it is but is still a group, so
   * the number keys can address it. */
  var groups = [];
  /* The group the user last clicked or typed in.  Until then the keys address
   * the first visible group while the reply still shows the recommendation
   * defaults, and the first group with an unanswered question afterwards.  The
   * target is deliberately not persisted: a reload starts from that rule. */
  var targetGroup = null;
  /* Question index -> raw state: selected option indexes, edits by option
   * index and the free/Other text.  The answer is derived from this by
   * answerOf(), which is the only place that decides what is answered. */
  var state = {};
  /* Replies whose answers a committed Send has consumed.  Keeping them here
   * keeps the cards static until the poll appends the human entry. */
  var sentReplies = {};
  var locked = false;
  /* Set only by a real card change on this page.  A text-only autosave sends
   * no answers key, so a stale page cannot overwrite another device's. */
  var answersChanged = false;
  /* Set when the active reply had no saved snapshot and got the recommendation
   * defaults.  Combined with answersChanged, this is the short window in which
   * the panel opens on its first question and the keys target the first visible
   * group, because every preselected answer must be seen in order. */
  var defaultsApplied = false;
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
      state[index] = {
        selected: [],
        edits: {},
        other: "",
        otherChosen: false,
      };
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
   * rendering, hydration, the count and the comment all go through it.  The
   * answer is the chosen listed options plus the Other text when Other is
   * chosen; Other's text alone does not answer a question. */
  function answerOf(card) {
    var answer = answerState(card);
    var selected = answer.selected.slice().sort(function (a, b) {
      return a - b;
    });
    if (card.__qcardKind === "free-text") {
      return { selected: [], other: answer.other.trim() ? answer.other : "" };
    }
    var other =
      answer.otherChosen && answer.other.trim() ? answer.other : "";
    if (card.__qcardKind === "choose-one" && other) {
      selected = [];
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

  /* The number is the row's keyboard hint.  It is shown only for the first
   * ten rows, because a larger number would advertise a key that cannot pick
   * it. */
  function marker(number) {
    var mark = document.createElement("span");
    mark.className = "qcard-mark";
    mark.setAttribute("aria-hidden", "true");
    if (number <= 10) {
      var label = document.createElement("span");
      label.className = "qcard-num";
      label.textContent = String(number);
      mark.appendChild(label);
    }
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
    index,
    number
  ) {
    var pick = pickButton();
    row.insertBefore(marker(number), row.firstChild);
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
      recommended: row.hasAttribute("data-recommended"),
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
    /* After the original label, so an edited label stays before the
     * recommendation tag rather than after it. */
    textElement.insertBefore(editedElement, labelElement.nextSibling);
    card.__qcardRows[index] = wireRow(
      row,
      textElement,
      labelElement,
      editedElement,
      row.getAttribute("data-text") || "",
      index,
      index + 1
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
      null,
      card.__qcardRows.length + 1
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

  /* Only a tabbed panel gets one: the button moves to the group's next
   * question without answering this one, which is useful for every card kind
   * and is the only way forward on a free-text card or a choose-many card that
   * needs no answer.  It is a real button element and not a submit, because a
   * panel sits inside the ticket page; the locked guard repeats what the
   * cards' inert state already does for the pointer. */
  function buildNext(card, group) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "qcard-next";
    button.textContent = "Next";
    button.setAttribute("aria-label", "Next question");
    button.addEventListener("click", function (event) {
      event.preventDefault();
      if (locked) {
        return;
      }
      targetGroup = group;
      if (moveTab(group, 1)) {
        /* The activated button sits in the card that just became hidden, so
         * focus would otherwise be lost to the body; the newly selected tab
         * keeps it in the group, and Tab then reaches the new card. */
        group.tabs[group.active].focus();
      }
    });
    card.appendChild(button);
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
    var changed = false;
    if (descriptor !== card.__qcardOther) {
      /* The pencil selects the option it belongs to, so the answer cannot be
       * edited while the option it replaces is not the answer. */
      if (card.__qcardKind === "choose-many") {
        if (answer.selected.indexOf(descriptor.index) === -1) {
          answer.selected.push(descriptor.index);
          changed = true;
        }
      } else {
        if (
          answer.selected.length !== 1 ||
          answer.selected[0] !== descriptor.index
        ) {
          answer.selected = [descriptor.index];
          changed = true;
        }
        if (answer.otherChosen) {
          answer.otherChosen = false;
          changed = true;
        }
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
    if (changed) {
      notifyChange();
    }
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
      /* The listed option replaces the answer, even when it already was the
       * answer: tapping a listed option never clears a choose-one question,
       * only replaces its answer.  Kept Other text stays for the pencil but no
       * longer answers the question. */
      answer.selected = [descriptor.index];
    }
    if (card.__qcardKind !== "choose-many") {
      /* In a one-answer card a listed choice un-chooses Other; its text stays
       * visible so a mis-tap does not destroy it. */
      answer.otherChosen = false;
    }
    syncSelection(card);
    updateCount();
    syncFormValidation();
    notifyChange();
    if (card.__qcardKind === "choose-one") {
      /* A choose-one pick always answers the question, including a tap on the
       * already selected option, so it always moves the reader on to the next
       * question of the same group.  choose-many, Other and an edit never
       * move: their answer is not finished with a single pick. */
      moveTab(card.__qgroup, 1);
    }
  }

  function toggleOther(card) {
    var answer = answerState(card);
    if (!answer.other.trim()) {
      startEdit(card, card.__qcardOther);
      return;
    }
    answer.otherChosen = !answer.otherChosen;
    if (answer.otherChosen && card.__qcardKind === "choose-one") {
      answer.selected = [];
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
      if (field.value.trim()) {
        answer.otherChosen = true;
        if (card.__qcardKind === "choose-one" && answer.selected.length) {
          answer.selected = [];
        }
      } else {
        answer.otherChosen = false;
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
      if (question.other_chosen === true) {
        answer.otherChosen = true;
      }
    });
  }

  /* Without a saved snapshot, the marked options are the starting answer:
   * the first marked option of a choose-one card, every marked option of a
   * choose-many card.  The card's tag stays visible even when the answer no
   * longer selects it. */
  function applyDefaults() {
    cards.forEach(function (card) {
      if (card.__qcardKind === "free-text") {
        return;
      }
      var answer = answerState(card);
      if (card.__qcardKind === "choose-one") {
        for (var index = 0; index < card.__qcardRows.length; index += 1) {
          if (card.__qcardRows[index].recommended) {
            answer.selected = [index];
            return;
          }
        }
        return;
      }
      card.__qcardRows.forEach(function (descriptor) {
        if (descriptor.recommended) {
          answer.selected.push(descriptor.index);
        }
      });
    });
  }

  /* ---------- grouping and tabs ---------- */

  function isLiveCard(node) {
    return (
      Boolean(node) &&
      node.nodeType === 1 &&
      node.classList.contains("qcard") &&
      cards.indexOf(node) !== -1
    );
  }

  function unansweredIn(groupCards) {
    for (var index = 0; index < groupCards.length; index += 1) {
      if (!answersFor(groupCards[index]).length) {
        return index;
      }
    }
    return 0;
  }

  /* The active reply is still showing only the values applied in advance:
   * there was no saved snapshot for it, and the human has not changed a card
   * on this page.  This is page state, not a second answer derivation. */
  function showingDefaults() {
    return defaultsApplied && !answersChanged;
  }

  /* A group is a run of live cards that are adjacent element siblings.  The
   * server renders one card per top-level callout, so two cards are adjacent
   * only when nothing but whitespace separates them.  Cards in different
   * reply sections have different parents and never join a group, which keeps
   * a panel inside the section whose summary opened it.  The server's question
   * list counts groups by the same rule (_question_groups in markdown.py), so
   * the two must stay identical: otherwise the list would link into a panel
   * the tabs do not have, or point at the wrong question within a group. */
  function buildGroups() {
    groups = [];
    var members = [];
    cards.forEach(function (card) {
      if (members.indexOf(card) !== -1) {
        return;
      }
      var run = [card];
      for (
        var sibling = card.nextElementSibling;
        isLiveCard(sibling);
        sibling = sibling.nextElementSibling
      ) {
        run.push(sibling);
      }
      var group = {
        cards: run,
        panel: null,
        tabs: [],
        active: showingDefaults() ? 0 : unansweredIn(run),
        id: "qgroup-" + activeCommentId + "-" + groups.length,
      };
      groups.push(group);
      run.forEach(function (member) {
        members.push(member);
        member.__qgroup = group;
      });
      mountGroup(group);
    });
  }

  /* The tab a group opens on: its first question while the reply still shows
   * untouched defaults, else its first unanswered question, or its first
   * question when every one is answered.  A draft hydrates before this runs,
   * so a saved answer moves the opening tab with it. */
  function showTab(group, index) {
    if (index < 0 || index >= group.cards.length) {
      return;
    }
    group.active = index;
    if (!group.panel) {
      /* A lone card has no strip and stays visible; the keys still address it
       * through group.cards[group.active]. */
      return;
    }
    var tabFocused =
      group.tabs.indexOf(document.activeElement) !== -1;
    group.cards.forEach(function (card, position) {
      card.hidden = position !== index;
      var tab = group.tabs[position];
      tab.setAttribute(
        "aria-selected",
        position === index ? "true" : "false"
      );
      /* Roving tabindex: the strip is one Tab stop, and the arrows (or a
       * click) pick which tab it lands on. */
      tab.tabIndex = position === index ? 0 : -1;
    });
    if (tabFocused) {
      /* Focus follows the selection whichever path changed it: an arrow, a
       * digit that advanced after a choose-one pick, or a click. */
      group.tabs[index].focus();
    }
  }

  /* Changing tabs never wraps and never leaves the group: the last tab stays
   * put, so a pick cannot land the reader somewhere unexpected.  Returns
   * whether the tab actually changed. */
  function moveTab(group, step) {
    if (!group || !group.panel) {
      return false;
    }
    var next = group.active + step;
    if (next < 0 || next >= group.cards.length) {
      return false;
    }
    showTab(group, next);
    return true;
  }

  function currentTab(group) {
    return group ? group.cards[group.active] : null;
  }

  /* Whether the group is rendered.  A card inside a closed <details> section
   * is not, and the keys must not act on a group the reader cannot see.  A
   * panel itself is never hidden, but all but one of its cards are, so use
   * the group's current card when there is no panel.  checkVisibility() is
   * the only reliable test here: Chromium keeps layout boxes for content
   * hidden by a closed details, so getClientRects() still reports a size. */
  function isRendered(node) {
    if (!node) {
      return false;
    }
    if (typeof node.checkVisibility === "function") {
      return node.checkVisibility();
    }
    return node.getClientRects().length > 0;
  }

  function isGroupVisible(group) {
    var anchor = group.panel || currentTab(group) || group.cards[0];
    return isRendered(anchor);
  }

  function focusedTabGroup() {
    var tab = document.activeElement;
    if (!tab || !tab.classList || !tab.classList.contains("qgroup-tab")) {
      return null;
    }
    return tab.__qgroup || null;
  }

  /* The group the keys act on: the tab that has focus, else the group last
   * clicked or typed in, else the first visible group with an unanswered
   * question.  An invisible target is skipped rather than used, so collapsing
   * its section does not leave the keys pointing inside it. */
  function keyTarget() {
    var tabGroup = focusedTabGroup();
    if (tabGroup && isGroupVisible(tabGroup)) {
      return tabGroup;
    }
    if (targetGroup && isGroupVisible(targetGroup)) {
      return targetGroup;
    }
    return defaultTarget();
  }

  /* Until the user clicks or types in a group, the keys address the first
   * visible group while the reply still shows untouched defaults, so the
   * preselected answers are reached in order.  Once a card changes or a saved
   * snapshot exists, the target is the first group with an unanswered
   * question, falling back to the first visible group when every question is
   * answered, because the keys must still work for changing an answer.
   * Groups with no rendered card (a closed section) are never eligible. */
  function defaultTarget() {
    var index;
    if (showingDefaults()) {
      for (index = 0; index < groups.length; index += 1) {
        if (isGroupVisible(groups[index])) {
          return groups[index];
        }
      }
      return null;
    }
    for (index = 0; index < groups.length; index += 1) {
      if (
        isGroupVisible(groups[index]) &&
        groups[index].cards.some(function (card) {
          return !answersFor(card).length;
        })
      ) {
        return groups[index];
      }
    }
    for (index = 0; index < groups.length; index += 1) {
      if (isGroupVisible(groups[index])) {
        return groups[index];
      }
    }
    return null;
  }

  function mountGroup(group) {
    if (group.cards.length < 2) {
      return;
    }
    var first = group.cards[0];
    var panel = document.createElement("div");
    panel.className = "qgroup";
    var tabs = document.createElement("div");
    tabs.className = "qgroup-tabs";
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Questions");
    panel.appendChild(tabs);
    group.cards.forEach(function (card, index) {
      var tab = document.createElement("button");
      tab.type = "button";
      tab.className = "qgroup-tab";
      tab.id = group.id + "-tab-" + index;
      tab.__qgroup = group;
      tab.setAttribute("role", "tab");
      /* The server gives a card the id its top question-list link points at.
       * Keep it; only a card rendered without an anchor (another caller, a
       * test) gets the panel's own id. */
      if (!card.id) {
        card.id = group.id + "-card-" + index;
      }
      tab.setAttribute("aria-controls", card.id);
      var number = document.createElement("span");
      number.className = "qgroup-tab-n";
      number.textContent = String(index + 1);
      tab.appendChild(number);
      var check = document.createElement("span");
      check.className = "qgroup-tab-check";
      check.setAttribute("aria-hidden", "true");
      check.textContent = "\u2713";
      check.hidden = true;
      tab.appendChild(check);
      tabs.appendChild(tab);
      card.setAttribute("role", "tabpanel");
      card.setAttribute("aria-labelledby", tab.id);
      group.tabs.push(tab);
      /* The last card has nowhere to go, and a lone card has no strip, so it
       * never reaches this function. */
      if (index < group.cards.length - 1) {
        buildNext(card, group);
      }
    });
    tabs.addEventListener("click", function (event) {
      var tab = event.target.closest(".qgroup-tab");
      var index = group.tabs.indexOf(tab);
      if (index === -1) {
        return;
      }
      event.preventDefault();
      targetGroup = group;
      showTab(group, index);
    });
    first.parentNode.insertBefore(panel, first);
    group.cards.forEach(function (card) {
      panel.appendChild(card);
    });
    group.panel = panel;
    if (locked) {
      panel.inert = true;
      panel.classList.add("is-locked");
    }
    showTab(group, group.active);
  }

  /* The check mark is the same answer derivation the Send body uses, so a tab
   * never claims an answer the comment would omit. */
  function syncTabs() {
    groups.forEach(function (group) {
      group.tabs.forEach(function (tab, index) {
        var answered = answersFor(group.cards[index]).length > 0;
        var check = tab.querySelector(".qgroup-tab-check");
        if (check) {
          check.hidden = !answered;
        }
        tab.classList.toggle("is-answered", answered);
        tab.setAttribute(
          "aria-label",
          "Question " + (index + 1) + (answered ? ", answered" : "")
        );
      });
    });
  }

  /* The server renders a list of the reply's questions at its end when its
   * cards fall into two or more groups.  On the live reply each item gets a
   * check mark once its question is answered; older replies keep plain links.
   * The test is answerOf() alone: it is true in exactly the cases where
   * answeredCount()'s answersFor() is non-empty, so the marks always agree
   * with the "N of M answered" count. */
  function answeredByAnswerOf(card) {
    var answer = answerOf(card);
    return answer.selected.length > 0 || answer.other !== "";
  }

  function syncList() {
    var list = activeEntry ? activeEntry.querySelector(".qlist") : null;
    if (!list) {
      return;
    }
    Array.prototype.forEach.call(list.querySelectorAll("li"), function (item) {
      var card = cardByIndex(Number(item.getAttribute("data-question-index")));
      var link = item.querySelector("a");
      if (!card || !link) {
        return;
      }
      var answered = answeredByAnswerOf(card);
      var check = link.querySelector(".qlist-check");
      if (answered && !check) {
        check = document.createElement("span");
        check.className = "qlist-check";
        check.setAttribute("aria-hidden", "true");
        check.textContent = "\u2713";
        link.appendChild(check);
      } else if (!answered && check) {
        check.remove();
      }
    });
  }

  /* A reply stops being live when a newer one arrives or a human replies, so
   * its list loses the marks: only the newest reply's list is a live index. */
  function clearList() {
    var list = activeEntry ? activeEntry.querySelector(".qlist") : null;
    if (!list) {
      return;
    }
    Array.prototype.forEach.call(
      list.querySelectorAll(".qlist-check"),
      function (check) {
        check.remove();
      }
    );
  }

  function activate(entry) {
    activeEntry = entry;
    activeCommentId = commentIdOf(entry);
    cards = [];
    groups = [];
    targetGroup = null;
    state = {};
    answersChanged = false;
    defaultsApplied = false;
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
    /* The recommendations are only defaults.  A saved snapshot for this reply
     * replaces them, including one whose questions are all empty, so a cleared
     * answer cannot come back on the next load.  Apply them before the groups
     * choose their opening tab, or the tab would open on an unanswered
     * question the defaults have already answered. */
    if (!saved || saved.comment_id !== activeCommentId) {
      applyDefaults();
      defaultsApplied = true;
    }
    buildGroups();
    cards.forEach(syncCard);
    syncTabs();
  }

  function deactivate() {
    clearList();
    /* The panel is removed, not just emptied: a restored card must sit exactly
     * where the server rendered it, in the order of the reply. */
    groups.forEach(function (group) {
      if (!group.panel) {
        group.cards.forEach(function (card) {
          card.replaceWith(card.__qcardStatic);
        });
        return;
      }
      group.cards.forEach(function (card) {
        group.panel.parentNode.insertBefore(card.__qcardStatic, group.panel);
      });
      group.panel.remove();
    });
    cards = [];
    groups = [];
    targetGroup = null;
    state = {};
    defaultsApplied = false;
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
    /* The list's check marks are part of the same answer state as the count,
     * so every refresh of one refreshes the other. */
    syncList();
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
    answersChanged = true;
    syncTabs();
    window.SymQuestions.onChange();
  }

  function setLocked(value) {
    locked = value;
    cards.forEach(function (card) {
      card.inert = value;
      card.classList.toggle("is-locked", value);
    });
    /* The panel's tab strip is a control too: locking the cards alone would
     * leave the tabs clickable mid-Send. */
    groups.forEach(function (group) {
      if (!group.panel) {
        return;
      }
      group.panel.inert = value;
      group.panel.classList.toggle("is-locked", value);
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

  /* The group the keys address is the one the user last touched.  A click on
   * a tab is caught by the tab strip's own listener, since a tab is outside
   * the cards. */
  function noteTarget(node) {
    var card = node && node.closest ? node.closest(".qcard") : null;
    if (card && card.__qgroup) {
      targetGroup = card.__qgroup;
    }
  }

  thread.addEventListener("click", function (event) {
    noteTarget(event.target);
    var link = event.target.closest(".qlist a");
    if (link) {
      /* Every list on the page uses the same reply-local indexes, so an older
       * reply's item can name a card in the live reply.  Resolve the link's
       * fragment instead and act only when that target is one of the live
       * reply's cards; an older link then just scrolls, as it always did. */
      var href = link.getAttribute("href") || "";
      var target =
        href.charAt(0) === "#" ? document.getElementById(href.slice(1)) : null;
      if (isLiveCard(target) && target.__qgroup) {
        targetGroup = target.__qgroup;
        showTab(target.__qgroup, target.__qgroup.cards.indexOf(target));
      }
      /* No preventDefault: once showTab has made the card the visible tab, the
       * browser's own anchor scroll takes the reader there.  Focus stays on
       * the link, never in a field, so the keyboard does not open. */
      return;
    }
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
    if (button.classList.contains("qcard-edit")) {
      startEdit(card, descriptor);
    } else if (row === card.__qcardOther.row) {
      toggleOther(card);
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
    /* Typing in a group's field makes that group the keys' target. */
    noteTarget(field);
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

  /* ---------- number keys and tab arrows ---------- */

  /* One-based option number: 1-9 are themselves, 0 is the tenth.  Returns 0
   * for a key that is not a digit. */
  function optionNumber(key) {
    if (!/^[0-9]$/.test(key)) {
      return 0;
    }
    var digit = Number(key);
    return digit === 0 ? 10 : digit;
  }

  /* Called by app.js from its own keydown handler, which has already applied
   * every guard (modifiers, a focused field, an open dialog, the prefix
   * sequences).  app.js is the single arbiter on purpose: a capture listener
   * here could claim a key before app.js had decided whether it was still
   * part of an armed "s"/"g" sequence or pending reply text.  Returns true
   * when the key belongs to the question cards, so app.js preventDefaults it
   * instead of typing it into the reply box.
   *
   * The lock is the one guard app.js cannot know about: a card locked by an
   * in-flight Send is inert for the mouse, and the keyboard must not be a
   * second way to change an answer that is already in the posted body. */
  function handleKey(event) {
    if (!activeEntry || !groups.length || locked) {
      return false;
    }
    var group = keyTarget();
    if (!group) {
      return false;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      if (!group.panel) {
        return false;
      }
      /* A held arrow must not run through every tab. */
      if (event.repeat) {
        return true;
      }
      /* Using the keys is the interaction that makes this group the target,
       * exactly like clicking inside it does.  showTab moves focus when the
       * reader was on the strip. */
      targetGroup = group;
      moveTab(group, event.key === "ArrowRight" ? 1 : -1);
      return true;
    }
    var number = optionNumber(event.key);
    if (!number) {
      return false;
    }
    var card = currentTab(group);
    if (!card || card.__qcardKind === "free-text") {
      /* A free-text question has no options to number, so its digits keep
       * starting a reply in the composer. */
      return false;
    }
    var rows = card.__qcardRows || [];
    var other = rows.length + 1;
    var picks = number <= rows.length;
    var opensOther =
      !picks && number === other && other <= 10 && Boolean(card.__qcardOther);
    if (!picks && !opensOther) {
      /* A digit past the last row falls through, so a reply that starts with
       * a number still types. */
      return false;
    }
    /* A held digit must change exactly one answer, not toggle on every
     * auto-repeat and then type the remaining repeats into the reply box. */
    if (event.repeat) {
      return true;
    }
    /* A keyboard pick or Other is a use of this group, so later keys keep
     * addressing it even after the reader clicks elsewhere. */
    targetGroup = group;
    if (picks) {
      pick(card, rows[number - 1]);
    } else {
      startEdit(card, card.__qcardOther);
    }
    return true;
  }

  /* ---------- the interface ticket.js calls ---------- */

  function answers() {
    if (!activeEntry || !answersChanged) {
      return null;
    }
    /* Every question is in the snapshot, including one with no answer.  A
     * stored snapshot suppresses the defaults, so leaving a cleared question
     * out would let its recommendation come back on the next load.  The
     * snapshot is sent only after a card change on this page: a text-only
     * autosave must not write stale defaults over another tab's answers. */
    return {
      comment_id: activeCommentId,
      questions: cards.map(function (card) {
        var answer = answerState(card);
        return {
          question_index: card.__qcardIndex,
          selected: answer.selected.slice().sort(function (a, b) {
            return a - b;
          }),
          edits: Object.assign({}, answer.edits),
          other: answer.other,
          other_chosen: answer.otherChosen,
        };
      }),
    };
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
    /* app.js consults this from its own keydown handler. */
    handleKey: handleKey,
    onChange: null,
  };

  refresh();
})();
