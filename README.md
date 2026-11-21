# Amortiza: calculadora de financiamento

Web app em **Python + Flask + SQLite**.

## Como rodar (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

Abra http://127.0.0.1:5000, crie uma conta e comece.

## Funcionalidades

- Criação de conta e login (senha com hash, proteção CSRF, dados isolados por usuário)
- Calculadora SAC ou PRICE, com taxa ao mês ou ao ano, seguros/taxas mensais e comparativo SAC x PRICE
- Armazenamento no SQLite: taxa, parcelas, juros, amortização, saldo devedor e parcelas pagas
- "Devo amortizar?": recomendação baseada em reserva de emergência, custo efetivo da dívida x rendimento
  líquido do investimento e comprometimento da renda, com tabela de "quanto amortizar"
- Simulação de amortização reduzindo prazo ou parcela, e registro da amortização (recalcula a tabela)
- Importação de planilha .xlsx/.csv (detecta taxa e sistema automaticamente) e exportação CSV

## TR (atualização monetária)

Contratos marcados com indexador **TR** têm o saldo corrigido mês a mês pela TR do aniversário do contrato.
A TR vem da API pública do Banco Central (SGS, série 226), consultada no máximo uma vez por dia e guardada
no SQLite (`indice_tr`). Meses ainda não publicados usam a média dos últimos 12 meses ou TR zero.
Sem internet, o app usa os valores salvos e mostra um aviso.

## Estrutura

| Arquivo | Conteúdo |
|---|---|
| `app.py` | rotas, login, sessão |
| `finance.py` | cálculos SAC/PRICE, simulação, recomendação |
| `importer.py` | leitura de planilhas |
| `indices.py` | TR do Banco Central (download, cache e projeção) |
| `db.py` | esquema, conexão SQLite e migração de bancos antigos |
| `templates/` | `app.html` (menu lateral + topo), `auth.html` (login/cadastro) e uma página por tela |
| `static/` | `style.css` (tokens de tema claro/escuro do Figma), `app.js`, `grafico.js` |
| `design/` | telas exportadas do Figma (referência) |
| `instance/app.db` | banco de dados (criado automaticamente) |

## E-mail (recuperação de senha)

Sem configuração, o link de "Esqueci minha senha" aparece no terminal onde o servidor está rodando.
Para enviar e-mails de verdade, defina as variáveis de ambiente:

```powershell
$env:SMTP_HOST = "smtp.seuprovedor.com"; $env:SMTP_PORT = "587"
$env:SMTP_USER = "usuario"; $env:SMTP_PASSWORD = "senha"; $env:SMTP_FROM = "Amortiza <nao-responda@seudominio.com>"
```

## Produção

Defina a variável `SECRET_KEY`, rode com um servidor WSGI (ex.: `waitress-serve app:app`) atrás de HTTPS
e não use `debug=True`.
