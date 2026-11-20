// Gráfico de área (série única) com mira e dica ao passar o mouse / tocar.
(function () {
  const el = document.getElementById('grafico');
  const dadosEl = document.getElementById('dados-grafico');
  if (!el || !dadosEl) return;
  const { serie, hoje } = JSON.parse(dadosEl.textContent);
  const select = document.querySelector('[data-serie-select]');
  const NOMES = { s: 'Saldo devedor', p: 'Valor da parcela', j: 'Juros por parcela' };
  const MESES = ['jan.', 'fev.', 'mar.', 'abr.', 'mai.', 'jun.', 'jul.', 'ago.', 'set.', 'out.', 'nov.', 'dez.'];
  const NS = 'http://www.w3.org/2000/svg';
  let chave = 's';

  const brl = (v) => 'R$ ' + v.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const compacto = (v) => v >= 1e6 ? (v / 1e6).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' mi'
    : v >= 1e3 ? Math.round(v / 1e3).toLocaleString('pt-BR') + ' mil' : Math.round(v).toLocaleString('pt-BR');
  const data = (s) => { const [a, m, d] = s.split('-'); return `${d} ${MESES[+m - 1]} ${a}`; };
  const tempo = (s) => { const [a, m] = s.split('-'); return +a + (+m - 1) / 12; };

  function passoBonito(max, n) {
    const bruto = max / n, mag = Math.pow(10, Math.floor(Math.log10(bruto)));
    const f = bruto / mag;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  }

  function no(tag, attrs, pai) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (pai) pai.appendChild(n);
    return n;
  }

  function desenhar() {
    el.innerHTML = '';
    const pts = serie.map((p, i) => ({ ...p, i, v: p[chave] })).filter((p) => p.v !== null && p.v !== undefined);
    if (pts.length < 2) return;
    const W = el.clientWidth, H = el.clientHeight;
    const m = { t: 8, r: 8, b: 26, l: 52 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const x0 = tempo(pts[0].d), x1 = tempo(pts[pts.length - 1].d);
    const vmax = Math.max(...pts.map((p) => p.v));
    const passo = passoBonito(vmax, 4), ymax = Math.ceil(vmax / passo) * passo || 1;
    const X = (p) => m.l + (tempo(p.d) - x0) / ((x1 - x0) || 1) * iw;
    const Y = (v) => m.t + ih - v / ymax * ih;

    const svg = no('svg', { viewBox: `0 0 ${W} ${H}`, 'aria-hidden': 'true' }, el);
    for (let v = 0; v <= ymax + 1e-6; v += passo) {
      no('line', { class: 'grade-l', x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, svg);
      no('text', { class: 'eixo-txt', x: 0, y: Y(v) + 4 }, svg).textContent = compacto(v);
    }
    const anos = x1 - x0, passoAno = anos > 25 ? 5 : anos > 12 ? 3 : anos > 5 ? 2 : 1;
    for (let a = Math.ceil(x0); a <= x1; a++) {
      if ((a - Math.ceil(x0)) % passoAno) continue;
      const x = m.l + (a - x0) / ((x1 - x0) || 1) * iw;
      no('text', { class: 'eixo-txt', x, y: H - 6, 'text-anchor': 'middle' }, svg).textContent = a;
    }
    const caminho = pts.map((p, k) => (k ? 'L' : 'M') + X(p).toFixed(1) + ' ' + Y(p.v).toFixed(1)).join(' ');
    no('path', { class: 'area', d: `${caminho} L${X(pts[pts.length - 1]).toFixed(1)} ${Y(0)} L${X(pts[0]).toFixed(1)} ${Y(0)} Z` }, svg);
    no('path', { class: 'linha', d: caminho }, svg);

    // marcador "hoje" (última parcela paga)
    const pHoje = serie[hoje];
    if (hoje > 0 && pHoje && hoje < serie.length - 1) {
      const xh = X(pHoje);
      no('line', { class: 'hoje', x1: xh, x2: xh, y1: m.t, y2: Y(0) }, svg);
      no('text', { class: 'hoje-txt', x: xh + 6, y: m.t + 10 }, svg).textContent = 'Você está aqui';
    }

    // camada de interação
    const mira = no('line', { class: 'mira', y1: m.t, y2: Y(0), visibility: 'hidden' }, svg);
    const ponto = no('circle', { class: 'ponto', r: 5, visibility: 'hidden' }, svg);
    const dica = document.createElement('div');
    dica.className = 'dica-graf'; dica.hidden = true; el.appendChild(dica);
    const alvo = no('rect', { x: m.l, y: 0, width: iw, height: H, fill: 'transparent' }, svg);

    function mover(ev) {
      const r = svg.getBoundingClientRect();
      const cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      let melhor = pts[0], dist = Infinity;
      for (const p of pts) { const d = Math.abs(X(p) - cx); if (d < dist) { dist = d; melhor = p; } }
      const x = X(melhor), y = Y(melhor.v);
      mira.setAttribute('x1', x); mira.setAttribute('x2', x); mira.setAttribute('visibility', 'visible');
      ponto.setAttribute('cx', x); ponto.setAttribute('cy', y); ponto.setAttribute('visibility', 'visible');
      const rot = melhor.i === 0 ? 'Valor financiado' : `Parcela ${String(melhor.i).padStart(3, '0')} · ${data(melhor.d)}`;
      dica.innerHTML = `<span>${rot}</span><strong>${brl(melhor.v)}</strong>`;
      dica.hidden = false;
      dica.style.left = Math.min(Math.max(x, 90), W - 90) + 'px';
      dica.style.top = y + 'px';
    }
    function sair() { mira.setAttribute('visibility', 'hidden'); ponto.setAttribute('visibility', 'hidden'); dica.hidden = true; }
    alvo.addEventListener('mousemove', mover);
    alvo.addEventListener('touchmove', mover, { passive: true });
    alvo.addEventListener('touchstart', mover, { passive: true });
    alvo.addEventListener('mouseleave', sair);
    alvo.addEventListener('touchend', sair);
  }

  if (select) select.addEventListener('change', () => {
    chave = select.value;
    document.querySelectorAll('[data-legenda-serie]').forEach((n) => (n.textContent = NOMES[chave]));
    document.querySelectorAll('[data-titulo-serie]').forEach((n) => (n.textContent = NOMES[chave].toLowerCase()));
    el.setAttribute('aria-label', 'Gráfico da evolução de ' + NOMES[chave].toLowerCase() + ' ao longo do contrato');
    desenhar();
  });
  let rt;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(desenhar, 100); });
  desenhar();
})();
