/* Применяем сохранённую тему до первой отрисовки, чтобы не мигало белым.
   Файл подключается синхронно в <head>: выносить в app.js нельзя,
   он с defer и отработает уже после отрисовки. */
(function () {
  try {
    var saved = localStorage.getItem('au-queue-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) {
    /* приватный режим или запрещённые cookie — остаёмся на системной теме */
  }
})();
