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
    if (!input) return;
    var atEnd = input.selectionStart === input.value.length;
    var formatted = formatGroup(input.value);
    if (formatted !== input.value) {
      input.value = formatted;
      if (atEnd) input.setSelectionRange(formatted.length, formatted.length);
    }
  });

  // ── Капча ─────────────────────────────────────────────────────────────
  var slot = document.querySelector('[data-captcha-slot]');
  var submitButton = document.querySelector('[data-booking-submit]');
  var hiddenInput = document.querySelector('[data-captcha-input]');

  function setSubmitReady(ready) {
    if (!submitButton) return;
    submitButton.disabled = !ready;
    submitButton.classList.toggle('is-disabled', !ready);
    submitButton.textContent = ready
      ? 'Записаться на приём'
      : 'Решите задание, чтобы записаться';
  }

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

  function lock(box) {
    box.classList.add('is-solved');
    var controls = box.querySelectorAll('button:not([data-captcha-reload]), input[type=range]');
    for (var i = 0; i < controls.length; i++) controls[i].disabled = true;
  }

  function reload() {
    if (!slot) return;
    setSubmitReady(false);
    fetch('/api/captcha', { headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        slot.innerHTML = data.html;
        if (hiddenInput) hiddenInput.value = data.id;
        bind(slot.querySelector('[data-captcha]'));
      })
      .catch(function () {
        slot.innerHTML =
          '<div class="callout callout--err"><div class="callout__title">' +
          'Задание не загрузилось</div><p style="margin:0;font-size:var(--text-14)">' +
          'Обновите страницу — похоже, связь с сервером моргнула.</p></div>';
      });
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
          setTimeout(function () { reload(); }, 2600);
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
    var cards = box.querySelectorAll('[data-tm-card]');
    if (!cards.length) return;
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

  function bindQuiz(box) {
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
    box.addEventListener('click', function (event) {
      var node = event.target.closest('[data-scheme-node]');
      if (!node || node.disabled) return;
      var all = box.querySelectorAll('[data-scheme-node]');
      for (var i = 0; i < all.length; i++) all[i].classList.remove('is-on');
      node.classList.add('is-on');
      send(box, node.getAttribute('data-node'));
    });
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

  function bind(box) {
    if (!box) return;
    setSubmitReady(false);

    var reloadButton = box.querySelector('[data-captcha-reload]');
    if (reloadButton) reloadButton.addEventListener('click', function () { reload(); });

    var kind = box.getAttribute('data-kind');
    if (kind === 'truth_myth') bindTruthMyth(box);
    else if (kind === 'quiz') bindQuiz(box);
    else if (kind === 'net_scheme') bindScheme(box);
    else if (kind === 'subnet') bindSubnet(box);
  }

  if (slot) bind(slot.querySelector('[data-captcha]'));

  // Просмотр капчи в админке — тот же движок, без формы брони
  var preview = document.querySelector('[data-captcha-preview] [data-captcha]');
  if (preview) bind(preview);

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
