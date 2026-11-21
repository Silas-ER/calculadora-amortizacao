(function () {
  const raiz = document.documentElement;

  // tema claro / escuro
  document.querySelectorAll('[data-tema-btn]').forEach((b) => b.addEventListener('click', () => {
    const novo = raiz.dataset.tema === 'escuro' ? 'claro' : 'escuro';
    raiz.dataset.tema = novo;
    localStorage.setItem('tema', novo);
    document.dispatchEvent(new CustomEvent('tema-mudou'));
  }));

  // menu lateral: no computador oculta/mostra (e lembra a escolha); no celular abre por cima
  const lateral = document.getElementById('lateral');
  const celular = window.matchMedia('(max-width: 860px)');
  document.querySelectorAll('[data-abre-menu]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!lateral) return;
    if (celular.matches) {
      lateral.classList.toggle('aberta');
    } else {
      const novo = raiz.dataset.menu === 'oculto' ? 'visivel' : 'oculto';
      raiz.dataset.menu = novo;
      localStorage.setItem('menu', novo);
      setTimeout(() => window.dispatchEvent(new Event('resize')), 220); // redesenha o gráfico na nova largura
    }
  }));
  document.addEventListener('click', (e) => {
    if (lateral && lateral.classList.contains('aberta') && !lateral.contains(e.target)) lateral.classList.remove('aberta');
  });

  // mostrar / ocultar senha
  document.querySelectorAll('[data-mostra-senha]').forEach((b) => b.addEventListener('click', () => {
    const campo = document.getElementById(b.dataset.mostraSenha);
    const mostrar = campo.type === 'password';
    campo.type = mostrar ? 'text' : 'password';
    b.setAttribute('aria-label', mostrar ? 'Ocultar senha' : 'Mostrar senha');
  }));

  // filtros que se aplicam sozinhos
  document.querySelectorAll('form[data-auto-submit]').forEach((f) => {
    let t;
    f.querySelectorAll('select').forEach((s) => s.addEventListener('change', () => f.submit()));
    f.querySelectorAll('input[type=search]').forEach((i) => i.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => f.submit(), 600);
    }));
  });

  // confirmações: janela própria do app (no lugar do confirm() do navegador)
  function confirmar({ titulo, texto, ok = 'Confirmar', perigo = false }) {
    return new Promise((resolve) => {
      const d = document.createElement('dialog');
      d.className = 'dialogo dialogo-confirma';
      d.setAttribute('aria-labelledby', 'confirma-titulo');
      d.innerHTML = '<h2 id="confirma-titulo"></h2><p class="muted"></p><div class="acoes">'
        + '<button type="button" class="btn" data-sim></button>'
        + '<button type="button" class="btn btn-sec" data-nao>Cancelar</button></div>';
      d.querySelector('h2').textContent = titulo;
      d.querySelector('p').textContent = texto || '';
      d.querySelector('p').hidden = !texto;
      const sim = d.querySelector('[data-sim]');
      sim.textContent = ok;
      if (perigo) sim.classList.add('btn-perigo');
      let resposta = false;
      sim.addEventListener('click', () => { resposta = true; d.close(); });
      d.querySelector('[data-nao]').addEventListener('click', () => d.close());
      d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
      d.addEventListener('close', () => { d.remove(); resolve(resposta); });
      document.body.appendChild(d);
      d.showModal();
      (perigo ? d.querySelector('[data-nao]') : sim).focus(); // em ação destrutiva, o foco fica no Cancelar
    });
  }

  document.querySelectorAll('form[data-confirma]').forEach((f) => f.addEventListener('submit', async (e) => {
    if (f.dataset.confirmado) return;
    e.preventDefault();
    const perigo = f.classList.contains('zona-perigo');
    const ok = await confirmar({ titulo: f.dataset.confirma, texto: f.dataset.confirmaTexto,
                                 ok: f.dataset.confirmaOk || (perigo ? 'Excluir' : 'Confirmar'), perigo });
    if (ok) { f.dataset.confirmado = '1'; f.requestSubmit(e.submitter || undefined); }
  }));
  // atalhos que preenchem o rendimento (ex.: "CDB 100% do CDI"): forma|taxa|tipo
  document.querySelectorAll('[data-preenche]').forEach((b) => b.addEventListener('click', () => {
    const form = b.closest('form');
    const [forma, taxa, tipo] = b.dataset.preenche.split('|');
    form.querySelector('[name="cforma"]').value = forma;
    form.querySelector('[name="ct"]').value = taxa;
    form.querySelector(`[name="ctipo"][value="${tipo}"]`).checked = true;
    form.querySelectorAll('[data-preenche]').forEach((x) => x.classList.toggle('ativo', x === b));
  }));

  // seletor que leva para outra página (ex.: escolher o contrato na simulação)
  document.querySelectorAll('select[data-navegar]').forEach((s) => s.addEventListener('change', () => {
    window.location.href = s.value;
  }));

  // menu da conta (avatar): fecha ao clicar fora ou com Esc
  const menuConta = document.querySelector('[data-menu-conta]');
  if (menuConta) {
    document.addEventListener('click', (e) => { if (!menuConta.contains(e.target)) menuConta.open = false; });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && menuConta.open) { menuConta.open = false; menuConta.querySelector('summary').focus(); }
    });
  }

  // máscaras numéricas: os dígitos entram pela direita (0,01 → 0,12 → 1,23 → 12,34 …)
  const MAX_DIGITOS = 15;
  const milhar = (s) => s.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  function formatar(digitos, casas) {
    if (!casas) return digitos.replace(/^0+(?=\d)/, '').slice(0, 4); // inteiros: só dígitos, "0" é válido
    digitos = digitos.replace(/^0+/, '').slice(0, MAX_DIGITOS);
    if (!digitos) return '';
    digitos = digitos.padStart(casas + 1, '0');
    return milhar(digitos.slice(0, -casas)) + ',' + digitos.slice(-casas);
  }
  // valor vindo do servidor ("336.000,00", "9,38", "6") → dígitos com o número certo de casas
  function digitosIniciais(valor, casas) {
    if (!valor) return '';
    const n = parseFloat(valor.replace(/\./g, '').replace(',', '.'));
    return isNaN(n) ? '' : Math.round(n * 10 ** casas).toString();
  }
  document.querySelectorAll('input[data-mascara]').forEach((inp) => {
    const tipo = inp.dataset.mascara;
    const casas = tipo === 'inteiro' ? 0 : +(inp.dataset.casas || 2);
    inp.inputMode = 'numeric';
    inp.autocomplete = 'off';
    if (casas && !inp.placeholder) inp.placeholder = '0,' + '0'.repeat(casas);
    inp.value = formatar(casas ? digitosIniciais(inp.value, casas) : inp.value.replace(/\D/g, ''), casas);
    const fimDoCampo = () => { const n = inp.value.length; inp.setSelectionRange(n, n); };
    inp.addEventListener('input', () => {
      inp.value = formatar(inp.value.replace(/\D/g, ''), casas);
      if (casas) fimDoCampo();
    });
    if (casas) {
      inp.addEventListener('focus', () => setTimeout(fimDoCampo));
      inp.addEventListener('click', fimDoCampo);
    }
  });

  // projeção da TR só faz sentido com indexador TR
  document.querySelectorAll('[data-so-tr]').forEach((campo) => {
    const form = campo.closest('form');
    const atualizar = () => {
      const tr = form.querySelector('[data-indexador]:checked')?.value === 'TR';
      campo.hidden = !tr;
    };
    form.querySelectorAll('[data-indexador]').forEach((r) => r.addEventListener('change', atualizar));
    atualizar();
  });

  // envia a foto de perfil assim que for escolhida
  document.querySelectorAll('[data-foto-input]').forEach((i) => i.addEventListener('change', () => {
    if (i.files[0]) i.closest('form').submit();
  }));

  // nome do arquivo escolhido no upload
  document.querySelectorAll('[data-arquivo]').forEach((i) => i.addEventListener('change', () => {
    const alvo = i.closest('label').querySelector('[data-nome-arquivo]');
    if (alvo && i.files[0]) alvo.textContent = i.files[0].name;
  }));

  // planilha: editar uma parcela com os valores do extrato
  const dlg = document.getElementById('dlg-parcela');
  if (dlg) {
    const form = dlg.querySelector('[data-form-parcela]');
    const campo = (n) => dlg.querySelector(`[data-campo="${n}"]`);
    document.querySelectorAll('[data-editar-parcela]').forEach((b) => b.addEventListener('click', () => {
      const d = b.dataset;
      form.action = d.acao;
      campo('numero').textContent = d.numero;
      campo('venc').textContent = d.venc;
      campo('parcela').value = d.parcela;
      campo('saldo').value = d.saldo;
      dlg.querySelector('[data-so-aberta]').hidden = d.paga === '1';
      dlg.querySelector('[name="paga"]').checked = false;
      dlg.querySelector('[data-so-ajustada]').hidden = d.ajustada !== '1';
      dlg.showModal();
      campo('parcela').focus();
    }));
    dlg.querySelector('[data-fechar]').addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); }); // clique fora fecha
  }

  // erro de formulário: leva a tela até a mensagem (o navegador pode manter a rolagem no fim da página)
  const erroForm = document.querySelector('[data-erro-form]');
  if (erroForm) {
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    requestAnimationFrame(() => erroForm.scrollIntoView({ block: 'center' }));
  }
})();
