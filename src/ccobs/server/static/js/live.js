/*
 * cc-observatory: live updates (ADR 0032). No inline code, no eval, no innerHTML, no
 * JavaScript dialogs (CSP, ADR 0034).
 *
 * Every page gives #live-status a data-stream URL (/ui/stream, with ?session= on a
 * session page; ADR 0044). This script opens one EventSource to it per tab, and on
 * each "changed" event dispatches a "ccobs:changed" event on document.body: the
 * elements that refetch (hx-trigger="ccobs:changed from:body") do the rest; a page with
 * none simply refetches nothing. The stream's "alerts" pings are not handled here yet.
 * Every "open", the first one and each reconnection, dispatches it too, so the page
 * catches up on whatever changed while it was not listening; no Last-Event-ID is needed.
 *
 * On an error the source is closed, #live-status says that live updates are paused,
 * and the script opens a new source after a delay that doubles from 5 s up to 60 s and
 * starts over once a source opens. Closing it ourselves keeps a refused stream (503,
 * too many tabs) from being retried by the browser every few seconds. The source closes
 * on pagehide and opens again when the page comes back from the back-forward cache.
 */
(function (window, document) {
  "use strict";

  var FIRST_DELAY_MS = 5000;
  var MAX_DELAY_MS = 60000;
  var PAUSED = "Live updates paused. Reconnecting\u2026";

  // The wait before the next attempt, after one that waited previousMs (0: none yet).
  function nextDelay(previousMs) {
    return previousMs > 0 ? Math.min(previousMs * 2, MAX_DELAY_MS) : FIRST_DELAY_MS;
  }

  function changed() {
    document.body.dispatchEvent(new window.Event("ccobs:changed"));
  }

  function start() {
    var status = document.getElementById("live-status");
    var url = status && status.getAttribute("data-stream");
    if (!url || !window.EventSource) {
      return;
    }
    var source = null;
    var timer = null;
    var delay = 0;

    function show(text) {
      if (status.textContent !== text) {
        status.textContent = text;
      }
    }

    function close() {
      if (source) {
        source.close();
        source = null;
      }
      if (timer) {
        window.clearTimeout(timer);
        timer = null;
      }
    }

    function open() {
      timer = null;
      source = new window.EventSource(url);
      source.addEventListener("open", function () {
        delay = 0;
        show("");
        changed();
      });
      source.addEventListener("changed", changed);
      source.addEventListener("error", function () {
        close();
        show(PAUSED);
        delay = nextDelay(delay);
        timer = window.setTimeout(open, delay);
      });
    }

    window.addEventListener("pagehide", close);
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && !source && !timer) {
        open();
      }
    });
    open();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(window, document);
