(function () {
  const raiz = document.documentElement;

  // tema claro / escuro
  document.querySelectorAll('[data-tema-btn]').forEach((b) => b.addEventListener('click', () => {
    const novo = raiz.dataset.tema === 'escuro' ? 'claro' : 'escuro';
    raiz.dataset.tema = novo;
    localStorage.setItem('tema', novo);
    document.dispatchEvent(new CustomEvent('tema-mudou'));
  }));

  // menu lateral no celular
  const lateral = document.getElementById('lateral');
  document.querySelectorAll('[data-abre-menu]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    lateral && lateral.classList.toggle('aberta');
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

  // confirmações
  document.querySelectorAll('form[data-confirma]').forEach((f) => f.addEventListener('submit', (e) => {
    if (!confirm(f.dataset.confirma)) e.preventDefault();
  }));
  document.querySelectorAll('[data-registrar]').forEach((b) => b.addEventListener('click', (e) => {
    const form = document.getElementById(b.getAttribute('form'));
    const valor = form.querySelector('[name=valor]').value;
    const mod = form.querySelector('[name=modalidade]:checked').value === 'prazo' ? 'reduzindo o prazo' : 'reduzindo a parcela';
    if (!confirm('Registrar amortização de R$ ' + valor + ', ' + mod + '?\n\n'
      + 'O cálculo usa os dados do contrato salvo e recalcula as parcelas futuras.')) e.preventDefault();
  }));

  // nome do arquivo escolhido no upload
  document.querySelectorAll('[data-arquivo]').forEach((i) => i.addEventListener('change', () => {
    const alvo = i.closest('label').querySelector('[data-nome-arquivo]');
    if (alvo && i.files[0]) alvo.textContent = i.files[0].name;
  }));
})();
