/* Запись на приём — клиентская часть: тема, маска группы, капча. */
(function () {
  'use strict';

  // ── Тема ──────────────────────────────────────────────────────────────
  var THEME_KEY = 'au-queue-theme';

  function currentTheme() {
    var explicit = document.documentElement.getAttribute('data-theme');
    if (explicit) return explicit;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  }

  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-theme-toggle]');
    if (!toggle) return;
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* приватный режим */ }
  });

  // ── Печать ────────────────────────────────────────────────────────────
  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-print]')) window.print();
  });

  // ── Подтверждения ─────────────────────────────────────────────────────
  document.addEventListener('submit', function (event) {
    var form = event.target;
    var question = form.getAttribute('data-confirm');
    if (question && !window.confirm(question)) event.preventDefault();
  });

  // ── Маска номера группы: АА-00-00 ─────────────────────────────────────
  function formatGroup(raw) {
    var chars = (raw || '').toUpperCase().replace(/[^0-9A-ZА-ЯЁ]/g, '');
    var letters = '';
    var digits = '';
    for (var i = 0; i < chars.length; i++) {
      var ch = chars[i];
      if (/[0-9]/.test(ch)) {
        if (letters.length > 0) digits += ch;
      } else if (letters.length < 2 && digits.length === 0) {
        letters += ch;
      }
    }
    var out = letters;
    if (digits.length) out += '-' + digits.slice(0, 2);
    if (digits.length > 2) out += '-' + digits.slice(2, 4);
    return out;
  }

  document.addEventListener('input', function (event) {
    var input = event.target.closest('[data-group-input]');
    if (!input || input.getAttribute('data-group-input') !== 'aa-00-00') return;
    var atEnd = input.selectionStart === input.value.length;
    var formatted = formatGroup(input.value);
    if (formatted !== input.value) {
      input.value = formatted;
      if (atEnd) input.setSelectionRange(formatted.length, formatted.length);
    }
  });

  // ── Капча ─────────────────────────────────────────────────────────────
  var slot = document.querySelector('[data-captcha-slot]');
  var submitButton = document.querySelector('[data-join-submit]');
  var hiddenInput = document.querySelector('[data-captcha-input]');
  var consentBox = document.querySelector('[data-consent]');
  // Сколько разбор ошибки висит на экране, прежде чем придёт другое задание.
  var WRONG_PAUSE = 2600;
  var solved = false;

  // Кнопка ждёт и решённое задание, и отметку согласия. Проверку того же
  // самого сервер всё равно повторит, но забытая галочка не должна сжигать
  // задание: на замену пришло бы новое, и человек решал бы его заново.
  function refreshSubmit() {
    if (!submitButton) return;
    var consented = !consentBox || consentBox.checked;
    var ready = solved && consented;
    submitButton.disabled = !ready;
    submitButton.classList.toggle('is-disabled', !ready);
    submitButton.textContent = !solved
      ? 'Решите задание, чтобы встать в очередь'
      : !consented
        ? 'Отметьте согласие на обработку данных'
        : 'Встать в очередь';
  }

  function setSubmitReady(ready) {
    solved = ready;
    refreshSubmit();
  }

  if (consentBox) consentBox.addEventListener('change', refreshSubmit);

  function showFeedback(box, result) {
    var feedback = box.querySelector('[data-captcha-feedback]');
    if (!feedback) return;
    feedback.className = 'captcha__feedback ' + (result.ok ? 'is-ok' : 'is-bad');
    feedback.innerHTML = '';
    var title = document.createElement('strong');
    title.textContent = result.title || (result.ok ? 'Верно' : 'Не сходится');
    var text = document.createElement('span');
    text.textContent = result.text || '';
    feedback.appendChild(title);
    feedback.appendChild(text);
  }

  // Сгоревшее задание больше не отвечает, поэтому гасим его управление.
  function freeze(box) {
    var controls = box.querySelectorAll(
      'button:not([data-captcha-reload]), input[type=range], select'
    );
    for (var i = 0; i < controls.length; i++) controls[i].disabled = true;
  }

  function lock(box) {
    box.classList.add('is-solved');
    freeze(box);
  }

  function showFailure(box) {
    box.innerHTML =
      '<div class="callout callout--err"><div class="callout__title">' +
      'Задание не загрузилось</div><p style="margin:0;font-size:var(--text-14)">' +
      'Обновите страницу — похоже, связь с сервером моргнула.</p></div>';
  }

  // В предпросмотре админки подсвечиваем тип, который сейчас на экране.
  function markPreviewKind(kind) {
    var links = document.querySelectorAll('[data-captcha-kind]');
    for (var i = 0; i < links.length; i++) {
      var active = links[i].getAttribute('data-captcha-kind') === kind;
      links[i].classList.toggle('btn--primary', active);
      links[i].classList.toggle('btn--ghost', !active);
    }
  }

  // Ставим на место уже готовое задание: сервер присылает его вместе с разбором
  // ошибки, так что второй запрос не нужен.
  function swap(data) {
    if (!slot || !data || !data.html) return;
    setSubmitReady(false);
    slot.innerHTML = data.html;
    if (hiddenInput) hiddenInput.value = data.id;
    if (slot.hasAttribute('data-captcha-preview')) markPreviewKind(data.kind);
    bind(slot.querySelector('[data-captcha]'));
  }

  // previous — задание, которое студент только что видел: сервер подберёт
  // замену другого типа.
  function reload(previous) {
    if (!slot) return;
    // В предпросмотре сохраняем выбранный ?kind и обновляем всю страницу.
    if (slot.hasAttribute('data-captcha-preview')) {
      window.location.reload();
      return;
    }
    setSubmitReady(false);
    fetch('/api/captcha' + (previous ? '?previous=' + encodeURIComponent(previous) : ''),
      { headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(swap)
      .catch(function () { showFailure(slot); });
  }

  function send(box, answer) {
    var id = box.getAttribute('data-id');
    var body = new URLSearchParams();
    body.append('answer', answer);

    return fetch('/api/captcha/' + encodeURIComponent(id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString()
    })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        showFeedback(box, result);
        if (result.ok) {
          lock(box);
          if (hiddenInput) hiddenInput.value = id;
          setSubmitReady(true);
        } else if (result.expired) {
          // Одна попытка: задание сгорело, дальше — другой тип.
          freeze(box);
          setTimeout(function () {
            if (result.replacement) swap(result.replacement);
            else reload(id);
          }, WRONG_PAUSE);
        }
        return result;
      })
      .catch(function () {
        showFeedback(box, {
          ok: false,
          title: 'Сервер не ответил',
          text: 'Похоже, пропал канал до сервера. Попробуйте ещё раз.'
        });
      });
  }

  // Правда или миф: отвечаем на все карточки, проверка уходит сама
  function bindTruthMyth(box) {
    if (!box.querySelector('[data-tm-card]')) return;
    var cards = box.querySelectorAll('[data-tm-card]');
    var answers = new Array(cards.length).fill(null);

    box.addEventListener('click', function (event) {
      var button = event.target.closest('.tm-btn');
      if (!button || button.disabled) return;
      var card = button.closest('[data-tm-card]');
      var index = parseInt(card.getAttribute('data-index'), 10);

      var siblings = card.querySelectorAll('.tm-btn');
      for (var i = 0; i < siblings.length; i++) siblings[i].classList.remove('is-on');
      button.classList.add('is-on');
      answers[index] = button.getAttribute('data-value');

      if (answers.indexOf(null) === -1) send(box, answers.join(','));
    });
  }

  // Один список вариантов: квиз, права доступа и разбор журнала
  function bindQuiz(box) {
    if (!box.querySelector('[data-quiz-option]')) return;
    box.addEventListener('click', function (event) {
      var option = event.target.closest('[data-quiz-option]');
      if (!option || option.disabled) return;
      var all = box.querySelectorAll('[data-quiz-option]');
      for (var i = 0; i < all.length; i++) all[i].classList.remove('is-on');
      option.classList.add('is-on');
      send(box, option.getAttribute('data-index'));
    });
  }

  function bindScheme(box) {
    if (!box.querySelector('[data-scheme-node]')) return;
    box.addEventListener('click', function (event) {
      var node = event.target.closest('[data-scheme-node]');
      if (!node || node.disabled) return;
      var all = box.querySelectorAll('[data-scheme-node]');
      for (var i = 0; i < all.length; i++) all[i].classList.remove('is-on');
      node.classList.add('is-on');
      send(box, node.getAttribute('data-node'));
    });
  }

  // Сервис ищет порт: ответ уходит, когда выбраны все порты
  function bindPorts(box) {
    var check = box.querySelector('[data-ports-check]');
    if (!check) return;
    var selects = box.querySelectorAll('[data-port-select]');

    function chosen() {
      var values = [];
      for (var i = 0; i < selects.length; i++) {
        if (!selects[i].value) return null;
        values.push(selects[i].value);
      }
      return values;
    }

    function render() { check.disabled = chosen() === null; }

    box.addEventListener('change', function (event) {
      if (event.target.closest('[data-port-select]')) render();
    });
    check.addEventListener('click', function () {
      var values = chosen();
      if (values) send(box, values.join(','));
    });
    render();
  }

  // Что за чем: порядок задаётся тапами, цифра показывает номер шага
  function bindOrder(box) {
    var check = box.querySelector('[data-order-check]');
    if (!check) return;
    var options = box.querySelectorAll('[data-order-option]');
    var picked = [];

    function render() {
      for (var i = 0; i < options.length; i++) {
        var option = options[i];
        var place = picked.indexOf(option.getAttribute('data-index'));
        var number = option.querySelector('[data-order-number]');
        if (number) number.textContent = place === -1 ? '·' : String(place + 1);
        option.classList.toggle('is-on', place !== -1);
        option.setAttribute('aria-pressed', place === -1 ? 'false' : 'true');
      }
      check.disabled = picked.length !== options.length;
    }

    box.addEventListener('click', function (event) {
      var option = event.target.closest('[data-order-option]');
      if (!option || option.disabled) return;
      var index = option.getAttribute('data-index');
      var place = picked.indexOf(index);
      // Повторный тап снимает шаг вместе со всеми, что шли после него.
      if (place === -1) picked.push(index);
      else picked = picked.slice(0, place);
      render();
    });

    var resetButton = box.querySelector('[data-order-reset]');
    if (resetButton) {
      resetButton.addEventListener('click', function () { picked = []; render(); });
    }
    check.addEventListener('click', function () {
      if (picked.length === options.length) send(box, picked.join(','));
    });
    render();
  }

  function maskFor(prefix) {
    var bits = 0xffffffff << (32 - prefix);
    return [(bits >>> 24) & 255, (bits >>> 16) & 255, (bits >>> 8) & 255, bits & 255].join('.');
  }

  function bindSubnet(box) {
    var panel = box.querySelector('[data-subnet]');
    if (!panel) return;

    var range = panel.querySelector('[data-subnet-range]');
    var needed = parseInt(panel.getAttribute('data-hosts'), 10);
    var outPrefix = panel.querySelector('[data-subnet-prefix]');
    var outMask = panel.querySelector('[data-subnet-mask]');
    var outHosts = panel.querySelector('[data-subnet-hosts]');
    var outVerdict = panel.querySelector('[data-subnet-verdict]');

    function render() {
      var prefix = parseInt(range.value, 10);
      var hosts = Math.max(Math.pow(2, 32 - prefix) - 2, 0);
      outPrefix.textContent = prefix;
      outMask.textContent = maskFor(prefix);
      outHosts.textContent = hosts;

      if (hosts < needed) {
        outVerdict.className = 'subnet__verdict small';
        outVerdict.textContent = 'Не хватает: нужно ' + needed;
      } else if (hosts >= needed * 2 + 2) {
        outVerdict.className = 'subnet__verdict big';
        outVerdict.textContent = 'Влезет, но адреса пропадут зря';
      } else {
        outVerdict.className = 'subnet__verdict fit';
        outVerdict.textContent = 'В самый раз: нужно ' + needed;
      }
    }

    range.addEventListener('input', render);
    render();

    var check = panel.querySelector('[data-subnet-check]');
    if (check) {
      check.addEventListener('click', function () { send(box, range.value); });
    }
  }

  // Тип задания знает сервер, а браузеру достаточно разметки: каждый
  // обработчик молча уходит, если его элементов в задании нет.
  var BINDERS = [bindTruthMyth, bindQuiz, bindScheme, bindSubnet, bindPorts, bindOrder];

  function bind(box) {
    if (!box) return;
    setSubmitReady(false);

    var reloadButton = box.querySelector('[data-captcha-reload]');
    if (reloadButton) {
      reloadButton.addEventListener('click', function () {
        reload(box.getAttribute('data-id'));
      });
    }

    for (var i = 0; i < BINDERS.length; i++) BINDERS[i](box);
  }

  if (slot) bind(slot.querySelector('[data-captcha]'));

  // ── Живая очередь сама подтягивает свежий список ──────────────────────
  function people(count) {
    var tail = count % 10;
    var hundred = count % 100;
    if (tail === 1 && hundred !== 11) return count + ' человек';
    if (tail >= 2 && tail <= 4 && (hundred < 12 || hundred > 14)) return count + ' человека';
    return count + ' человек';
  }

  var queueBox = document.querySelector('[data-queue-list]');
  var statusBar = document.querySelector('[data-queue-status]');
  if (statusBar) {
    var wasOpen = statusBar.classList.contains('status-bar--open');
    var sessionId = statusBar.getAttribute('data-session-id') || '';
    var sessionRevision = statusBar.getAttribute('data-session-revision') || '';
    var myStatus = statusBar.getAttribute('data-mine-status') || '';
    var every = Math.max(parseInt(statusBar.getAttribute('data-poll'), 10) || 15, 5) * 1000;

    function refreshQueue() {
      // В фоне телефон всё равно ничего не показывает — не тратим батарею.
      if (document.hidden) return;

      fetch('/api/queue', { headers: { 'Accept': 'application/json' } })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          // Замечаем открытие даже без списка, смену приёма между опросами,
          // правку кабинета/времени/заметки и изменение своей записи.
          if (data.open !== wasOpen || String(data.session_id || '') !== sessionId ||
              (data.session_revision || '') !== sessionRevision ||
              (data.mine_status || '') !== myStatus) {
            window.location.reload();
            return;
          }

          if (queueBox) queueBox.innerHTML = data.html;

          var badge = document.querySelector('[data-waiting-badge]');
          if (badge) badge.textContent = 'ждут: ' + data.waiting;

          var ahead = document.querySelector('[data-ahead]');
          if (ahead && data.mine !== null) {
            ahead.textContent = data.ahead === 0 ? 'Вы следующий' : 'Перед вами: ' + people(data.ahead);
          }
        })
        .catch(function () { /* связь моргнула — попробуем на следующем круге */ });
    }

    setInterval(refreshQueue, every);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) refreshQueue();
    });
  }

  // ── Комментарий обязателен для некоторых целей визита ─────────────────
  var commentField = document.getElementById('comment');
  if (commentField) {
    var radios = document.querySelectorAll('input[name="purpose_id"]');
    var update = function () {
      var checked = document.querySelector('input[name="purpose_id"]:checked');
      var required = checked && checked.getAttribute('data-needs-comment') === '1';
      commentField.required = !!required;
      var label = document.querySelector('label[for="comment"] .muted');
      if (label) label.textContent = required ? '(обязательно)' : '(необязательно)';
    };
    for (var r = 0; r < radios.length; r++) radios[r].addEventListener('change', update);
    update();
  }
})();
