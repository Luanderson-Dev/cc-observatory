/*
 * cc-observatory: browser notifications for new alerts (ADR 0044). No inline code, no
 * eval, no innerHTML, no JavaScript dialogs (CSP, ADR 0034); no Service Worker, so
 * notifications show only while a tab of the app is open.
 *
 * live.js dispatches "ccobs:alerts" on document.body for each alerts ping and each time
 * its stream opens; a saved settings form sends one too (HX-Trigger). On each, if this
 * browser allowed notifications, the script reads /api/v1/alerts?after=<cursor> (oldest
 * first, page by page while a page comes back full) and shows a notification for each
 * unseen alert that is still on and fired less than 5 minutes ago. The cursor is the
 * newest alert id read, kept in localStorage under "ccobs.alerts.cursor" so every tab of
 * the origin shares it (in memory when the storage is unavailable); it moves past every
 * alert read, shown or not. On the first run (no cursor) it is set to the newest alert
 * and nothing shows, so an old history never floods the screen. Each notification's tag
 * is "ccobs-alert-<id>": two tabs that read the same alert replace one notification
 * instead of stacking two, and a tab skips an id another tab already stored. A click
 * focuses the tab and opens the session (or the alerts page for a daily alert).
 *
 * On the alerts page it fills #alert-notifications (hidden by the CSS while empty): a
 * button that asks for the permission (only from that click), or a sentence saying what
 * the browser does. Failed reads and unexpected answers are ignored: the next ping reads
 * again from the cursor. Nothing here throws to the console.
 */
(function (window, document) {
  "use strict";

  var CURSOR_KEY = "ccobs.alerts.cursor";
  var ALERTS_URL = "/api/v1/alerts";
  var PAGE_SIZE = 50;
  var FRESH_MS = 5 * 60 * 1000;
  var PRODUCT = "cc-observatory";
  var SHORT_ID = 8;
  var SLOT_ID = "alert-notifications";
  var ASK = "Notify me in this browser";
  var SAYS = {
    granted:
      "This browser shows a notification for each new alert while a tab of the app is open.",
    denied:
      "Notifications are blocked for this site. " +
      "Allow them in the browser's site settings to get them.",
    missing: "This browser can't show notifications.",
  };
  // The notification's title for each rule; an alert of another rule is not shown.
  var TITLES = {
    waiting_user: "waiting on you",
    repeated_failures: "API requests failing",
    session_cost: "session cost over the limit",
    daily_cost: "daily cost over the limit",
  };

  // Keep in step with ccobs.domain.presentation.format_duration (app.js has the same
  // function; test_notify_js.py compares the words with describe_alert's).
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

  function grouped(digits) {
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function twoDecimals(value) {
    var parts = value.toFixed(2).split(".");
    return grouped(parts[0]) + "." + parts[1];
  }

  // ccobs.domain.usage.format_usd: four decimals under a dollar, two from a dollar up.
  // toFixed rounds an exact tie up while Python rounds half to even (1.125 gives $1.13
  // here, $1.12 there), so test_notify_js.py's parity cases avoid ties on purpose.
  function formatUsd(value) {
    if (value === 0) {
      return "$0.00";
    }
    if (value < 0.0001) {
      return "under $0.0001";
    }
    return value < 1 ? "$" + value.toFixed(4) : "$" + twoDecimals(value);
  }

  // A dollar limit as a user typed it: "$10", or "$12.50" with cents.
  function limitUsd(value) {
    return "$" + (value === Math.floor(value) ? grouped(String(value)) : twoDecimals(value));
  }

  // ccobs.domain.presentation.describe_alert for an alert still on.
  function describe(item) {
    if (item.rule === "waiting_user") {
      return "Waiting on you for over " + formatDuration(Math.round(item.value * 1000));
    }
    if (item.rule === "repeated_failures") {
      return "API requests failed " + Math.round(item.value) + " times";
    }
    var cost = formatUsd(item.value) + " (limit " + limitUsd(item.threshold) + ")";
    if (item.rule === "session_cost") {
      return "This session cost " + cost;
    }
    return "The last 24 hours cost " + cost;
  }

  // The title and body of an alert's notification: the project name and short session
  // id for a session alert, never a path, then what happened.
  function alertText(item) {
    var what = describe(item);
    if (item.subject_kind === "session") {
      var project = item.project || "Unknown project";
      what = project + " (" + item.session_id.slice(0, SHORT_ID) + "): " + what;
    }
    return { title: PRODUCT + ": " + TITLES[item.rule], body: what };
  }

  window.ccobsAlertText = alertText;

  function isId(value) {
    return typeof value === "number" && value >= 0 && Number.isSafeInteger(value);
  }

  function isNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  // An alert this script can word, still on.
  function showable(item) {
    var subject =
      item.subject_kind === "day" ||
      (item.subject_kind === "session" &&
        typeof item.session_id === "string" &&
        (item.project === null || typeof item.project === "string"));
    return (
      subject &&
      Object.prototype.hasOwnProperty.call(TITLES, item.rule) &&
      item.cleared_at_ms === null &&
      isNumber(item.fired_at_ms) &&
      isNumber(item.value) &&
      isNumber(item.threshold)
    );
  }

  // The cursor: the stored one, which other tabs move too, or the one in memory when
  // the storage is unavailable (blocked, or full), whichever is further.
  var memory = null;

  function storage() {
    try {
      return window.localStorage || null;
    } catch (error) {
      return null;
    }
  }

  function storedCursor() {
    var store = storage();
    var raw = null;
    try {
      raw = store ? store.getItem(CURSOR_KEY) : null;
    } catch (error) {
      return null;
    }
    var value = raw !== null && /^[0-9]+$/.test(raw) ? Number(raw) : null;
    return isId(value) ? value : null;
  }

  function cursor() {
    var stored = storedCursor();
    if (stored === null || memory === null) {
      return stored === null ? memory : stored;
    }
    return Math.max(stored, memory);
  }

  function write(id) {
    memory = id;
    var store = storage();
    try {
      if (store) {
        store.setItem(CURSOR_KEY, String(id));
      }
    } catch (error) {
      // Kept in memory: this tab goes on from it.
    }
  }

  // Moves the cursor up to id, never down: the stored value is read again right before
  // the write, so a tab never writes below what another tab stored meanwhile.
  function advance(id) {
    var current = cursor();
    if (current === null || current < id) {
      write(id);
    }
  }

  // The newest alert's id from a ?limit=1 answer: 0 with none, null for a wrong shape.
  function newestId(items) {
    if (!items.length) {
      return 0;
    }
    return items[0] !== null && typeof items[0] === "object" && isId(items[0].id)
      ? items[0].id
      : null;
  }

  function permitted() {
    var api = window.Notification;
    return Boolean(api) && api.permission === "granted" && typeof window.fetch === "function";
  }

  // A list of alerts from the API, or a rejection for anything else.
  function read(query) {
    return window
      .fetch(ALERTS_URL + "?" + query, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("alerts read failed: " + response.status);
        }
        return response.json();
      })
      .then(function (items) {
        if (!Array.isArray(items)) {
          throw new Error("alerts read failed: not a list");
        }
        return items;
      });
  }

  function show(item) {
    var text = alertText(item);
    var href =
      item.subject_kind === "session"
        ? "/sessions/" + encodeURIComponent(item.session_id)
        : "/alerts";
    try {
      var notification = new window.Notification(text.title, {
        body: text.body,
        tag: "ccobs-alert-" + item.id,
      });
      notification.addEventListener("click", function (event) {
        try {
          event.preventDefault();
          window.focus();
          window.location.assign(href);
          notification.close();
        } catch (error) {
          // Nothing more to do: the user can open the app themselves.
        }
      });
    } catch (error) {
      // The browser refused this one (no constructor on some phones): read all the same.
    }
  }

  // Reads the alerts after the cursor, a page at a time. An empty first page checks the
  // newest alert: a cursor past it comes from another database (the data dir wiped, or
  // another instance on the same origin, whose ids restart at 1) and would silence every
  // alert, so it comes down to that id, written directly, showing nothing.
  function readAfter(after, first) {
    return read("after=" + after + "&limit=" + PAGE_SIZE).then(function (items) {
      if (first && !items.length) {
        return read("limit=1").then(function (newest) {
          var id = newestId(newest);
          var current = cursor();
          if (id !== null && current !== null && id < current) {
            write(id);
          }
        });
      }
      var last = after;
      for (var index = 0; index < items.length; index += 1) {
        var item = items[index];
        // Oldest first, past the cursor: anything else is not this API's answer.
        if (item === null || typeof item !== "object" || !isId(item.id) || item.id <= last) {
          return undefined;
        }
        last = item.id;
        var seen = cursor();
        if (seen !== null && item.id <= seen) {
          continue; // Another tab read it already.
        }
        advance(item.id);
        // fired_at_ms is the server's clock, Date.now() the browser's: in practice both run
        // on one machine. A browser clock over 5 minutes ahead shows nothing; one behind
        // only widens the window (a "future" alert still shows).
        if (showable(item) && window.Date.now() - item.fired_at_ms < FRESH_MS) {
          show(item);
        }
      }
      return items.length === PAGE_SIZE ? readAfter(last, false) : undefined;
    });
  }

  function catchUp() {
    var start = cursor();
    if (start !== null) {
      return readAfter(start, true);
    }
    // The first run: start from the newest alert, showing nothing.
    return read("limit=1").then(function (items) {
      var newest = newestId(items);
      if (newest !== null) {
        advance(newest);
      }
    });
  }

  // One read at a time per tab; pings during a read make one more read after it.
  var reading = false;
  var again = false;

  function check() {
    if (!permitted()) {
      return;
    }
    if (reading) {
      again = true;
      return;
    }
    reading = true;
    function done() {
      reading = false;
      if (again) {
        again = false;
        check();
      }
    }
    try {
      catchUp().then(done, done);
    } catch (error) {
      done();
    }
  }

  // The control on the alerts page: the button while the browser has not been asked,
  // a sentence otherwise. After an answer, moveFocus keeps the focus on the control, but
  // only if it is still on the old button or nowhere (the body): never stolen.
  function render(slot, moveFocus) {
    var active = document.activeElement;
    var focus =
      moveFocus && (active === null || active === document.body || active === slot.children[0]);
    var api = window.Notification;
    var state = api ? api.permission : "missing";
    var element;
    if (state === "default") {
      element = document.createElement("button");
      element.type = "button";
      element.className = "alert-notifications__ask";
      element.textContent = ASK;
      element.addEventListener("click", function () {
        ask(slot);
      });
    } else {
      element = document.createElement("p");
      element.className = "alert-notifications__text";
      element.textContent = state === "granted" || state === "denied" ? SAYS[state] : SAYS.missing;
      if (focus) {
        element.setAttribute("tabindex", "-1");
      }
    }
    slot.textContent = "";
    slot.appendChild(element);
    if (focus) {
      element.focus();
    }
  }

  // Asks for the permission, from the button's click (browsers ignore a request without
  // a user gesture). Older browsers answer through the callback, newer ones a promise.
  function ask(slot) {
    var answered = false;
    function answer() {
      if (answered) {
        return;
      }
      answered = true;
      render(slot, true);
      check();
    }
    try {
      var pending = window.Notification.requestPermission(answer);
      if (pending && typeof pending.then === "function") {
        pending.then(answer, answer);
      }
    } catch (error) {
      answer();
    }
  }

  function start() {
    var slot = document.getElementById(SLOT_ID);
    if (slot) {
      render(slot, false);
    }
    document.body.addEventListener("ccobs:alerts", function () {
      try {
        check();
      } catch (error) {
        // Never to the console: the next ping tries again.
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(window, document);
