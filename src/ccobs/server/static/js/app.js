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
 *   or to the first new row at the end of the list.
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

  // The "Show older" swap in progress: its list, how many rows it had, the control it
  // replaces and whether that control's button had the focus.
  var pending = null;
  // The "Show older" button that had the focus when it was pressed: read before htmx
  // disables it for the request, which may move the focus to the body.
  var pressed = null;

  function isPager(element) {
    return Boolean(element && element.classList && element.classList.contains("more__button"));
  }

  function pagerRequest(event) {
    var button = event.detail.elt;
    if (isPager(button)) {
      pressed = document.activeElement === button ? button : null;
    }
  }

  function beforePager(event) {
    var button = event.detail.requestConfig && event.detail.requestConfig.elt;
    // An error response is not swapped (htmx's default): nothing to follow up.
    if (!isPager(button) || !event.detail.shouldSwap) {
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
    }
  }

  function refresh() {
    tick();
    localTimes();
    waitingTitle();
    copyButtons();
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
    document.body.addEventListener("htmx:beforeRequest", pagerRequest);
    document.body.addEventListener("htmx:beforeSwap", beforePager);
    document.body.addEventListener("htmx:afterSettle", afterPager);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(window, document);
