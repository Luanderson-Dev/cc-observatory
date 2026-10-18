/*
 * cc-observatory: the UI's small behaviours (ADR 0031). No inline code, no eval, no
 * JavaScript dialogs (CSP, ADR 0034).
 *
 * - Ticks every <time data-since-ms> once a second, in the server's duration format.
 * - Gives each <time datetime> a title with the moment in the browser's time zone.
 * - Puts the number of sessions waiting on you in the title: "(2) cc-observatory".
 * - The theme toggle (system, light, dark), stored in localStorage; theme.js applies it
 *   before the first paint.
 * - The copy button of the hook settings.
 * - After a "Show older" page lands: drops the rows already on the page (a block that
 *   was open, or an agent that was running, when the page loaded may come again in an
 *   older page), and moves the focus that the replaced button held to the new button,
 *   or to the first new row at the end of the list, or to the list's heading when the
 *   last page brought nothing new.
 *   A page still on its way when a live refresh replaces its list is aborted.
 * - Live updates (ADR 0032, live.js dispatches the refetches): after the session page's
 *   refresh, drops from the older lists the rows the refreshed blocks and agents now
 *   hold; across the board's swap, keeps the collapsed bays the user opened open and
 *   the focus on the same strip link or bay summary.
 *
 * Everything runs again after htmx swaps a fragment in.
 */
(function (window, document) {
  "use strict";

  var THEME_KEY = "ccobs-theme";
  var PRODUCT = "cc-observatory";
  var TICK_MS = 1000;
  // Keep in step with ccobs.domain.presentation.format_duration (a test compares them):
  // the two largest units, the second zero-padded, rounded down, "0s" when negative.
  var UNITS = [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
    ["s", 1],
  ];

  function formatDuration(ms) {
    var seconds = Math.floor(Math.max(ms, 0) / 1000);
    for (var index = 0; index < UNITS.length - 1; index += 1) {
      var size = UNITS[index][1];
      if (seconds >= size) {
        var minorSize = UNITS[index + 1][1];
        var minor = Math.floor((seconds % size) / minorSize);
        var padded = minor < 10 ? "0" + minor : String(minor);
        return Math.floor(seconds / size) + UNITS[index][0] + " " + padded + UNITS[index + 1][0];
      }
    }
    return seconds + "s";
  }

  window.ccobsFormatDuration = formatDuration;

  function tick() {
    var now = Date.now();
    var times = document.querySelectorAll("[data-since-ms]");
    for (var index = 0; index < times.length; index += 1) {
      var since = Number(times[index].getAttribute("data-since-ms"));
      if (isFinite(since)) {
        var text = formatDuration(now - since);
        if (times[index].textContent !== text) {
          times[index].textContent = text;
        }
      }
    }
  }

  function localTimes() {
    var times = document.querySelectorAll("time[datetime]");
    for (var index = 0; index < times.length; index += 1) {
      var moment = new Date(times[index].getAttribute("datetime"));
      if (!isNaN(moment.getTime())) {
        times[index].title = moment.toLocaleString(undefined, {
          dateStyle: "medium",
          timeStyle: "medium",
        });
      }
    }
  }

  function waitingTitle() {
    var board = document.getElementById("board");
    if (!board || !board.hasAttribute("data-waiting")) {
      return;
    }
    var waiting = Number(board.getAttribute("data-waiting"));
    document.title = waiting > 0 ? "(" + waiting + ") " + PRODUCT : PRODUCT;
  }

  function storedTheme() {
    try {
      return window.localStorage.getItem(THEME_KEY);
    } catch (error) {
      return null;
    }
  }

  function applyTheme(choice) {
    var root = document.documentElement;
    if (choice === "light" || choice === "dark") {
      root.setAttribute("data-theme", choice);
    } else {
      root.removeAttribute("data-theme");
    }
    try {
      if (choice === "light" || choice === "dark") {
        window.localStorage.setItem(THEME_KEY, choice);
      } else {
        window.localStorage.removeItem(THEME_KEY);
      }
    } catch (error) {
      // Not stored: the choice lasts until the page is left.
    }
  }

  function themeToggle() {
    var toggle = document.querySelector(".theme-toggle");
    if (!toggle) {
      return;
    }
    var current = storedTheme();
    if (current !== "light" && current !== "dark") {
      current = "system";
    }
    var inputs = toggle.querySelectorAll("input[name='theme']");
    for (var index = 0; index < inputs.length; index += 1) {
      inputs[index].checked = inputs[index].value === current;
    }
    toggle.addEventListener("change", function (event) {
      applyTheme(event.target.value);
    });
    toggle.hidden = false;
  }

  function copyButtons() {
    var buttons = document.querySelectorAll("[data-copy-target]");
    for (var index = 0; index < buttons.length; index += 1) {
      var button = buttons[index];
      var target = document.getElementById(button.getAttribute("data-copy-target"));
      if (!target || !window.navigator.clipboard || button.hasAttribute("data-ready")) {
        continue;
      }
      button.setAttribute("data-ready", "");
      button.hidden = false;
      button.addEventListener("click", copier(button, target));
    }
  }

  function copier(button, target) {
    return function () {
      window.navigator.clipboard.writeText(target.textContent).then(
        function () {
          button.textContent = "Copied";
          window.setTimeout(function () {
            button.textContent = "Copy";
          }, 2000);
        },
        function () {
          button.textContent = "Select the text to copy it";
        }
      );
    };
  }

  // Rows that stand for one block or agent; a second one of the same ID is a repeat.
  var ROW_KEYS = ["data-block-id", "data-agent-id"];

  // Removes from the rows of list from index firstNew on those whose ID is already on
  // the page, outside them, and returns the rows that remain.
  function dropRepeats(root, list, firstNew) {
    var fresh = Array.prototype.slice.call(list.children, firstNew);
    for (var k = 0; k < ROW_KEYS.length; k += 1) {
      var key = ROW_KEYS[k];
      var seen = Object.create(null);
      var all = root.querySelectorAll("[" + key + "]");
      for (var index = 0; index < all.length; index += 1) {
        if (fresh.indexOf(all[index]) === -1) {
          seen[all[index].getAttribute(key)] = true;
        }
      }
      fresh = fresh.filter(function (row) {
        if (row.hasAttribute(key) && seen[row.getAttribute(key)] === true) {
          list.removeChild(row);
          return false;
        }
        return true;
      });
    }
    return fresh;
  }

  window.ccobsDropRepeats = dropRepeats;

  // The older list of each section, the region the live refresh replaces, and the key
  // of their rows.
  var OLDER_LISTS = [
    ["closed-blocks-older", "blocks-live", "data-block-id"],
    ["agents-older", "agents-live", "data-agent-id"],
  ];

  // Removes from each older list the rows whose ID its section's region holds.
  function dropOlderRepeats(root) {
    for (var index = 0; index < OLDER_LISTS.length; index += 1) {
      var older = root.getElementById(OLDER_LISTS[index][0]);
      var region = root.getElementById(OLDER_LISTS[index][1]);
      if (!older || !region) {
        continue;
      }
      var key = OLDER_LISTS[index][2];
      var held = Object.create(null);
      var rows = region.querySelectorAll("[" + key + "]");
      for (var r = 0; r < rows.length; r += 1) {
        held[rows[r].getAttribute(key)] = true;
      }
      var children = Array.prototype.slice.call(older.children);
      for (var c = 0; c < children.length; c += 1) {
        if (children[c].hasAttribute(key) && held[children[c].getAttribute(key)] === true) {
          older.removeChild(children[c]);
        }
      }
    }
  }

  window.ccobsDropOlderRepeats = dropOlderRepeats;

  // What the board's swap would lose: the bays open and the focused link or summary.
  function boardState(board, active) {
    var state = { open: [], href: null, bay: null };
    var bays = board.querySelectorAll("details[data-bay]");
    for (var index = 0; index < bays.length; index += 1) {
      if (bays[index].open) {
        state.open.push(bays[index].getAttribute("data-bay"));
      }
    }
    if (active && board.contains(active)) {
      if (active.tagName === "SUMMARY") {
        state.bay = active.parentNode.getAttribute("data-bay");
      } else if (active.hasAttribute("href")) {
        state.href = active.getAttribute("href");
      }
    }
    return state;
  }

  function restoreBoard(board, state) {
    var target = null;
    var bays = board.querySelectorAll("details[data-bay]");
    for (var index = 0; index < bays.length; index += 1) {
      var key = bays[index].getAttribute("data-bay");
      if (state.open.indexOf(key) !== -1) {
        bays[index].open = true;
      }
      if (state.bay === key) {
        target = bays[index].querySelector("summary");
      }
    }
    if (state.href !== null) {
      var links = board.querySelectorAll("a[href]");
      for (var l = 0; l < links.length && !target; l += 1) {
        if (links[l].getAttribute("href") === state.href) {
          target = links[l];
        }
      }
    }
    if (target) {
      target.focus({ preventScroll: true });
    }
  }

  window.ccobsBoardState = boardState;
  window.ccobsRestoreBoard = restoreBoard;

  // The board's state, from just before htmx swaps it to just after.
  var boardSaved = null;

  function beforeBoard(event) {
    var target = event.detail.target;
    if (target && target.id === "board" && event.detail.shouldSwap) {
      boardSaved = boardState(target, document.activeElement);
    }
  }

  function afterBoard(event) {
    if (boardSaved && event.target && event.target.id === "board") {
      restoreBoard(event.target, boardSaved);
      boardSaved = null;
    }
  }

  // The "Show older" swap in progress: its list, how many rows it had, the control it
  // replaces and whether that control's button had the focus.
  var pending = null;
  // The "Show older" button that had the focus when it was pressed: read before htmx
  // disables it for the request, which may move the focus to the body.
  var pressed = null;

  function isPager(element) {
    return Boolean(element && element.classList && element.classList.contains("more__button"));
  }

  // Set on a "Show older" button while its request is on its way, until its response
  // is handled.
  var LOADING = "data-ccobs-loading";

  function pagerRequest(event) {
    var button = event.detail.elt;
    if (isPager(button)) {
      pressed = document.activeElement === button ? button : null;
      button.setAttribute(LOADING, "");
    }
  }

  // A live refresh that starts its window over replaces the older list and the "Show
  // older" control out of band. A page still on its way would then land with the old
  // cursor: htmx dispatches its beforeSwap on the detached button, which no listener
  // here sees, and its out-of-band control would replace the new one. htmx cleans up
  // the old control (htmx:beforeCleanupElement, on it and on each child, while they are
  // still in the document and before their request data is cleared), so a button still
  // loading is aborted there: htmx's own htmx:abort handler aborts its XHR, and no
  // response is handled.
  function abortStalePager(event) {
    var element = event.target;
    if (isPager(element) && element.hasAttribute(LOADING)) {
      element.removeAttribute(LOADING);
      if (pressed === element) {
        pressed = null;
      }
      window.htmx.trigger(element, "htmx:abort");
    }
  }

  window.ccobsPagerRequest = pagerRequest;
  window.ccobsAbortStalePager = abortStalePager;

  function beforePager(event) {
    var button = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (!isPager(button)) {
      return;
    }
    // The response is here: its own swap replaces the button, nothing to abort.
    button.removeAttribute(LOADING);
    // An error response is not swapped (htmx's default): nothing to follow up.
    if (!event.detail.shouldSwap) {
      pressed = null;
      return;
    }
    pending = {
      list: event.detail.target,
      count: event.detail.target.children.length,
      control: button.parentNode.id,
      focused: pressed === button,
    };
    pressed = null;
  }

  window.ccobsBeforePager = beforePager;

  function afterPager() {
    if (!pending) {
      return;
    }
    var done = pending;
    pending = null;
    var fresh = dropRepeats(document, done.list, done.count);
    if (!done.focused) {
      return;
    }
    var control = document.getElementById(done.control);
    var next = control && control.querySelector("button");
    if (next) {
      next.focus();
    } else if (fresh.length) {
      fresh[0].setAttribute("tabindex", "-1");
      fresh[0].focus();
    } else {
      // The end, and nothing new: the list's heading, not the body.
      var section = done.list.closest("section");
      var heading = section && section.querySelector("h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus();
      }
    }
  }

  function refresh() {
    tick();
    localTimes();
    waitingTitle();
    copyButtons();
    dropOlderRepeats(document);
  }

  function start() {
    themeToggle();
    refresh();
    window.setInterval(function () {
      if (!document.hidden) {
        tick();
      }
    }, TICK_MS);
    document.body.addEventListener("htmx:afterSwap", refresh);
    document.body.addEventListener("htmx:afterSwap", afterBoard);
    document.body.addEventListener("htmx:beforeRequest", pagerRequest);
    document.body.addEventListener("htmx:beforeSwap", beforePager);
    document.body.addEventListener("htmx:beforeSwap", beforeBoard);
    document.body.addEventListener("htmx:beforeCleanupElement", abortStalePager);
    // A request that failed before any response leaves nothing to abort.
    document.body.addEventListener("htmx:sendError", function (event) {
      if (isPager(event.detail.elt)) {
        event.detail.elt.removeAttribute(LOADING);
      }
    });
    document.body.addEventListener("htmx:afterSettle", afterPager);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(window, document);
