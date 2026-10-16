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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(window, document);
