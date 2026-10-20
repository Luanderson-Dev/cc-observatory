"""notify.js: browser notifications for new alerts (ADR 0044).

On each ``ccobs:alerts`` event (live.js dispatches one per alerts ping and per stream
open, and a saved settings form sends one through ``HX-Trigger``), a browser that
allowed notifications reads ``/api/v1/alerts?after=<cursor>`` and shows one for each
unseen alert still on that fired less than 5 minutes ago. The cursor is the newest alert
``id`` read, kept in ``localStorage`` for every tab of the origin; the first visit only
sets it. On the alerts page the script also renders the permission control. Checked under
node with fake browser objects (a fake ``Notification``, ``fetch``, ``localStorage`` and
clock); test_static.py scans the file for eval, innerHTML and dialogs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

from ccobs.domain.alerts import AlertRule
from ccobs.domain.presentation import describe_alert

STATIC: Final = Path(__file__).parents[2] / "src" / "ccobs" / "server" / "static"
NOTIFY_JS: Final = STATIC / "js" / "notify.js"
SESSION: Final = "abcdef01-2345-4678-9abc-def012345678"
GRANTED: Final = (
    "This browser shows a notification for each new alert while a tab of the app is open."
)
DENIED: Final = (
    "Notifications are blocked for this site. Allow them in the browser's site settings "
    "to get them."
)
MISSING: Final = "This browser can't show notifications."
ASK: Final = "Notify me in this browser"

# A fake browser around notify.js. Each scenario builds a fresh one (env), drives it and
# returns its log; the harness prints every scenario's log as one JSON object.
HARNESS: Final = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const NOW = 1800000000000;
const KEY = 'ccobs.alerts.cursor';
const SESSION = 'abcdef01-2345-4678-9abc-def012345678';
const URL = '/api/v1/alerts';
const DAILY = {
  rule: 'daily_cost', subject_kind: 'day', session_id: null, project: null,
  value: 52.1, threshold: 50,
};

function alert(id, extra) {
  return Object.assign({
    id, rule: 'waiting_user', subject_kind: 'session', session_id: SESSION, project: 'shop',
    fired_at_ms: NOW - 10000, cleared_at_ms: null, value: 32, threshold: 30,
  }, extra || {});
}

function env(opts) {
  opts = opts || {};
  const log = [];
  const store = {};
  if (opts.stored !== undefined) { store[KEY] = opts.stored; }
  const routes = opts.routes || {};
  const shown = [];
  const state = { now: NOW };

  class El {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this._text = '';
      this.attributes = {}; this.handlers = {}; this.className = ''; this.type = '';
    }
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
    addEventListener(name, handler) {
      (this.handlers[name] = this.handlers[name] || []).push(handler);
    }
    focus() { document.activeElement = this; log.push('focus:' + this.tagName); }
    click() { (this.handlers.click || []).forEach((h) => h({ type: 'click' })); }
    describe() {
      const attrs = Object.keys(this.attributes)
        .map((k) => ' ' + k + '=' + this.attributes[k]).join('');
      const type = this.type ? ' type=' + this.type : '';
      const name = this.tagName.toLowerCase() + (this.className ? '.' + this.className : '');
      return '<' + name + type + attrs + '>'
        + this._text + this.children.map((c) => c.describe()).join('');
    }
  }

  const slot = opts.slot ? new El('div') : null;
  const bodyHandlers = {};
  const document = {
    readyState: 'complete', activeElement: null,
    body: {
      addEventListener(name, handler) {
        (bodyHandlers[name] = bodyHandlers[name] || []).push(handler);
      },
    },
    getElementById(id) { return id === 'alert-notifications' ? slot : null; },
    createElement(tag) { return new El(tag); },
    addEventListener() {},
  };

  class FakeNotification {
    constructor(title, options) {
      if (opts.notificationThrows) {
        log.push('notification-throws');
        throw new TypeError('Illegal constructor');
      }
      this.title = title; this.options = options; this.handlers = {};
      shown.push(this);
      log.push('notify:' + title + '|' + options.body + '|' + options.tag
        + (Object.keys(options).sort().join(',') === 'body,tag'
          ? '' : '|extra:' + Object.keys(options)));
    }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    close() { log.push('close'); }
  }
  FakeNotification.permission = opts.permission || 'granted';
  let answer = null;
  FakeNotification.requestPermission = function (callback) {
    log.push('request');
    if (opts.callbackOnly) {
      answer = (value) => { FakeNotification.permission = value; callback(value); };
      return undefined;
    }
    return new Promise((resolve) => {
      answer = (value) => { FakeNotification.permission = value; resolve(value); };
    });
  };

  function fetch(url, init) {
    log.push('fetch:' + url);
    if (init && (init.method && init.method !== 'GET')) { log.push('method:' + init.method); }
    if (init && init.credentials !== 'same-origin') { log.push('credentials:' + init.credentials); }
    let route = routes[url];
    if (typeof route === 'function') { route = route(); }
    if (route === undefined || route === 'error') {
      return Promise.reject(new TypeError('Failed to fetch'));
    }
    const status = route.status || 200;
    const body = route.status ? route.body : route;
    return Promise.resolve({
      ok: status >= 200 && status < 300, status,
      json() {
        if (route === 'bad-json') { return Promise.reject(new SyntaxError('bad')); }
        return Promise.resolve(body);
      },
    });
  }

  const storage = {
    getItem(key) { return key in store ? store[key] : null; },
    setItem(key, value) { store[key] = String(value); log.push('store:' + value); },
    removeItem(key) { delete store[key]; },
  };
  const window = {
    Date: { now: () => state.now },
    fetch: opts.noFetch ? undefined : fetch,
    focus() { log.push('window-focus'); },
    location: { assign(url) { log.push('navigate:' + url); } },
  };
  if (!opts.noApi) { window.Notification = FakeNotification; }
  if (opts.storage === 'throws') {
    Object.defineProperty(window, 'localStorage', { get() { throw new Error('SecurityError'); } });
  } else if (opts.storage === 'methods-throw') {
    window.localStorage = {
      getItem() { throw new Error('denied'); },
      setItem() { throw new Error('denied'); },
    };
  } else {
    window.localStorage = storage;
  }

  new Function('window', 'document', src)(window, document);

  return {
    log, store, shown, state, slot, document, window, Notification: FakeNotification,
    ping() {
      (bodyHandlers['ccobs:alerts'] || []).forEach((h) => {
        try { h({ type: 'ccobs:alerts' }); } catch (error) { log.push('thrown:' + error.message); }
      });
    },
    answer(value) { answer(value); },
    render() { return slot ? slot.describe() : null; },
    listeners() { return Object.keys(bodyHandlers); },
  };
}

async function settle() {
  for (let n = 0; n < 20; n += 1) { await new Promise((resolve) => setImmediate(resolve)); }
}

const unhandled = [];
process.on('unhandledRejection', (error) => { unhandled.push(String(error)); });

const SCENARIOS = {
  async first_run() {
    const e = env({ routes: {
      [URL + '?limit=1']: [alert(7)],
      [URL + '?after=7&limit=50']: [],
    } });
    e.log.push('listens:' + e.listeners());
    e.ping(); await settle();
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    e.ping(); await settle();
    return e.log;
  },
  async first_run_empty() {
    const e = env({ routes: { [URL + '?limit=1']: [], [URL + '?after=0&limit=50']: [alert(1)] } });
    e.ping(); await settle();
    e.ping(); await settle();
    return e.log;
  },
  async first_run_wrong_shape() {
    const e = env({ routes: { [URL + '?limit=1']: [{ id: -1 }] } });
    e.ping(); await settle();
    return e.log;
  },
  async new_alert() {
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: [alert(8)],
      [URL + '?after=8&limit=50']: [],
      [URL + '?limit=1']: [alert(8)],
    } });
    e.ping(); await settle();
    // The same ping again, and the settings save's HX-Trigger: nothing new.
    e.ping(); await settle();
    e.ping(); await settle();
    return e.log;
  },
  async rules() {
    const e = env({ stored: '0', routes: { [URL + '?after=0&limit=50']: [
      alert(1, { project: null }),
      alert(2, { rule: 'repeated_failures', value: 3, threshold: 3 }),
      alert(3, { rule: 'session_cost', value: 12.5, threshold: 10 }),
      alert(4, DAILY),
      alert(5, { value: 134 }),
    ] } });
    e.ping(); await settle();
    return e.log;
  },
  async skipped() {
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: [
        alert(8, { cleared_at_ms: NOW - 1000 }),
        alert(9, { fired_at_ms: NOW - 300000 }),
        alert(10, { fired_at_ms: NOW - 299999 }),
        alert(11, { rule: 'something_new' }),
        alert(12, { value: 'x' }),
        alert(13, { subject_kind: 'session', session_id: null }),
      ],
    } });
    e.ping(); await settle();
    return e.log;
  },
  async pagination() {
    const full = [];
    for (let id = 8; id < 58; id += 1) { full.push(alert(id, { fired_at_ms: NOW - 600000 })); }
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: full,
      [URL + '?after=57&limit=50']: [alert(58)],
    } });
    e.ping(); await settle();
    return e.log;
  },
  async other_tab() {
    // Another tab moved the shared cursor to 8 while this one was reading.
    let e = null;
    e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: () => {
        e.store['ccobs.alerts.cursor'] = '8';
        return [alert(8), alert(9)];
      },
    } });
    e.ping(); await settle();
    return e.log;
  },
  async overlapping_pings() {
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: [alert(8)],
      [URL + '?after=8&limit=50']: [],
      [URL + '?limit=1']: [alert(8)],
    } });
    e.ping(); e.ping(); e.ping();
    await settle();
    return e.log;
  },
  async session_click() {
    const e = env({ stored: '7', routes: { [URL + '?after=7&limit=50']: [alert(8)] } });
    e.ping(); await settle();
    let prevented = false;
    e.shown[0].handlers.click({ preventDefault() { prevented = true; } });
    e.log.push('prevented:' + prevented);
    return e.log;
  },
  async daily_click() {
    const e = env({ stored: '7', routes: { [URL + '?after=7&limit=50']: [
      alert(8, DAILY),
    ] } });
    e.ping(); await settle();
    e.shown[0].handlers.click({ preventDefault() {} });
    return e.log;
  },
  async fetch_errors() {
    let calls = 0;
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: () => {
        calls += 1;
        return [
          undefined, 'error', { status: 500, body: [] }, 'bad-json',
          { status: 200, body: { detail: [] } }, [{ id: 'eight' }], [null], [alert(8)],
        ][calls];
      },
    } });
    for (let n = 0; n < 7; n += 1) { e.ping(); await settle(); }
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    return e.log;
  },
  async no_progress() {
    // A page whose ids do not go past the cursor is not trusted, and not followed up.
    const e = env({ stored: '7', routes: { [URL + '?after=7&limit=50']: [alert(7), alert(9)] } });
    e.ping(); await settle();
    return e.log;
  },
  async storage_throws() {
    let newest = 7;
    const e = env({ storage: 'throws', routes: {
      [URL + '?limit=1']: () => [alert(newest)],
      [URL + '?after=7&limit=50']: [alert(8)],
      [URL + '?after=8&limit=50']: [],
      [URL + '?after=3&limit=50']: [alert(4)],
    } });
    e.ping(); await settle();
    newest = 8;
    e.ping(); await settle();
    e.ping(); await settle();
    // The app's data was wiped: the cursor in memory comes down too.
    e.log.push('wiped');
    newest = 3;
    e.ping(); await settle();
    e.ping(); await settle();
    return e.log;
  },
  async storage_methods_throw() {
    const e = env({ storage: 'methods-throw', routes: {
      [URL + '?limit=1']: [alert(7)],
      [URL + '?after=7&limit=50']: [alert(8)],
    } });
    e.ping(); await settle();
    e.ping(); await settle();
    return e.log;
  },
  async bad_stored_cursor() {
    const e = env({ stored: 'nine', routes: { [URL + '?limit=1']: [alert(7)] } });
    e.ping(); await settle();
    return e.log;
  },
  async stale_cursor() {
    // The data dir was wiped (or another instance took the origin): ids restart at 1.
    const e = env({ stored: '500', routes: {
      [URL + '?after=500&limit=50']: [],
      [URL + '?limit=1']: [alert(3)],
      [URL + '?after=3&limit=50']: [alert(4)],
    } });
    e.ping(); await settle();
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    e.ping(); await settle();
    return e.log;
  },
  async stale_cursor_no_alerts() {
    const e = env({ stored: '500', routes: {
      [URL + '?after=500&limit=50']: [], [URL + '?limit=1']: [],
    } });
    e.ping(); await settle();
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    return e.log;
  },
  async stale_check_bad_answers() {
    // The newest alert's read fails or has the wrong shape: the cursor stays.
    let calls = 0;
    const e = env({ stored: '500', routes: {
      [URL + '?after=500&limit=50']: [],
      [URL + '?limit=1']: () => {
        calls += 1;
        return [undefined, [{ id: 'three' }], [null], { status: 500, body: [] }][calls];
      },
    } });
    for (let n = 0; n < 3; n += 1) { e.ping(); await settle(); }
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    return e.log;
  },
  async full_then_empty() {
    // Only an empty first page checks the newest alert: a follow-up page may be empty.
    const full = [];
    for (let id = 8; id < 58; id += 1) { full.push(alert(id, { fired_at_ms: NOW - 600000 })); }
    const e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: full,
      [URL + '?after=57&limit=50']: [],
    } });
    e.ping(); await settle();
    return e.log.filter((line) => !line.startsWith('store:'));
  },
  async other_tab_ahead() {
    // Another tab moved the shared cursor past this tab's page: it never goes back.
    let e = null;
    e = env({ stored: '7', routes: {
      [URL + '?after=7&limit=50']: () => {
        e.store['ccobs.alerts.cursor'] = '10';
        return [alert(8), alert(9), alert(11)];
      },
    } });
    e.ping(); await settle();
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    return e.log;
  },
  async browser_clock_behind() {
    // fired_at_ms is the server's clock; a browser clock behind it still shows the alert.
    const e = env({ stored: '7', routes: { [URL + '?after=7&limit=50']: [
      alert(8, { fired_at_ms: NOW + 600000 }),
    ] } });
    e.ping(); await settle();
    return e.log;
  },
  async notification_throws() {
    const e = env({
      stored: '7', notificationThrows: true, routes: { [URL + '?after=7&limit=50']: [alert(8)] },
    });
    e.ping(); await settle();
    e.log.push('cursor:' + e.store['ccobs.alerts.cursor']);
    return e.log;
  },
  async permission_default() {
    const e = env({ permission: 'default', slot: true, routes: {
      [URL + '?limit=1']: [alert(7)],
    } });
    e.log.push('render:' + e.render());
    // Not allowed yet: a ping reads nothing.
    e.ping(); await settle();
    e.log.push('clicking');
    e.slot.children[0].focus();
    e.slot.children[0].click();
    e.answer('granted');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async permission_focus_moved() {
    // The user moved on while the browser asked: the focus stays where they put it.
    const e = env({ permission: 'default', slot: true, routes: { [URL + '?limit=1']: [] } });
    e.slot.children[0].focus();
    e.slot.children[0].click();
    e.document.createElement('input').focus();
    e.answer('granted');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async permission_focus_on_body() {
    const e = env({ permission: 'default', slot: true });
    e.slot.children[0].click();
    e.document.activeElement = e.document.body;
    e.answer('denied');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async permission_refused() {
    const e = env({ permission: 'default', slot: true });
    e.slot.children[0].click();
    e.answer('denied');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async permission_dismissed() {
    const e = env({ permission: 'default', slot: true });
    e.slot.children[0].click();
    e.answer('default');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async permission_callback_only() {
    const e = env({
      permission: 'default', slot: true, callbackOnly: true, routes: { [URL + '?limit=1']: [] },
    });
    e.slot.children[0].click();
    e.answer('granted');
    await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async granted_slot() {
    const e = env({ permission: 'granted', slot: true });
    return [e.render()];
  },
  async denied_slot() {
    const e = env({ permission: 'denied', slot: true, routes: {} });
    e.ping(); await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async missing_api() {
    const e = env({ noApi: true, slot: true, routes: {} });
    e.ping(); await settle();
    e.log.push('render:' + e.render());
    return e.log;
  },
  async missing_fetch() {
    const e = env({ noFetch: true, stored: '7' });
    e.ping(); await settle();
    return e.log;
  },
  async other_page() {
    const e = env({ stored: '7', routes: { [URL + '?after=7&limit=50']: [alert(8)] } });
    e.log.push('render:' + e.render());
    e.ping(); await settle();
    return e.log;
  },
};

(async () => {
  const results = {};
  for (const name of Object.keys(SCENARIOS)) {
    try {
      results[name] = await SCENARIOS[name]();
    } catch (error) {
      // A scenario that cannot go on (no notification to click): its log says why.
      results[name] = ['crashed:' + error.message];
    }
  }
  await settle();
  results.unhandled = unhandled;
  console.log(JSON.stringify(results));
})();
"""

# The words notify.js gives each alert, compared with describe_alert's.
TEXT_HARNESS: Final = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const window = {};
const document = { readyState: 'loading', addEventListener() {} };
new Function('window', 'document', src)(window, document);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map((c) => window.ccobsAlertText({
  id: 1, rule: c[0], value: c[1], threshold: c[2], subject_kind: 'day', session_id: null,
  project: null, fired_at_ms: 0, cleared_at_ms: null,
}).body)));
"""

TEXT_CASES: Final[tuple[tuple[AlertRule, float, float], ...]] = (
    ("waiting_user", 32, 30),
    ("waiting_user", 134, 30),
    ("waiting_user", 0.4, 0),
    ("waiting_user", 3_780, 60),
    ("waiting_user", 90_000, 60),
    ("repeated_failures", 3, 3),
    ("repeated_failures", 12, 5),
    ("session_cost", 12.5, 10),
    ("session_cost", 0.5, 0.25),
    ("session_cost", 0, 0),
    ("session_cost", 0.00001, 0),
    ("session_cost", 1_234.567, 1_000),
    ("daily_cost", 52.1, 50),
    ("daily_cost", 1_234_567.891, 12.5),
    ("daily_cost", 75, 1_000_000),
)


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        # CI runners have node: there a missing node is a failure, not a skip.
        if os.environ.get("CI"):
            pytest.fail("node is not installed, and CI must run this test")
        pytest.skip("node is not installed")
    return node


def _run(script: str, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - node from PATH, fixed arguments
        [_node(), "-e", script, str(NOTIFY_JS), *arguments],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.stderr == ""
    return completed.stdout


@pytest.fixture(scope="module")
def logs() -> dict[str, list[str]]:
    results: dict[str, list[str]] = json.loads(_run(HARNESS))
    return results


def notified(title: str, body: str, alert_id: int) -> str:
    return f"notify:cc-observatory: {title}|{body}|ccobs-alert-{alert_id}"


WAITING: Final = notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 8)


def test_nothing_escapes_to_the_console(logs: dict[str, list[str]]) -> None:
    assert logs["unhandled"] == []
    for name, log in logs.items():
        assert not [line for line in log if line.startswith("thrown:")], name


def test_the_first_run_sets_the_cursor_to_the_newest_alert_and_shows_nothing(
    logs: dict[str, list[str]],
) -> None:
    assert logs["first_run"] == [
        "listens:ccobs:alerts",
        "fetch:/api/v1/alerts?limit=1",
        "store:7",
        "cursor:7",
        # Nothing after the cursor: the newest alert is read, and it is not below it.
        "fetch:/api/v1/alerts?after=7&limit=50",
        "fetch:/api/v1/alerts?limit=1",
    ]


def test_the_first_run_with_no_alert_starts_from_zero(logs: dict[str, list[str]]) -> None:
    assert logs["first_run_empty"] == [
        "fetch:/api/v1/alerts?limit=1",
        "store:0",
        "fetch:/api/v1/alerts?after=0&limit=50",
        "store:1",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 1),
    ]


def test_a_first_run_with_a_wrong_answer_sets_no_cursor(logs: dict[str, list[str]]) -> None:
    assert logs["first_run_wrong_shape"] == ["fetch:/api/v1/alerts?limit=1"]


def test_a_new_alert_still_on_shows_one_notification_once(logs: dict[str, list[str]]) -> None:
    assert logs["new_alert"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        WAITING,
        # The same ping again, and the HX-Trigger after a save: read, nothing new.
        "fetch:/api/v1/alerts?after=8&limit=50",
        "fetch:/api/v1/alerts?limit=1",
        "fetch:/api/v1/alerts?after=8&limit=50",
        "fetch:/api/v1/alerts?limit=1",
    ]


def test_each_rule_has_its_title_and_body(logs: dict[str, list[str]]) -> None:
    assert [line for line in logs["rules"] if line.startswith("notify:")] == [
        notified("waiting on you", "Unknown project (abcdef01): Waiting on you for over 32s", 1),
        notified("API requests failing", "shop (abcdef01): API requests failed 3 times", 2),
        notified(
            "session cost over the limit",
            "shop (abcdef01): This session cost $12.50 (limit $10)",
            3,
        ),
        notified("daily cost over the limit", "The last 24 hours cost $52.10 (limit $50)", 4),
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 2m 14s", 5),
    ]


def test_cleared_old_or_unknown_alerts_are_skipped_but_move_the_cursor(
    logs: dict[str, list[str]],
) -> None:
    assert logs["skipped"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        # Fired exactly 5 minutes ago: too old.
        "store:9",
        "store:10",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 10),
        # A rule this script does not know, a value that is not a number, a session alert
        # without its session: not shown, still read.
        "store:11",
        "store:12",
        "store:13",
    ]


def test_a_full_page_is_followed_up(logs: dict[str, list[str]]) -> None:
    log = logs["pagination"]
    assert log[0] == "fetch:/api/v1/alerts?after=7&limit=50"
    assert log[1:51] == [f"store:{alert_id}" for alert_id in range(8, 58)]
    assert log[51:] == [
        "fetch:/api/v1/alerts?after=57&limit=50",
        "store:58",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 58),
    ]


def test_an_alert_another_tab_already_read_is_not_shown_again(
    logs: dict[str, list[str]],
) -> None:
    assert logs["other_tab"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:9",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 9),
    ]


def test_pings_during_a_read_make_one_more_read_after_it(logs: dict[str, list[str]]) -> None:
    assert logs["overlapping_pings"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        WAITING,
        "fetch:/api/v1/alerts?after=8&limit=50",
        "fetch:/api/v1/alerts?limit=1",
    ]


def test_a_click_focuses_the_tab_and_opens_the_session(logs: dict[str, list[str]]) -> None:
    assert logs["session_click"][-5:] == [
        WAITING,
        "window-focus",
        f"navigate:/sessions/{SESSION}",
        "close",
        "prevented:true",
    ]


def test_a_click_on_a_daily_alert_opens_the_alerts_page(logs: dict[str, list[str]]) -> None:
    assert logs["daily_click"][-3:] == ["window-focus", "navigate:/alerts", "close"]


def test_failed_reads_and_wrong_shapes_are_ignored_until_the_next_ping(
    logs: dict[str, list[str]],
) -> None:
    fetch = "fetch:/api/v1/alerts?after=7&limit=50"
    # A network error, a 500, a body that is not JSON, JSON that is not a list, an item
    # with no numeric id, an item that is not an object; then the alert.
    assert logs["fetch_errors"] == [
        fetch,
        fetch,
        fetch,
        fetch,
        fetch,
        fetch,
        fetch,
        "store:8",
        WAITING,
        "cursor:8",
    ]


def test_a_page_that_does_not_move_past_the_cursor_is_dropped(
    logs: dict[str, list[str]],
) -> None:
    assert logs["no_progress"] == ["fetch:/api/v1/alerts?after=7&limit=50"]


def test_without_local_storage_the_cursor_lives_in_memory(logs: dict[str, list[str]]) -> None:
    expected = [
        "fetch:/api/v1/alerts?limit=1",
        "fetch:/api/v1/alerts?after=7&limit=50",
        WAITING,
    ]
    assert logs["storage_throws"] == [
        *expected,
        "fetch:/api/v1/alerts?after=8&limit=50",
        "fetch:/api/v1/alerts?limit=1",
        "wiped",
        "fetch:/api/v1/alerts?after=8&limit=50",
        "fetch:/api/v1/alerts?limit=1",
        "fetch:/api/v1/alerts?after=3&limit=50",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 4),
    ]
    assert logs["storage_methods_throw"] == expected


def test_a_stored_cursor_that_is_not_an_id_counts_as_none(logs: dict[str, list[str]]) -> None:
    assert logs["bad_stored_cursor"] == ["fetch:/api/v1/alerts?limit=1", "store:7"]


def test_a_cursor_past_the_newest_alert_comes_down_to_it_showing_nothing(
    logs: dict[str, list[str]],
) -> None:
    assert logs["stale_cursor"] == [
        "fetch:/api/v1/alerts?after=500&limit=50",
        "fetch:/api/v1/alerts?limit=1",
        "store:3",
        "cursor:3",
        "fetch:/api/v1/alerts?after=3&limit=50",
        "store:4",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 4),
    ]
    assert logs["stale_cursor_no_alerts"] == [
        "fetch:/api/v1/alerts?after=500&limit=50",
        "fetch:/api/v1/alerts?limit=1",
        "store:0",
        "cursor:0",
    ]


def test_a_failed_or_wrong_newest_alert_read_keeps_the_cursor(
    logs: dict[str, list[str]],
) -> None:
    pair = ["fetch:/api/v1/alerts?after=500&limit=50", "fetch:/api/v1/alerts?limit=1"]
    # An item with no numeric id, an item that is not an object, a 500.
    assert logs["stale_check_bad_answers"] == [*pair, *pair, *pair, "cursor:500"]


def test_an_empty_follow_up_page_does_not_check_the_newest_alert(
    logs: dict[str, list[str]],
) -> None:
    assert logs["full_then_empty"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "fetch:/api/v1/alerts?after=57&limit=50",
    ]


def test_the_cursor_never_goes_below_one_another_tab_stored(
    logs: dict[str, list[str]],
) -> None:
    assert logs["other_tab_ahead"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:11",
        notified("waiting on you", "shop (abcdef01): Waiting on you for over 32s", 11),
        "cursor:11",
    ]


def test_an_alert_fired_ahead_of_the_browser_clock_still_shows(
    logs: dict[str, list[str]],
) -> None:
    assert logs["browser_clock_behind"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        WAITING,
    ]


def test_a_notification_the_browser_refuses_still_moves_the_cursor(
    logs: dict[str, list[str]],
) -> None:
    assert logs["notification_throws"] == [
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        "notification-throws",
        "cursor:8",
    ]


def test_the_alerts_page_offers_a_button_that_asks_only_on_click(
    logs: dict[str, list[str]],
) -> None:
    assert logs["permission_default"] == [
        f"render:<div><button.alert-notifications__ask type=button>{ASK}",
        # A ping before the user allows: nothing read, nothing asked.
        "clicking",
        "focus:BUTTON",
        "request",
        # The answer replaces the button with the sentence, which takes the focus, and the
        # first read only sets the cursor.
        "focus:P",
        "fetch:/api/v1/alerts?limit=1",
        "store:7",
        f"render:<div><p.alert-notifications__text tabindex=-1>{GRANTED}",
    ]


def test_the_answer_moves_the_focus_only_from_the_button_or_the_body(
    logs: dict[str, list[str]],
) -> None:
    assert logs["permission_focus_moved"] == [
        "focus:BUTTON",
        "request",
        "focus:INPUT",
        "fetch:/api/v1/alerts?limit=1",
        "store:0",
        f"render:<div><p.alert-notifications__text>{GRANTED}",
    ]
    assert logs["permission_focus_on_body"] == [
        "request",
        "focus:P",
        f"render:<div><p.alert-notifications__text tabindex=-1>{DENIED}",
    ]


def test_a_refused_or_dismissed_request_says_so_or_offers_the_button_again(
    logs: dict[str, list[str]],
) -> None:
    assert logs["permission_refused"] == [
        "request",
        "focus:P",
        f"render:<div><p.alert-notifications__text tabindex=-1>{DENIED}",
    ]
    assert logs["permission_dismissed"] == [
        "request",
        "focus:BUTTON",
        f"render:<div><button.alert-notifications__ask type=button>{ASK}",
    ]


def test_an_older_browser_answers_through_the_callback(logs: dict[str, list[str]]) -> None:
    assert logs["permission_callback_only"] == [
        "request",
        "focus:P",
        "fetch:/api/v1/alerts?limit=1",
        "store:0",
        f"render:<div><p.alert-notifications__text tabindex=-1>{GRANTED}",
    ]


def test_the_control_says_what_the_browser_does(logs: dict[str, list[str]]) -> None:
    assert logs["granted_slot"] == [f"<div><p.alert-notifications__text>{GRANTED}"]
    assert logs["denied_slot"] == [f"render:<div><p.alert-notifications__text>{DENIED}"]
    assert logs["missing_api"] == [f"render:<div><p.alert-notifications__text>{MISSING}"]
    assert logs["missing_fetch"] == []


def test_other_pages_render_no_control_and_still_notify(logs: dict[str, list[str]]) -> None:
    assert logs["other_page"] == [
        "render:null",
        "fetch:/api/v1/alerts?after=7&limit=50",
        "store:8",
        WAITING,
    ]


def test_the_wording_matches_describe_alert() -> None:
    bodies = json.loads(_run(TEXT_HARNESS, json.dumps(TEXT_CASES)))

    expected = [
        describe_alert(rule, value, threshold, cleared=False)
        for rule, value, threshold in TEXT_CASES
    ]
    assert bodies == expected
