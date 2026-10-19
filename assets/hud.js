/* J.A.R.V.I.S. — общий HUD-движок для дашборда и виджета.
   Звуки (синтез), анимация чисел, графики, фон из частиц, эмоции, мост к нативному окну. */
(function () {
  'use strict';
  const H = window.HUD = {};

  /* ── токен для POST-команд ядру ── */
  const _fetch = window.fetch.bind(window);
  window.fetch = (u, o = {}) => {
    o.headers = Object.assign({}, o.headers || {}, { 'X-Jarvis-Token': window.JARVIS_TOKEN || '' });
    return _fetch(u, o);
  };
  H.post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}) }).then(r => r.json());
  H.get = url => fetch(url, { cache: 'no-store' }).then(r => r.json());
  H.$ = id => document.getElementById(id);
  H.esc = t => String(t == null ? '' : t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  H.clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  /* ── форматирование ── */
  const GB = H.GB = 1024 ** 3;
  H.fmtGB = (b, d = 1) => (b / GB).toFixed(d).replace('.', ',') + ' ГБ';
  H.fmtBytes = b => b >= GB ? H.fmtGB(b) : b >= 1048576 ? Math.round(b / 1048576) + ' МБ' : Math.round(b / 1024) + ' КБ';
  H.fmtRate = b => b >= 1048576 ? (b / 1048576).toFixed(1).replace('.', ',') + ' МБ/с' : b >= 1024 ? Math.round(b / 1024) + ' КБ/с' : Math.round(b) + ' Б/с';
  H.fmtUp = s => {
    if (s == null) return '—';
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return (d ? d + ' д ' : '') + h + ' ч ' + m + ' мин';
  };
  H.plural = (n, f) => { n = Math.abs(n) % 100; if (n > 10 && n < 20) return f[2]; n %= 10; return n === 1 ? f[0] : n >= 2 && n <= 4 ? f[1] : f[2]; };
  H.levelColor = (v, warn, crit, inverse) => {
    const bad = inverse ? v <= crit : v >= crit, mid = inverse ? v <= warn : v >= warn;
    return bad ? 'var(--red)' : mid ? 'var(--amber)' : 'var(--c)';
  };

  /* ── эмоции: цвет и подпись ── */
  H.EMO = {
    calm: ['#3ff0ff', 'СПОКОЕН'], happy: ['#3dffb0', 'В ДУХЕ'], witty: ['#b98bff', 'ИРОНИЗИРУЕТ'],
    concerned: ['#ffb347', 'ОБЕСПОКОЕН'], alarmed: ['#ff3d5a', 'ТРЕВОГА'], proud: ['#ffd166', 'ГОРДИТСЯ'],
    excited: ['#ff6bd6', 'ВООДУШЕВЛЁН'], tired: ['#7b93ff', 'НОЧНОЙ РЕЖИМ'], curious: ['#4fd8ff', 'ЛЮБОПЫТСТВУЕТ'],
    warm: ['#ff9f7a', 'ЗАБОТИТСЯ'],
  };
  H.emotion = 'calm';
  H.setEmotion = e => {
    if (!H.EMO[e]) e = 'calm';
    H.emotion = e;
    document.documentElement.style.setProperty('--emo', H.EMO[e][0]);
    document.documentElement.dataset.emo = e;
    document.querySelectorAll('[data-emo-label]').forEach(el => el.textContent = H.EMO[e][1]);
  };

  /* ── звуки HUD (синтез, без файлов) ── */
  H.sound = (() => {
    let ctx = null, on = true, master = null;
    try { on = localStorage.getItem('hudSound') !== '0'; } catch (e) {}
    const ac = () => {
      if (!ctx) { ctx = new (window.AudioContext || window.webkitAudioContext)(); master = ctx.createGain(); master.gain.value = .9; master.connect(ctx.destination); }
      if (ctx.state === 'suspended') ctx.resume();
      return ctx;
    };
    function tone(f1, f2, dur, vol, type, delay) {
      if (!on) return;
      try {
        const c = ac(), t = c.currentTime + (delay || 0), o = c.createOscillator(), g = c.createGain();
        o.type = type || 'sine';
        o.frequency.setValueAtTime(f1, t); o.frequency.exponentialRampToValueAtTime(Math.max(20, f2), t + dur);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vol, t + 0.012);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g).connect(master); o.start(t); o.stop(t + dur + 0.03);
      } catch (e) {}
    }
    function noise(dur, vol, f, delay) {
      if (!on) return;
      try {
        const c = ac(), t = c.currentTime + (delay || 0), n = Math.floor(c.sampleRate * dur);
        const buf = c.createBuffer(1, n, c.sampleRate), d = buf.getChannelData(0);
        for (let i = 0; i < n; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / n);
        const s = c.createBufferSource(), bp = c.createBiquadFilter(), g = c.createGain();
        bp.type = 'bandpass'; bp.frequency.value = f || 2000; bp.Q.value = .8;
        g.gain.value = vol; s.buffer = buf; s.connect(bp).connect(g).connect(master); s.start(t);
      } catch (e) {}
    }
    const api = {
      get on() { return on; },
      boot() { tone(110, 880, .9, .05, 'sawtooth'); tone(220, 1760, .7, .03, 'sine', .15); noise(.6, .04, 900, 0);
        tone(880, 1320, .16, .045, 'sine', .95); tone(1320, 1980, .22, .04, 'sine', 1.1); },
      blip() { tone(1250, 1850, .07, .03); },
      tick() { tone(2600, 2200, .025, .012, 'square'); },
      hover() { tone(3200, 3000, .018, .006, 'sine'); },
      click() { tone(1800, 900, .06, .03, 'triangle'); },
      open() { tone(400, 1600, .25, .035, 'sine'); noise(.25, .02, 3000); },
      close() { tone(1600, 300, .25, .03, 'sine'); },
      wake() { tone(700, 1400, .1, .045); tone(1400, 2100, .1, .035, 'sine', .1); },
      speak() { tone(900, 1300, .05, .02); },
      alert() { tone(660, 440, .18, .05, 'triangle'); tone(660, 440, .18, .05, 'triangle', .24); },
      siren() { for (let i = 0; i < 4; i++) { tone(520, 1040, .45, .045, 'sawtooth', i * .9); tone(1040, 520, .45, .045, 'sawtooth', i * .9 + .45); } },
      scan() { for (let i = 0; i < 9; i++) tone(1400 + i * 90, 1500 + i * 90, .05, .02, 'square', i * 1.0); tone(300, 2400, 1.2, .025, 'sine'); },
      done() { tone(880, 880, .1, .04); tone(1320, 1320, .12, .04, 'sine', .12); tone(1760, 1760, .2, .04, 'sine', .26); },
      shutter() { noise(.08, .12, 4000); noise(.06, .08, 2500, .09); },
      achieve() { [0, 4, 7, 12].forEach((s, i) => tone(523 * Math.pow(2, s / 12), 523 * Math.pow(2, s / 12), .2, .045, 'triangle', i * .11)); },
      party() { [0, 3, 7, 10, 12, 15].forEach((s, i) => tone(330 * Math.pow(2, s / 12), 330 * Math.pow(2, s / 12) * 1.01, .16, .04, 'square', i * .13)); },
      overload() { tone(80, 1600, 1.4, .05, 'sawtooth'); noise(1.2, .05, 600); },
      toggle() { on = !on; try { localStorage.setItem('hudSound', on ? '1' : '0'); } catch (e) {} if (on) api.blip(); return on; },
      unlock() { try { ac(); } catch (e) {} },
    };
    document.addEventListener('pointerdown', () => api.unlock(), { once: true });
    return api;
  })();

  /* ── плавные числа ── */
  const tw = new Map();
  H.tween = (el, to, opts = {}) => {
    if (!el) return;
    const dec = opts.dec || 0, suf = opts.suf || '';
    const from = tw.has(el) ? tw.get(el) : (parseFloat(String(el.textContent).replace(',', '.')) || 0);
    tw.set(el, to);
    if (!isFinite(to)) { el.textContent = '—'; return; }
    const t0 = performance.now(), d = opts.ms || 700;
    const step = now => {
      const k = Math.min(1, (now - t0) / d), e = 1 - Math.pow(1 - k, 3), v = from + (to - from) * e;
      el.textContent = v.toFixed(dec).replace('.', ',') + suf;
      if (k < 1 && tw.get(el) === to) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  /* ── печатная машинка ── */
  const tyT = new Map();
  H.type = (el, text, speed) => {
    if (!el) return;
    clearInterval(tyT.get(el));
    let i = 0; el.textContent = '';
    const s = speed || Math.max(12, Math.min(38, 2600 / Math.max(1, text.length)));
    tyT.set(el, setInterval(() => {
      i += 1 + (text.length > 160 ? 1 : 0);
      el.textContent = text.slice(0, i);
      if (i >= text.length) clearInterval(tyT.get(el));
    }, s));
  };

  /* ── эффект «расшифровки» текста ── */
  H.scramble = (el, text, ms = 900) => {
    if (!el) return;
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%&@$<>/\\';
    const t0 = performance.now();
    const step = now => {
      const k = Math.min(1, (now - t0) / ms), n = Math.floor(text.length * k);
      let s = text.slice(0, n);
      for (let i = n; i < text.length; i++) s += text[i] === ' ' ? ' ' : chars[Math.floor(Math.random() * chars.length)];
      el.textContent = s;
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  /* ── графики (canvas) ── */
  H.area = (canvas, series, opts = {}) => {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    if (opts.grid !== false) {
      ctx.strokeStyle = 'rgba(63,240,255,.07)'; ctx.lineWidth = 1;
      for (let i = 1; i < 4; i++) { const y = Math.round(h * i / 4) + .5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
      for (let i = 1; i < 12; i++) { const x = Math.round(w * i / 12) + .5; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
    }
    const N = opts.n || 60;
    const all = series.flatMap(s => s.data.slice(-N));
    const max = opts.max || Math.max(1, ...all) * 1.15;
    series.forEach(s => {
      const d = s.data.slice(-N);
      if (d.length < 2) return;
      const step = w / (N - 1), x0 = w - (d.length - 1) * step;
      const pts = d.map((v, i) => [x0 + i * step, h - (Math.min(v, max) / max) * (h - 6) - 3]);
      ctx.beginPath();
      pts.forEach(([x, y], i) => {
        if (!i) return ctx.moveTo(x, y);
        const [px, py] = pts[i - 1], cx = (px + x) / 2;
        ctx.bezierCurveTo(cx, py, cx, y, x, y);
      });
      ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 1.8; ctx.shadowColor = s.color; ctx.shadowBlur = 8; ctx.stroke(); ctx.shadowBlur = 0;
      if (s.fill) {
        ctx.lineTo(w, h); ctx.lineTo(x0, h); ctx.closePath();
        const gr = ctx.createLinearGradient(0, 0, 0, h); gr.addColorStop(0, s.fill); gr.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = gr; ctx.fill();
      }
      const [lx, ly] = pts[pts.length - 1];
      ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(lx - 1, ly, 2.4, 0, Math.PI * 2); ctx.fill();
    });
    if (opts.label) { ctx.fillStyle = 'rgba(120,170,190,.85)'; ctx.font = '10px "JetBrains Mono", monospace'; ctx.fillText(opts.label(max), 6, 12); }
  };

  /* ── кольцевой индикатор (SVG) ── */
  H.ring = (el, opts = {}) => {
    const r = opts.r || 46, sw = opts.sw || 6, size = (r + sw + 8) * 2, c = size / 2, C = 2 * Math.PI * r;
    let ticks = '';
    for (let i = 0; i < 60; i++) {
      const a = i * 6 * Math.PI / 180, long = i % 5 === 0, r1 = r + sw / 2 + 3, r2 = r1 + (long ? 5 : 2.5);
      ticks += `<line x1="${c + r1 * Math.cos(a)}" y1="${c + r1 * Math.sin(a)}" x2="${c + r2 * Math.cos(a)}" y2="${c + r2 * Math.sin(a)}" stroke="currentColor" stroke-opacity="${long ? .55 : .2}" stroke-width="1"/>`;
    }
    el.innerHTML = `<svg viewBox="0 0 ${size} ${size}" class="ring-svg">
      <g class="ring-ticks" style="color:var(--c)">${ticks}</g>
      <circle cx="${c}" cy="${c}" r="${r}" fill="none" stroke="rgba(63,240,255,.09)" stroke-width="${sw}"/>
      <circle class="ring-v" cx="${c}" cy="${c}" r="${r}" fill="none" stroke-width="${sw}" stroke-linecap="round"
        transform="rotate(-90 ${c} ${c})" style="stroke-dasharray:${C};stroke-dashoffset:${C};stroke:var(--c)"/>
      <circle cx="${c}" cy="${c}" r="${r - sw - 4}" fill="none" stroke="rgba(63,240,255,.18)" stroke-dasharray="1.5 4"/>
      <g class="ring-spin" style="transform-origin:${c}px ${c}px"><circle cx="${c}" cy="${c}" r="${r - sw - 9}" fill="none" stroke="var(--c)" stroke-opacity=".35" stroke-width="1.2" stroke-dasharray="14 10 3 10"/></g>
      <text class="ring-num" x="${c}" y="${c + (opts.small ? 5 : 7)}" text-anchor="middle">--</text>
      <text class="ring-unit" x="${c}" y="${c + (opts.small ? 18 : 24)}" text-anchor="middle">${opts.unit || '%'}</text>
    </svg>`;
    el._C = C;
    return el;
  };
  H.setRing = (el, pct, text, color) => {
    if (!el || !el._C) return;
    const v = el.querySelector('.ring-v');
    v.style.strokeDashoffset = el._C * (1 - H.clamp(pct, 0, 100) / 100);
    v.style.stroke = color || 'var(--c)';
    v.style.filter = `drop-shadow(0 0 5px ${color || 'var(--c)'})`;
    const num = el.querySelector('.ring-num');
    if (typeof text === 'number') H.tween(num, text); else num.textContent = text;
  };

  /* ── фон: сеть частиц ── */
  H.particles = (canvas, opts = {}) => {
    const ctx = canvas.getContext('2d');
    let w, h, pts = [], mouse = { x: -999, y: -999 };
    const N = opts.n || 70;
    function resize() {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = w * dpr; canvas.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!pts.length) pts = Array.from({ length: N }, () => ({ x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - .5) * .25, vy: (Math.random() - .5) * .25, r: Math.random() * 1.6 + .4 }));
    }
    window.addEventListener('resize', resize); resize();
    canvas.parentElement.addEventListener('mousemove', e => { const b = canvas.getBoundingClientRect(); mouse = { x: (e.clientX - b.left) * w / b.width, y: (e.clientY - b.top) * h / b.height }; });
    let boost = 0;
    H.particlesBoost = v => { boost = Math.max(boost, v); };
    (function frame() {
      ctx.clearRect(0, 0, w, h);
      const col = getComputedStyle(document.documentElement).getPropertyValue('--emo').trim() || '#3ff0ff';
      boost *= .96;
      for (const p of pts) {
        p.x += p.vx * (1 + boost * 6); p.y += p.vy * (1 + boost * 6);
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
        const dm = Math.hypot(p.x - mouse.x, p.y - mouse.y);
        if (dm < 120) { p.x += (p.x - mouse.x) / dm * .6; p.y += (p.y - mouse.y) / dm * .6; }
      }
      ctx.lineWidth = .6;
      for (let i = 0; i < pts.length; i++) {
        const a = pts[i];
        for (let j = i + 1; j < pts.length; j++) {
          const b = pts[j], d = Math.hypot(a.x - b.x, a.y - b.y);
          if (d < 130) { ctx.strokeStyle = col; ctx.globalAlpha = (1 - d / 130) * .16; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
        }
        ctx.globalAlpha = .55; ctx.fillStyle = col; ctx.beginPath(); ctx.arc(a.x, a.y, a.r, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
      requestAnimationFrame(frame);
    })();
  };

  /* ── голографический глобус (сеть) ── */
  H.globe = canvas => {
    const ctx = canvas.getContext('2d');
    let rot = 0, arcs = [], speed = 1;
    H.globeActivity = v => { speed = 1 + Math.min(6, v); if (Math.random() < Math.min(.9, v * .4) && arcs.length < 14) addArc(); };
    function addArc() {
      arcs.push({ a1: Math.random() * Math.PI * 2, b1: (Math.random() - .5) * 2.2, a2: Math.random() * Math.PI * 2, b2: (Math.random() - .5) * 2.2, t: 0 });
    }
    function proj(lon, lat, R, cx, cy) {
      const x = Math.cos(lat) * Math.sin(lon + rot), z = Math.cos(lat) * Math.cos(lon + rot), y = Math.sin(lat);
      return { x: cx + x * R, y: cy - y * R * .96, z };
    }
    (function frame() {
      const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
      if (w && (canvas.width !== w * dpr)) { canvas.width = w * dpr; canvas.height = h * dpr; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
      const R = Math.min(w, h) / 2 - 6, cx = w / 2, cy = h / 2;
      const col = getComputedStyle(document.documentElement).getPropertyValue('--c').trim() || '#3ff0ff';
      rot += .004 * speed;
      const g = ctx.createRadialGradient(cx, cy, R * .2, cx, cy, R * 1.1);
      g.addColorStop(0, 'rgba(63,240,255,.10)'); g.addColorStop(1, 'rgba(63,240,255,0)');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R * 1.1, 0, 7); ctx.fill();
      ctx.strokeStyle = col; ctx.lineWidth = .7;
      for (let lat = -75; lat <= 75; lat += 15) {
        ctx.beginPath(); let first = true;
        for (let lon = 0; lon <= 360; lon += 6) {
          const p = proj(lon * Math.PI / 180, lat * Math.PI / 180, R, cx, cy);
          ctx.globalAlpha = p.z > 0 ? .45 : .08;
          if (first) { ctx.moveTo(p.x, p.y); first = false; } else ctx.lineTo(p.x, p.y);
        }
        ctx.globalAlpha = .22; ctx.stroke();
      }
      for (let lon = 0; lon < 360; lon += 20) {
        ctx.beginPath();
        for (let lat = -90; lat <= 90; lat += 6) {
          const p = proj(lon * Math.PI / 180, lat * Math.PI / 180, R, cx, cy);
          lat === -90 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y);
        }
        ctx.globalAlpha = .18; ctx.stroke();
      }
      // «города»
      for (let i = 0; i < 26; i++) {
        const p = proj(i * 2.4, Math.sin(i * 7.1) * 1.1, R, cx, cy);
        if (p.z > 0) { ctx.globalAlpha = .8; ctx.fillStyle = col; ctx.fillRect(p.x - 1, p.y - 1, 2, 2); }
      }
      // дуги трафика
      ctx.lineWidth = 1.3;
      arcs = arcs.filter(a => a.t < 1.6);
      for (const a of arcs) {
        a.t += .012 * speed;
        ctx.beginPath();
        const steps = 24, end = Math.min(1, a.t);
        for (let k = 0; k <= steps * end; k++) {
          const s = k / steps, lon = a.a1 + (a.a2 - a.a1) * s, lat = a.b1 + (a.b2 - a.b1) * s;
          const p = proj(lon, lat, R * (1 + Math.sin(s * Math.PI) * .25), cx, cy);
          k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y);
        }
        ctx.globalAlpha = Math.max(0, 1 - Math.max(0, a.t - 1) * 1.6); ctx.strokeStyle = '#ff6bd6'; ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.strokeStyle = col; ctx.globalAlpha = .6; ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.stroke(); ctx.globalAlpha = 1;
      requestAnimationFrame(frame);
    })();
  };

  /* ── мост к нативному окну ── */
  H.native = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.jarvis;
  H.send = msg => { if (H.native) H.native.postMessage(msg); };

  /* в браузере (без приложения) — говорим сами через /api/tts_audio */
  let audioEl = null, analyser = null, actx2 = null;
  H.browserSay = (text, emotion) => {
    if (H.native || !text) return;
    try {
      if (audioEl) audioEl.pause();
      audioEl = new Audio('/api/tts_audio?kind=jarvis&emotion=' + encodeURIComponent(emotion || '') + '&text=' + encodeURIComponent(text));
      audioEl.crossOrigin = 'anonymous';
      actx2 = actx2 || new (window.AudioContext || window.webkitAudioContext)();
      const src = actx2.createMediaElementSource(audioEl);
      analyser = actx2.createAnalyser(); analyser.fftSize = 256;
      src.connect(analyser).connect(actx2.destination);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      const loop = () => {
        if (!audioEl || audioEl.paused) { window.jarvisLevel && window.jarvisLevel(0); return; }
        analyser.getByteTimeDomainData(buf);
        let s = 0; for (const v of buf) s += ((v - 128) / 128) ** 2;
        window.jarvisLevel && window.jarvisLevel(Math.min(1, Math.sqrt(s / buf.length) * 4));
        requestAnimationFrame(loop);
      };
      audioEl.addEventListener('playing', loop);
      audioEl.play().catch(() => {});
    } catch (e) {}
  };

  /* команда Джарвису текстом */
  H.command = async text => {
    text = (text || '').trim();
    if (!text) return null;
    if (H.native) { H.send('cmd:' + text); return null; }
    const r = await H.post('/api/voice', { cmd: 'text', text });
    if (r && r.say) { window.jarvisSays && window.jarvisSays(r.say, r.emotion); H.browserSay(r.say, r.emotion); }
    if (r && r.ui) window.jarvisUI && window.jarvisUI(r.ui);
    return r;
  };
  H.action = async name => {
    if (['shutdown', 'restart'].includes(name) && H.native) { H.send('action:' + name); return; }
    if (H.native) { H.send('action:' + name); return; }
    const r = await H.post('/api/action', { name });
    if (r && r.say) { window.jarvisSays && window.jarvisSays(r.say, r.emotion); H.browserSay(r.say, r.emotion); }
    if (r && r.ui) window.jarvisUI && window.jarvisUI(r.ui);
  };
  H.say = (text, emotion) => {
    if (H.native) H.send('say:' + JSON.stringify({ text, emotion: emotion || '' }));
    else { window.jarvisSays && window.jarvisSays(text, emotion); H.browserSay(text, emotion); }
  };
})();
