'use strict';
// Проверяем настоящий app.js в DOM-окружении без браузера и npm-зависимостей.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/js/app.js'), 'utf8');

function boot(elements = {}, response = {}) {
  const handlers = {};
  const intervals = [];
  const state = {fetches: [], bodies: [], reloads: 0, timers: []};
  const document = {
    hidden: false,
    documentElement: {getAttribute() { return null; }},
    addEventListener(name, fn) { (handlers[name] ||= []).push(fn); },
    querySelector(selector) { return elements[selector] || null; },
    querySelectorAll(selector) { return elements[selector + '[]'] || []; },
    createElement(tag) { return el(tag); },
    getElementById() { return null; }
  };
  const context = {
    document,
    window: {location: {reload() { state.reloads++; }}},
    setInterval(fn) { intervals.push(fn); },
    setTimeout(fn) { state.timers.push(fn); },
    URLSearchParams,
    fetch(url, options = {}) {
      state.fetches.push(url);
      if (options.body) state.bodies.push(options.body);
      const answer = typeof response === 'function' ? response(url, options) : response;
      return Promise.resolve({json: () => Promise.resolve(answer)});
    }
  };
  vm.runInNewContext(source, context);
  // Пауза с разбором ошибки в тестах не ждёт по-настоящему.
  state.fireTimers = () => { const due = state.timers.splice(0); due.forEach((fn) => fn()); };
  return {document, handlers, intervals, state};
}

// ── Крошечная модель DOM: хватает селекторов, которые встречаются в app.js ──
function matchesOne(node, selector) {
  const tokens = selector.trim().match(/:not\([^)]*\)|\[[^\]]*\]|\.[\w-]+|^[a-z]+/g) || [];
  return tokens.every((token) => {
    if (token.startsWith(':not(')) return !matchesOne(node, token.slice(5, -1));
    if (token.startsWith('[')) {
      const [name, value] = token.slice(1, -1).split('=');
      const actual = node.getAttribute(name);
      return value === undefined ? actual !== null : actual === value;
    }
    if (token.startsWith('.')) {
      return (node.getAttribute('class') || '').split(/\s+/).includes(token.slice(1));
    }
    return node.tag === token;
  });
}

function matches(node, selector) {
  return selector.split(',').some((part) => matchesOne(node, part));
}

function el(tag, attrs = {}, children = []) {
  const listeners = {};
  const classes = new Set((attrs.class || '').split(/\s+/).filter(Boolean));
  const node = {
    tag,
    children,
    parent: null,
    disabled: false,
    value: attrs.value || '',
    textContent: '',
    innerHTML: '',
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name))
    },
    getAttribute(name) {
      if (name === 'class') return [...classes].join(' ');
      return name in attrs ? attrs[name] : null;
    },
    setAttribute(name, value) { attrs[name] = value; },
    hasAttribute(name) { return name in attrs; },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    appendChild(child) { child.parent = node; children.push(child); },
    querySelectorAll(selector) { return descendants(node).filter((n) => matches(n, selector)); },
    querySelector(selector) { return node.querySelectorAll(selector)[0] || null; },
    closest(selector) {
      for (let at = node; at; at = at.parent) if (matches(at, selector)) return at;
      return null;
    },
    listeners
  };
  children.forEach((child) => { child.parent = node; });
  return node;
}

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

// Событие всплывает: app.js ловит клики и на самой кнопке, и на всей капче.
function dispatch(node, type) {
  for (let at = node; at; at = at.parent) {
    for (const fn of at.listeners[type] || []) fn({target: node});
  }
}

function captcha(kind, controls) {
  const box = el('div', {'data-captcha': '', 'data-id': 'challenge-id', 'data-kind': kind}, [
    el('button', {'data-captcha-reload': ''}),
    ...controls,
    el('div', {'data-captcha-feedback': ''})
  ]);
  return box;
}

function studentPage(box, response, next = null) {
  let current = box;
  const slot = {
    querySelector() { return current; },
    hasAttribute() { return false; },
    set innerHTML(value) { current = next; }
  };
  const hidden = {value: 'challenge-id'};
  const submit = {classList: {toggle() {}}, disabled: true, textContent: ''};
  const page = boot({
    '[data-captcha-slot]': slot,
    '[data-captcha-input]': hidden,
    '[data-join-submit]': submit
  }, response);
  return {...page, hidden, submit, box: () => current};
}

function queuePage(open, response, options = {}) {
  const attrs = {
    'data-session-id': open ? '1' : '',
    'data-session-revision': open ? 'version-one' : '',
    'data-mine-status': options.mineStatus || '',
    'data-poll': '15'
  };
  const list = {innerHTML: 'old list'};
  const badge = {textContent: ''};
  const ahead = {textContent: ''};
  const elements = {
    '[data-queue-status]': {
      classList: {contains() { return open; }},
      getAttribute(name) { return attrs[name] || ''; }
    },
    '[data-waiting-badge]': badge,
    '[data-ahead]': ahead
  };
  if (open) elements['[data-queue-list]'] = list;
  return {...boot(elements, response), list, badge, ahead};
}

const openQueue = {open: true, session_id: 1, session_revision: 'version-one',
  mine_status: '', html: 'new list', waiting: 2, mine: null};
const closedQueue = {open: false, session_id: null, session_revision: '', mine_status: '', html: ''};

async function poll(page) {
  assert.equal(page.intervals.length, 1);
  page.intervals[0]();
  await new Promise(setImmediate);
}

test('closed page polls and notices opening without a queue-list element', async () => {
  const page = queuePage(false, openQueue);
  await poll(page);
  assert.deepEqual(page.state.fetches, ['/api/queue']);
  assert.equal(page.state.reloads, 1);
});

test('closed page stays stable while reception remains closed', async () => {
  const page = queuePage(false, closedQueue);
  await poll(page);
  assert.equal(page.state.reloads, 0);
});

test('open page notices reception closing', async () => {
  const page = queuePage(true, closedQueue);
  await poll(page);
  assert.equal(page.state.reloads, 1);
});

test('close and reopen between polls refreshes the whole page', async () => {
  const page = queuePage(true, {...openQueue, session_id: 2});
  await poll(page);
  assert.equal(page.state.reloads, 1);
  assert.equal(page.list.innerHTML, 'old list');
});

test('room, time or note revision refreshes the whole page', async () => {
  const page = queuePage(true, {...openQueue, session_revision: 'edited'});
  await poll(page);
  assert.equal(page.state.reloads, 1);
});

test('teacher status change refreshes own ticket', async () => {
  const page = queuePage(true, {...openQueue, mine_status: 'done'}, {mineStatus: 'waiting'});
  await poll(page);
  assert.equal(page.state.reloads, 1);
});

test('ordinary poll updates list, counters and position without reload', async () => {
  const page = queuePage(true, {...openQueue, mine_status: 'waiting', mine: 2, ahead: 1},
    {mineStatus: 'waiting'});
  await poll(page);
  assert.equal(page.state.reloads, 0);
  assert.equal(page.list.innerHTML, 'new list');
  assert.equal(page.badge.textContent, 'ждут: 2');
  assert.equal(page.ahead.textContent, 'Перед вами: 1 человек');
});

test('polling resumes when the page becomes visible', async () => {
  const page = queuePage(false, openQueue);
  page.document.hidden = true;
  await poll(page);
  assert.equal(page.state.fetches.length, 0);
  page.document.hidden = false;
  page.handlers.visibilitychange[0]();
  await new Promise(setImmediate);
  assert.equal(page.state.reloads, 1);
});

function typeGroup(value, mask) {
  const page = boot();
  const input = {value, selectionStart: value.length, setSelectionRange() {},
    getAttribute() { return mask; }};
  page.handlers.input[0]({target: {closest() { return input; }}});
  return input.value;
}

test('standard group retains convenient formatting', () => {
  assert.equal(typeGroup('кт2404', 'aa-00-00'), 'КТ-24-04');
});

test('custom group formats are not truncated or rewritten', () => {
  for (const value of ['КСП-24-04', '2026/42', 'ABC-1234', 'ГРУППА_1']) {
    assert.equal(typeGroup(value, ''), value);
  }
});

function captchaBox() {
  const clicks = [];
  const button = {addEventListener(_, fn) { clicks.push(fn); }};
  return {
    clicks,
    querySelector(selector) { return selector === '[data-captcha-reload]' ? button : null; },
    getAttribute(name) { return name === 'data-kind' ? 'quiz' : 'challenge-id'; },
    addEventListener() {}
  };
}

test('admin preview reload has one handler and preserves selected type via page reload', () => {
  const box = captchaBox();
  const slot = {querySelector() { return box; }, hasAttribute() { return true; }};
  const page = boot({'[data-captcha-slot]': slot});
  assert.equal(box.clicks.length, 1);
  box.clicks[0]();
  assert.equal(page.state.reloads, 1);
  assert.equal(page.state.fetches.length, 0);
});

test('student captcha reload asks for another type and binds the new button', async () => {
  const first = captchaBox();
  const next = captchaBox();
  let current = first;
  const slot = {
    querySelector() { return current; },
    hasAttribute() { return false; },
    set innerHTML(value) { current = next; }
  };
  const hidden = {value: 'old-id'};
  const page = boot({'[data-captcha-slot]': slot, '[data-captcha-input]': hidden},
    {html: 'new captcha', id: 'new-id'});
  first.clicks[0]();
  await new Promise(setImmediate);
  // previous — задание с экрана: сервер не повторит его тип.
  assert.deepEqual(page.state.fetches, ['/api/captcha?previous=challenge-id']);
  assert.equal(page.state.reloads, 0);
  assert.equal(hidden.value, 'new-id');
  assert.equal(next.clicks.length, 1);
});

// ── Типы заданий ──────────────────────────────────────────────────────────

function optionsList(attribute, count, extra = () => []) {
  return Array.from({length: count}, (_, index) =>
    el('button', {[attribute]: '', 'data-index': String(index)}, extra(index)));
}

function portsBox() {
  const selects = [22, 80, 443].map(() => el('select', {'data-port-select': ''}));
  const check = el('button', {'data-ports-check': ''});
  check.disabled = true;
  return {box: captcha('ports', [...selects, check]), selects, check};
}

function orderBox() {
  const options = optionsList('data-order-option', 4,
    () => [el('i', {'data-order-number': ''})]);
  const reset = el('button', {'data-order-reset': ''});
  const check = el('button', {'data-order-check': ''});
  check.disabled = true;
  return {box: captcha('order_steps', [...options, reset, check]), options, reset, check};
}

function numbers(options) {
  return options.map((option) => option.querySelector('[data-order-number]').textContent);
}

const accepted = {ok: true, title: 'Верно', text: 'Разбор'};

test('ports captcha waits for every select and answers in service order', async () => {
  const ports = portsBox();
  const page = studentPage(ports.box, accepted);

  assert.equal(ports.check.disabled, true);
  ports.selects[0].value = '22';
  dispatch(ports.selects[0], 'change');
  assert.equal(ports.check.disabled, true, 'одного порта мало');

  ports.selects[1].value = '443';
  ports.selects[2].value = '80';
  dispatch(ports.selects[2], 'change');
  assert.equal(ports.check.disabled, false);

  dispatch(ports.check, 'click');
  await new Promise(setImmediate);
  assert.deepEqual(page.state.fetches, ['/api/captcha/challenge-id']);
  assert.deepEqual(page.state.bodies, ['answer=22%2C443%2C80']);
  assert.equal(page.submit.disabled, false);
});

test('order captcha numbers the taps and sends the sequence', async () => {
  const order = orderBox();
  const page = studentPage(order.box, accepted);

  assert.deepEqual(numbers(order.options), ['·', '·', '·', '·']);
  [2, 0, 3].forEach((index) => dispatch(order.options[index], 'click'));
  assert.deepEqual(numbers(order.options), ['2', '·', '1', '3']);
  assert.equal(order.check.disabled, true, 'без последнего шага проверять нечего');

  dispatch(order.options[1], 'click');
  assert.equal(order.check.disabled, false);
  dispatch(order.check, 'click');
  await new Promise(setImmediate);
  assert.deepEqual(page.state.bodies, ['answer=2%2C0%2C3%2C1']);
});

test('tapping a chosen step again drops it with everything after it', () => {
  const order = orderBox();
  studentPage(order.box, accepted);
  [0, 1, 2, 3].forEach((index) => dispatch(order.options[index], 'click'));
  dispatch(order.options[1], 'click');
  assert.deepEqual(numbers(order.options), ['1', '·', '·', '·']);
  assert.equal(order.check.disabled, true);

  dispatch(order.reset, 'click');
  assert.deepEqual(numbers(order.options), ['·', '·', '·', '·']);
});

test('wrong answer shows the explanation and swaps in the ready replacement', async () => {
  const wrong = {
    ok: false, expired: true, title: 'Мимо', text: 'Разбор ошибки',
    replacement: {id: 'next-id', kind: 'quiz', html: '<div>другое задание</div>'}
  };
  const first = orderBox();
  const next = portsBox();
  const page = studentPage(first.box, wrong, next.box);

  [0, 1, 2, 3].forEach((index) => dispatch(first.options[index], 'click'));
  dispatch(first.check, 'click');
  await new Promise(setImmediate);

  const feedback = first.box.querySelector('[data-captcha-feedback]');
  assert.equal(feedback.children.map((node) => node.textContent).join(' · '),
    'Мимо · Разбор ошибки');
  assert.equal(first.box.classList.contains('is-solved'), false, 'ошибка не красит как решённое');
  assert.equal(first.check.disabled, true, 'сгоревшее задание больше не отвечает');

  page.state.fireTimers();
  assert.equal(page.box(), next.box, 'на месте уже другое задание');
  assert.equal(page.hidden.value, 'next-id');
  // Замена пришла вместе с разбором — второй раз к серверу не ходим.
  assert.deepEqual(page.state.fetches, ['/api/captcha/challenge-id']);
  assert.equal(next.check.disabled, true, 'новое задание связано с обработчиками');
});

test('expired challenge without a replacement falls back to a fresh request', async () => {
  const expired = {ok: false, expired: true, title: 'Задание устарело', text: 'Возьмите новое.'};
  const quiz = captcha('quiz', optionsList('data-quiz-option', 4));
  const page = studentPage(quiz, expired);

  dispatch(quiz.querySelector('[data-quiz-option]'), 'click');
  await new Promise(setImmediate);
  page.state.fireTimers();
  assert.deepEqual(page.state.fetches,
    ['/api/captcha/challenge-id', '/api/captcha?previous=challenge-id']);
});

// ── Согласие на обработку данных ─────────────────────────────────────────

// Кнопка записи ждёт и решённое задание, и отметку согласия: забытая галочка
// не должна сжигать задание — на замену пришло бы новое.
function consentPage(response) {
  const quiz = captcha('quiz', optionsList('data-quiz-option', 4));
  const slot = {querySelector() { return quiz; }, hasAttribute() { return false; }};
  const submit = {
    classList: {classes: new Set(), toggle(name, on) { on ? this.classes.add(name) : this.classes.delete(name); },
      contains(name) { return this.classes.has(name); }},
    disabled: true,
    textContent: ''
  };
  const consent = el('input', {'data-consent': ''});
  consent.checked = false;
  const page = boot({
    '[data-captcha-slot]': slot,
    '[data-captcha-input]': {value: 'challenge-id'},
    '[data-join-submit]': submit,
    '[data-consent]': consent
  }, response);
  return {...page, quiz, submit, consent};
}

test('solved captcha alone does not unlock the submit button', async () => {
  const page = consentPage(accepted);
  dispatch(page.quiz.querySelector('[data-quiz-option]'), 'click');
  await new Promise(setImmediate);

  assert.equal(page.submit.disabled, true, 'без согласия кнопка заперта');
  assert.equal(page.submit.textContent, 'Отметьте согласие на обработку данных');

  page.consent.checked = true;
  dispatch(page.consent, 'change');
  assert.equal(page.submit.disabled, false);
  assert.equal(page.submit.textContent, 'Встать в очередь');

  // Галочку сняли обратно — отправлять снова нельзя.
  page.consent.checked = false;
  dispatch(page.consent, 'change');
  assert.equal(page.submit.disabled, true);
});

test('consent alone does not unlock the submit button either', () => {
  const page = consentPage(accepted);
  page.consent.checked = true;
  dispatch(page.consent, 'change');
  assert.equal(page.submit.disabled, true);
  assert.equal(page.submit.textContent, 'Решите задание, чтобы встать в очередь');
});

test('preview swaps in the replacement and moves the highlight to its type', async () => {
  const wrong = {
    ok: false, expired: true, title: 'Мимо', text: 'Разбор',
    replacement: {id: 'next-id', kind: 'logs', html: '<div>журнал</div>'}
  };
  const quiz = captcha('quiz', optionsList('data-quiz-option', 4));
  const next = captcha('logs', optionsList('data-quiz-option', 4));
  const links = ['quiz', 'logs'].map((kind) =>
    el('a', {'data-captcha-kind': kind, class: kind === 'quiz' ? 'btn btn--primary' : 'btn btn--ghost'}));
  let current = quiz;
  const slot = {
    querySelector() { return current; },
    hasAttribute() { return true; },
    set innerHTML(value) { current = next; }
  };
  const page = boot({'[data-captcha-slot]': slot, '[data-captcha-kind][]': links}, wrong);

  dispatch(quiz.querySelector('[data-quiz-option]'), 'click');
  await new Promise(setImmediate);
  page.state.fireTimers();

  assert.equal(current, next, 'в предпросмотре тоже приходит замена');
  assert.equal(links[0].getAttribute('class'), 'btn btn--ghost');
  assert.equal(links[1].getAttribute('class'), 'btn btn--primary');
});
