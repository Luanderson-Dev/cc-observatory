/*
 * cc-observatory: apply the stored theme before the first paint (ADR 0031).
 *
 * Loaded synchronously in <head>, as an external file (the CSP allows no inline script).
 * "light" or "dark" sets data-theme on <html>; anything else leaves the system preference
 * to the stylesheet. Storage can be unavailable (blocked, private mode): then the system
 * preference applies.
 */
(function () {
  "use strict";
  var choice = null;
  try {
    choice = window.localStorage.getItem("ccobs-theme");
  } catch (error) {
    choice = null;
  }
  if (choice === "light" || choice === "dark") {
    document.documentElement.setAttribute("data-theme", choice);
  }
})();
