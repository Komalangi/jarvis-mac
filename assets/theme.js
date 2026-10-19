// Темы оформления: поворот оттенка всей страницы (базовый цвет — голубой реактор).
// Тема хранится в настройках ядра (settings.theme), меняется голосом: «тема красная».
(function () {
  const THEMES = { arc: 0, ice: 35, violet: 95, red: 170, gold: 215, green: -35 };
  let cur = null;
  function apply(name) {
    if (name === cur) return;
    cur = name;
    const deg = THEMES[name] || 0;
    document.documentElement.style.filter = deg ? 'hue-rotate(' + deg + 'deg) saturate(1.08)' : '';
    document.documentElement.dataset.theme = name || 'arc';
  }
  async function poll() {
    try {
      const r = await fetch('/api/settings', { cache: 'no-store' });
      const s = await r.json();
      apply(s.theme || 'arc');
    } catch (e) { /* ядро ещё не готово */ }
  }
  window.JARVIS_THEMES = THEMES;
  window.applyTheme = apply;
  poll();
  setInterval(poll, 5000);
})();
