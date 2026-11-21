import csv
import hashlib
import io
import math
import os
import re
import secrets
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import finance as fz
import indices
from db import close_db, get_db, init_db
from importer import ImportacaoErro, importar_planilha

app = Flask(__name__, instance_relative_config=True)
os.makedirs(app.instance_path, exist_ok=True)
app.config["DATABASE"] = os.path.join(app.instance_path, "app.db")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB por upload
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = timedelta(days=30)


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    caminho = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(caminho):
        with open(caminho, "w") as f:
            f.write(secrets.token_hex(32))
    with open(caminho) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.teardown_appcontext(close_db)
init_db(app)


def num_br(v, casas=2):
    if v is None or v == "":
        return ""
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


app.jinja_env.filters.update({
    "brl": fz.brl, "pct": fz.pct, "num_br": num_br, "data_longa": fz.data_longa,
    "meses_extenso": fz.meses_extenso,
    "databr": lambda s: "-" if not s else "/".join(reversed(str(s)[:10].split("-"))),
})
SISTEMA_DESC = {"SAC": "Amortização constante", "PRICE": "Parcelas fixas"}
app.jinja_env.globals["SISTEMA_DESC"] = SISTEMA_DESC


# ---------------------------------------------------------------- segurança e contexto

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def antes():
    db = get_db()
    uid = session.get("usuario_id")
    g.user = db.execute("SELECT id, email, nome, foto_tipo, foto_versao FROM usuarios WHERE id = ?",
                        (uid,)).fetchone() if uid else None
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or not secrets.compare_digest(token, request.form.get("csrf_token", "")):
            abort(400, "Token de segurança inválido. Recarregue a página e tente novamente.")
    g.contratos, g.fin_nav = [], None
    if g.user:
        g.contratos = db.execute(
            "SELECT id, nome, instituicao, numero_contrato FROM financiamentos WHERE usuario_id = ? ORDER BY id DESC",
            (g.user["id"],)).fetchall()
        atual = session.get("fin_atual")
        g.fin_nav = next((c for c in g.contratos if c["id"] == atual), g.contratos[0] if g.contratos else None)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def _destino_seguro(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("index")


# ---------------------------------------------------------------- autenticação

@app.route("/")
def index():
    if not g.user:
        return redirect(url_for("login"))
    if g.fin_nav:
        return redirect(url_for("visao_geral", fid=g.fin_nav["id"]))
    return redirect(url_for("contratos"))


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@app.route("/registrar", methods=["GET", "POST"])
def registrar():
    if g.user:
        return redirect(url_for("index"))
    form = {"nome": "", "email": ""}
    if request.method == "POST":
        form = {"nome": request.form.get("nome", "").strip()[:80], "email": request.form.get("email", "").strip().lower()}
        senha, confirma = request.form.get("senha", ""), request.form.get("confirma", "")
        erro = None
        if not form["nome"]:
            erro = "Informe seu nome."
        elif not EMAIL_RE.fullmatch(form["email"]):
            erro = "Informe um e-mail válido."
        elif len(senha) < 8:
            erro = "A senha deve ter pelo menos 8 caracteres."
        elif senha != confirma:
            erro = "As senhas não conferem."
        else:
            db = get_db()
            try:
                cur = db.execute("INSERT INTO usuarios (email, nome, password_hash) VALUES (?, ?, ?)",
                                 (form["email"], form["nome"], generate_password_hash(senha)))
                db.commit()
            except Exception:
                erro = "Já existe uma conta com este e-mail."
            else:
                session.clear()
                session["usuario_id"] = cur.lastrowid
                flash(f"Conta criada. Bem-vindo(a), {form['nome'].split()[0]}!", "ok")
                return redirect(url_for("index"))
        flash(erro, "erro")
    return render_template("registrar.html", form=form, pagina="Criar conta")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        senha = request.form.get("senha", "")
        u = get_db().execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
        if u and check_password_hash(u["password_hash"], senha):
            session.clear()
            session.permanent = bool(request.form.get("manter"))
            session["usuario_id"] = u["id"]
            return redirect(_destino_seguro(request.args.get("next")))
        flash("E-mail ou senha incorretos.", "erro")
    return render_template("login.html", email=email, pagina="Entrar")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- recuperação de senha

def enviar_email(destino, assunto, corpo):
    """Envia por SMTP se configurado (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM);
    caso contrário, mostra a mensagem no terminal do servidor (modo desenvolvimento)."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        app.logger.warning("SMTP não configurado. E-mail para %s\nAssunto: %s\n\n%s", destino, assunto, corpo)
        return
    msg = EmailMessage()
    msg["Subject"], msg["To"] = assunto, destino
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "nao-responda@localhost"))
    msg.set_content(corpo)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=20) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


@app.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    if g.user:
        return redirect(url_for("index"))
    enviado = False
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        db = get_db()
        u = db.execute("SELECT id, email FROM usuarios WHERE email = ?", (email,)).fetchone()
        if u:
            token = secrets.token_urlsafe(32)
            db.execute("DELETE FROM redefinicoes_senha WHERE usuario_id = ?", (u["id"],))
            db.execute("INSERT INTO redefinicoes_senha (token_hash, usuario_id, expira_em) VALUES (?, ?, ?)",
                       (_hash_token(token), u["id"], (datetime.now() + timedelta(hours=1)).isoformat()))
            db.commit()
            link = url_for("redefinir_senha", token=token, _external=True)
            try:
                enviar_email(u["email"], "Redefinição de senha · Amortiza",
                             f"Recebemos um pedido para redefinir sua senha.\n\nAcesse o link abaixo (válido por 1 hora):\n{link}\n\n"
                             "Se não foi você, ignore este e-mail.")
            except (OSError, smtplib.SMTPException):
                app.logger.exception("Falha ao enviar e-mail de redefinição")
        enviado = True  # mesma resposta exista ou não a conta
    return render_template("esqueci_senha.html", enviado=enviado, pagina="Esqueci minha senha")


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token):
    db = get_db()
    r = db.execute("SELECT * FROM redefinicoes_senha WHERE token_hash = ? AND usado = 0", (_hash_token(token),)).fetchone()
    valido = r is not None and datetime.fromisoformat(r["expira_em"]) > datetime.now()
    if valido and request.method == "POST":
        senha, confirma = request.form.get("senha", ""), request.form.get("confirma", "")
        if len(senha) < 8:
            flash("A senha deve ter pelo menos 8 caracteres.", "erro")
        elif senha != confirma:
            flash("As senhas não conferem.", "erro")
        else:
            db.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?", (generate_password_hash(senha), r["usuario_id"]))
            db.execute("UPDATE redefinicoes_senha SET usado = 1 WHERE token_hash = ?", (r["token_hash"],))
            db.commit()
            session.clear()
            flash("Senha redefinida. Entre com a nova senha.", "ok")
            return redirect(url_for("login"))
    return render_template("redefinir_senha.html", valido=valido, pagina="Redefinir senha")


# ---------------------------------------------------------------- páginas públicas, conta e erros

@app.route("/privacidade")
def privacidade():
    return render_template("privacidade.html", pagina="Privacidade")


@app.route("/termos")
def termos():
    return render_template("termos.html", pagina="Termos de uso")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/conta", methods=["GET", "POST"])
@login_required
def conta():
    db = get_db()
    if request.method == "POST":
        acao = request.form.get("acao")
        if acao == "perfil":
            nome = request.form.get("nome", "").strip()[:80]
            email = request.form.get("email", "").strip().lower()
            if not nome:
                flash("Informe seu nome.", "erro")
            elif not EMAIL_RE.fullmatch(email):
                flash("Informe um e-mail válido.", "erro")
            elif db.execute("SELECT 1 FROM usuarios WHERE email = ? AND id <> ?", (email, g.user["id"])).fetchone():
                flash("Já existe outra conta com este e-mail.", "erro")
            else:
                db.execute("UPDATE usuarios SET nome = ?, email = ? WHERE id = ?", (nome, email, g.user["id"]))
                db.commit()
                flash("Dados atualizados.", "ok")
                return redirect(url_for("conta"))
        elif acao == "senha":
            u = db.execute("SELECT password_hash FROM usuarios WHERE id = ?", (g.user["id"],)).fetchone()
            nova, confirma = request.form.get("nova", ""), request.form.get("confirma", "")
            if not check_password_hash(u["password_hash"], request.form.get("atual", "")):
                flash("Senha atual incorreta.", "erro")
            elif len(nova) < 8:
                flash("A nova senha deve ter pelo menos 8 caracteres.", "erro")
            elif nova != confirma:
                flash("As senhas não conferem.", "erro")
            else:
                db.execute("UPDATE usuarios SET password_hash = ? WHERE id = ?", (generate_password_hash(nova), g.user["id"]))
                db.commit()
                flash("Senha alterada.", "ok")
                return redirect(url_for("conta"))
    return render_template("conta.html", pagina="Minha conta")


FOTO_MAX = 2 * 1024 * 1024


def _tipo_imagem(dados):
    """Identifica a imagem pelo conteúdo, não pela extensão."""
    if dados.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if dados.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/conta/foto")
@login_required
def foto():
    u = get_db().execute("SELECT foto, foto_tipo FROM usuarios WHERE id = ?", (g.user["id"],)).fetchone()
    if not u["foto"]:
        abort(404)
    return Response(u["foto"], mimetype=u["foto_tipo"],
                    headers={"Cache-Control": "private, max-age=31536000", "X-Content-Type-Options": "nosniff"})


@app.post("/conta/foto")
@login_required
def enviar_foto():
    arq = request.files.get("foto")
    dados = arq.read(FOTO_MAX + 1) if arq and arq.filename else b""
    tipo = _tipo_imagem(dados)
    if not dados:
        flash("Selecione uma imagem.", "erro")
    elif len(dados) > FOTO_MAX:
        flash("A foto deve ter até 2 MB.", "erro")
    elif not tipo:
        flash("Envie uma imagem JPG, PNG ou WebP.", "erro")
    else:
        db = get_db()
        db.execute("UPDATE usuarios SET foto = ?, foto_tipo = ?, foto_versao = foto_versao + 1 WHERE id = ?",
                   (dados, tipo, g.user["id"]))
        db.commit()
        flash("Foto atualizada.", "ok")
    return redirect(url_for("conta"))


@app.post("/conta/foto/remover")
@login_required
def remover_foto():
    db = get_db()
    db.execute("UPDATE usuarios SET foto = NULL, foto_tipo = NULL, foto_versao = foto_versao + 1 WHERE id = ?",
               (g.user["id"],))
    db.commit()
    flash("Foto removida.", "ok")
    return redirect(url_for("conta"))


@app.post("/conta/excluir")
@login_required
def excluir_conta():
    db = get_db()
    u = db.execute("SELECT password_hash FROM usuarios WHERE id = ?", (g.user["id"],)).fetchone()
    if not check_password_hash(u["password_hash"], request.form.get("senha", "")):
        flash("Senha incorreta. A conta não foi excluída.", "erro")
        return redirect(url_for("conta"))
    db.execute("DELETE FROM usuarios WHERE id = ?", (g.user["id"],))
    db.commit()
    session.clear()
    flash("Sua conta e todos os seus dados foram excluídos.", "ok")
    return redirect(url_for("login"))


ERROS = {
    400: ("Algo não saiu como esperado.", "A requisição não pôde ser processada. Recarregue a página e tente novamente."),
    404: ("Página não encontrada.", "O endereço pode ter mudado, ou este conteúdo não pertence à sua conta."),
    413: ("Arquivo grande demais.", "Envie uma planilha de até 5 MB."),
    500: ("Tivemos um problema do nosso lado.", "Tente novamente em instantes."),
}


def _erro(e):
    codigo = getattr(e, "code", 500) or 500
    titulo, texto = ERROS.get(codigo, ERROS[500])
    if codigo == 400 and getattr(e, "description", "") and "Token" in e.description:
        texto = e.description
    return render_template("erro.html", codigo=codigo, titulo=titulo, texto=texto, pagina=f"Erro {codigo}"), codigo


for _c in ERROS:
    app.register_error_handler(_c, _erro)


# ---------------------------------------------------------------- helpers de dados

def get_fin(fid):
    fin = get_db().execute("SELECT * FROM financiamentos WHERE id = ? AND usuario_id = ?",
                           (fid, g.user["id"])).fetchone()
    if fin is None:
        abort(404)
    session["fin_atual"] = fid
    g.fin_nav = fin
    return fin


def _ler_parcelas(db, fid):
    return [dict(r) for r in db.execute("SELECT * FROM parcelas WHERE financiamento_id = ? ORDER BY numero", (fid,))]


def _saldo_antes(linha):
    """Saldo logo após a parcela anterior (antes da correção do mês e do pagamento desta linha)."""
    return round(linha["saldo_devedor"] + linha["amortizacao"] - (linha.get("correcao") or 0), 2)


def _alvos(fin):
    """Parcela informada pelo extrato, se houver: {numero: valor}."""
    return {fin["ajuste_parcela"]: fin["ajuste_valor"]} if fin["ajuste_parcela"] and fin["ajuste_valor"] else None


def _regerar_restantes(db, fin, k, correcoes=None):
    """Recalcula as parcelas posteriores a k a partir do saldo atual."""
    restantes = [l for l in _ler_parcelas(db, fin["id"]) if l["numero"] > k]
    if not restantes:
        return
    prox = restantes[0]
    novas = fz.gerar_tabela(_saldo_antes(prox), fin["taxa_mensal"], len(restantes), fin["sistema"],
                            date.fromisoformat(prox["data_vencimento"]), prox["encargos"], prox["numero"],
                            correcoes, _alvos(fin))
    db.execute("DELETE FROM parcelas WHERE financiamento_id = ? AND numero > ?", (fin["id"], k))
    salvar_parcelas(db, fin["id"], novas)


def sincronizar_tr(db, fin, linhas):
    """Atualiza a TR e, se houver valores novos, recalcula as parcelas ainda não pagas.

    Retorna (linhas, status) — status traz a data da última TR e quantos meses usam TR publicada.
    """
    k = fin["parcelas_pagas"]
    desde = fz.add_months(date.fromisoformat(fin["data_primeira"]), -1)
    status = indices.atualizar(db, desde)
    restantes = [l for l in linhas if l["numero"] > k]
    tabela = indices.TabelaTR(db, desde)
    venc = [l["data_vencimento"] for l in restantes]
    fatores, conhecidos = tabela.fatores(venc, fin["projecao_tr"])
    status.update({"conhecidos": conhecidos, "projetados": len(venc) - conhecidos,
                   "media": tabela.media_12(date.fromisoformat(fin["data_primeira"]).day),
                   "fatores": fatores})
    assinatura = (f"{status['ultima']}|{fin['projecao_tr']}|{k}|{len(linhas)}"
                  f"|{fin['ajuste_parcela']}:{fin['ajuste_valor']}")
    if restantes and assinatura != fin["tr_assinatura"]:
        _regerar_restantes(db, fin, k, fatores)
        db.execute("UPDATE financiamentos SET tr_assinatura = ? WHERE id = ?", (assinatura, fin["id"]))
        db.commit()
        linhas = _ler_parcelas(db, fin["id"])
    # parcela calibrada pelo extrato: as simulações usam a correção efetivamente aplicada naquele mês
    status["ajustada"] = None
    restantes = [l for l in linhas if l["numero"] > k]
    for idx, l in enumerate(restantes):
        if _alvos(fin) and l["numero"] in _alvos(fin) and idx < len(fatores):
            base = _saldo_antes(l)
            fatores[idx] = l["correcao"] / base if base else 0.0
            status["ajustada"] = l["numero"]
    return linhas, status


def carregar_estado(fin):
    db = get_db()
    linhas = _ler_parcelas(db, fin["id"])
    tr = None
    if fin["indexador"] == "TR":
        linhas, tr = sincronizar_tr(db, fin, linhas)
    amortizacoes = [dict(r) for r in db.execute(
        "SELECT * FROM amortizacoes WHERE financiamento_id = ? ORDER BY id DESC", (fin["id"],))]
    k = fin["parcelas_pagas"]
    restantes = [l for l in linhas if l["numero"] > k]
    pagas = [l for l in linhas if l["numero"] <= k]
    if restantes:
        prox = restantes[0]
        saldo = _saldo_antes(prox)
        ctx = {"saldo": saldo, "i": fin["taxa_mensal"], "n_rest": len(restantes), "sistema": fin["sistema"],
               "encargos": prox["encargos"], "data_prox": date.fromisoformat(prox["data_vencimento"]),
               "num_ini": prox["numero"], "correcoes": tr["fatores"] if tr else None}
    else:
        prox, saldo, ctx = None, 0.0, None
    return {
        "linhas": linhas, "restantes": restantes, "pagas": pagas, "proxima": prox, "saldo": saldo, "ctx": ctx,
        "amortizacoes": amortizacoes, "tr": tr,
        "correcao_restante": sum(l.get("correcao", 0) for l in restantes),
        "juros_restantes": sum(l["juros"] for l in restantes),
        "total_pago": sum(l["valor_parcela"] for l in pagas) + sum(a["valor"] for a in amortizacoes),
        "economia_total": sum(a["economia"] for a in amortizacoes),
        "progresso": 1 - saldo / fin["valor_financiado"] if fin["valor_financiado"] else 0,
    }


def salvar_parcelas(db, fid, parcelas):
    db.executemany(
        "INSERT INTO parcelas (financiamento_id, numero, data_vencimento, valor_parcela, amortizacao, "
        "juros, encargos, correcao, saldo_devedor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(fid, p["numero"], p["data_vencimento"], p["valor_parcela"], p["amortizacao"], p["juros"],
          p["encargos"], p.get("correcao", 0), p["saldo_devedor"]) for p in parcelas])


def num(campo, padrao=None):
    v = fz.parse_num(request.form.get(campo))
    return padrao if v is None else v


# ---------------------------------------------------------------- contratos

@app.route("/financiamentos")
@login_required
def contratos():
    fins = get_db().execute("SELECT * FROM financiamentos WHERE usuario_id = ? ORDER BY id DESC",
                            (g.user["id"],)).fetchall()
    return render_template("contratos.html", itens=[(f, carregar_estado(f)) for f in fins], pagina="Meus contratos")


@app.route("/simulador", methods=["GET", "POST"])
@login_required
def simulador():
    form = {"nome": "", "instituicao": "", "valor": "", "taxa": "", "tipo_taxa": "anual", "prazo": "",
            "data_primeira": fz.add_months(date.today(), 1).isoformat(), "encargos": "0"}
    de = request.args.get("de", type=int)
    if request.method == "GET" and de:
        f = get_fin(de)
        form.update({"valor": num_br(f["valor_financiado"]), "taxa": num_br(f["taxa_anual"] * 100, 2),
                     "prazo": str(f["prazo_original"]), "data_primeira": f["data_primeira"],
                     "encargos": num_br(f["encargos_mensais"])})
    comp = None
    if request.method == "POST" or de:
        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form})
        valor, taxa, prazo = fz.parse_num(form["valor"]), fz.parse_num(form["taxa"]), fz.parse_num(form["prazo"])
        encargos = fz.parse_num(form["encargos"]) or 0.0
        erro = None
        if not valor or valor <= 0:
            erro = "Informe o valor financiado."
        elif taxa is None or taxa < 0:
            erro = "Informe a taxa de juros."
        elif not prazo or prazo < 1 or prazo > 600 or prazo != int(prazo):
            erro = "Informe um prazo entre 1 e 600 meses."
        try:
            data_primeira = date.fromisoformat(form["data_primeira"])
        except ValueError:
            erro = erro or "Data da primeira parcela inválida."
        if erro:
            flash(erro, "erro")
        else:
            prazo = int(prazo)
            i = fz.taxa_mensal(taxa, form["tipo_taxa"])
            tabelas = {s: fz.gerar_tabela(valor, i, prazo, s, data_primeira, encargos) for s in ("SAC", "PRICE")}
            acao = request.form.get("acao", "")
            if acao.startswith("salvar_"):
                sistema = acao.removeprefix("salvar_")
                if sistema in tabelas:
                    db = get_db()
                    cur = db.execute(
                        "INSERT INTO financiamentos (usuario_id, nome, instituicao, valor_financiado, taxa_mensal, "
                        "taxa_anual, prazo_meses, prazo_original, sistema, encargos_mensais, data_primeira, origem) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'calculado')",
                        (g.user["id"], form["nome"] or "Meu imóvel", form["instituicao"], valor, i,
                         fz.mensal_para_anual(i), prazo, prazo, sistema, encargos, data_primeira.isoformat()))
                    salvar_parcelas(db, cur.lastrowid, tabelas[sistema])
                    db.commit()
                    flash("Contrato salvo. Informe em “Editar contrato” quantas parcelas já foram pagas.", "ok")
                    return redirect(url_for("visao_geral", fid=cur.lastrowid))
            comp = {"i": i, "anual": fz.mensal_para_anual(i), "tabelas": tabelas,
                    "totais": {s: fz.totais(t) for s, t in tabelas.items()}}
    return render_template("simulador.html", form=form, comp=comp, pagina="Comparar SAC e Price")


@app.route("/financiamentos/cadastrar", methods=["GET", "POST"])
@login_required
def cadastrar():
    campos = ("nome", "instituicao", "numero_contrato", "valor", "taxa", "tipo_taxa", "prazo", "sistema",
              "data_primeira", "encargos", "parcelas_pagas", "saldo_atual", "prazo_restante", "parcela_extrato",
              "indexador", "projecao_tr")
    form = {k: "" for k in campos}
    form.update({"tipo_taxa": "anual", "sistema": "SAC", "encargos": "0", "parcelas_pagas": "0",
                 "indexador": "TR", "projecao_tr": "media12"})
    if request.method == "POST":
        form.update({k: request.form.get(k, form[k]).strip() for k in campos})
        valor, taxa, prazo = num("valor"), num("taxa"), num("prazo")
        encargos, pagas = num("encargos", 0.0), num("parcelas_pagas", 0)
        saldo_atual, prazo_rest = num("saldo_atual"), num("prazo_restante")
        parcela_extrato = num("parcela_extrato") or None
        erro = None
        if not form["nome"]:
            erro = "Dê um nome ao imóvel."
        elif not valor or valor <= 0:
            erro = "Informe o valor financiado."
        elif taxa is None or taxa < 0 or taxa > 100:
            erro = "Informe a taxa de juros."
        elif not prazo or prazo < 1 or prazo > 600 or prazo != int(prazo):
            erro = "Informe um prazo entre 1 e 600 meses."
        elif form["sistema"] not in ("SAC", "PRICE"):
            erro = "Escolha SAC ou Price."
        elif encargos < 0:
            erro = "Seguros e taxas não podem ser negativos."
        elif pagas < 0 or pagas != int(pagas) or pagas > prazo:
            erro = "Parcelas pagas deve ser um número entre 0 e o prazo."
        elif saldo_atual is not None and saldo_atual <= 0:
            erro = "O saldo devedor atual deve ser maior que zero. Deixe em branco para calcular pelas condições originais."
        elif prazo_rest is not None and (saldo_atual is None or prazo_rest < 1 or prazo_rest > 600 or prazo_rest != int(prazo_rest)):
            erro = "Parcelas restantes só valem junto com o saldo devedor atual (entre 1 e 600)."
        elif form["indexador"] not in ("nenhum", "TR") or form["projecao_tr"] not in ("media12", "zero"):
            erro = "Escolha a atualização monetária do contrato."
        elif parcela_extrato is not None and parcela_extrato <= encargos:
            erro = "O valor da parcela do extrato deve ser maior que os seguros e taxas mensais."
        try:
            data_primeira = date.fromisoformat(form["data_primeira"])
        except ValueError:
            erro = erro or "Informe a data da 1ª parcela."
        if not erro:
            prazo, pagas = int(prazo), int(pagas)
            i = fz.taxa_mensal(taxa, form["tipo_taxa"])
            db = get_db()
            restantes = (int(prazo_rest) if prazo_rest else prazo - pagas) if saldo_atual is not None else prazo - pagas
            total = pagas + restantes
            fatores, tr_erro = None, None
            if form["indexador"] == "TR":
                desde = fz.add_months(data_primeira, -1)
                tr_erro = indices.atualizar(db, desde)["erro"]
                venc = [fz.add_months(data_primeira, m) for m in range(max(prazo, total))]
                fatores, _ = indices.TabelaTR(db, desde).fatores(venc, form["projecao_tr"])
            ajuste = (pagas + 1, parcela_extrato) if parcela_extrato else (0, 0.0)
            alvos = {ajuste[0]: ajuste[1]} if parcela_extrato else None
            linhas = fz.gerar_tabela(valor, i, prazo, form["sistema"], data_primeira, encargos, 1, fatores,
                                     None if saldo_atual is not None else alvos)
            if saldo_atual is not None:
                linhas = linhas[:pagas] + fz.gerar_tabela(saldo_atual, i, restantes, form["sistema"],
                                                          fz.add_months(data_primeira, pagas), encargos, pagas + 1,
                                                          fatores[pagas:] if fatores else None, alvos)
            cur = db.execute(
                "INSERT INTO financiamentos (usuario_id, nome, instituicao, numero_contrato, valor_financiado, taxa_mensal, "
                "taxa_anual, prazo_meses, prazo_original, sistema, encargos_mensais, data_primeira, parcelas_pagas, "
                "indexador, projecao_tr, ajuste_parcela, ajuste_valor, origem) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'manual')",
                (g.user["id"], form["nome"][:100], form["instituicao"][:100], form["numero_contrato"][:40], valor, i,
                 fz.mensal_para_anual(i), len(linhas), prazo, form["sistema"], encargos, data_primeira.isoformat(), pagas,
                 form["indexador"], form["projecao_tr"], ajuste[0], ajuste[1]))
            salvar_parcelas(db, cur.lastrowid, linhas)
            db.commit()
            flash("Contrato cadastrado.", "ok")
            if tr_erro:
                flash(f"{tr_erro}. As parcelas usam a TR projetada e serão atualizadas na próxima consulta.", "erro")
            return redirect(url_for("visao_geral", fid=cur.lastrowid))
        return render_template("cadastrar.html", form=form, erro=erro, pagina="Cadastrar contrato")
    return render_template("cadastrar.html", form=form, pagina="Cadastrar contrato")


@app.route("/financiamentos/<int:fid>")
@login_required
def visao_geral(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    inicio = fz.add_months(date.fromisoformat(fin["data_primeira"]), -1).isoformat()
    serie = [{"d": inicio, "s": fin["valor_financiado"], "p": None, "j": None}] + [
        {"d": l["data_vencimento"], "s": l["saldo_devedor"], "p": l["valor_parcela"], "j": l["juros"]}
        for l in est["linhas"]]
    sugestao = sim = None
    if est["ctx"]:
        sugestao = fz.sugestao_amortizacao(est["saldo"])
        sim = fz.simular_amortizacao(est["ctx"], sugestao, "prazo")
    movs = [{"titulo": f"Parcela {l['numero']:03d} paga", "data": l["data_vencimento"], "valor": l["valor_parcela"],
             "tipo": "parcela"} for l in est["pagas"][-4:]]
    movs += [{"titulo": "Amortização extra", "data": a["data"], "valor": a["valor"], "tipo": "amortizacao"}
             for a in est["amortizacoes"][:4]]
    movs.sort(key=lambda m: m["data"], reverse=True)
    return render_template("visao_geral.html", fin=fin, est=est, serie=serie, sugestao=sugestao, sim=sim,
                           movs=movs[:4], pagina="Visão geral", hoje=date.today().isoformat())


POR_PAGINA = 12


@app.route("/financiamentos/<int:fid>/parcelas")
@login_required
def parcelas(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    sistema = request.args.get("sistema", fin["sistema"]).upper()
    if sistema not in ("SAC", "PRICE"):
        sistema = fin["sistema"]
    simulado = sistema != fin["sistema"]
    if simulado:
        linhas = fz.gerar_tabela(fin["valor_financiado"], fin["taxa_mensal"], fin["prazo_original"], sistema,
                                 date.fromisoformat(fin["data_primeira"]), fin["encargos_mensais"])
        extras = 0.0
    else:
        linhas = est["linhas"]
        extras = sum(a["valor"] for a in est["amortizacoes"])
    k = fin["parcelas_pagas"]
    com_tr = bool(est["tr"]) and not simulado
    conhecidos = est["tr"]["conhecidos"] if com_tr else 0
    for l in linhas:
        l["status"] = "paga" if l["numero"] <= k else ("proxima" if l["numero"] == k + 1 else "prevista")
        l["tr_extrato"] = com_tr and l["numero"] == est["tr"].get("ajustada")
        l["tr_projetada"] = com_tr and not l["tr_extrato"] and l["numero"] > k + conhecidos

    resumo = {"financiado": fin["valor_financiado"], "juros": sum(l["juros"] for l in linhas),
              "custo": sum(l["valor_parcela"] for l in linhas) + extras, "pagas": min(k, len(linhas)),
              "total": len(linhas)}

    status = request.args.get("status", "todas")
    q = request.args.get("q", "").strip()
    filtradas = [l for l in linhas if status == "todas" or l["status"] == status
                 or (status == "prevista" and l["status"] == "proxima")]
    if q:
        filtradas = [l for l in filtradas if q.lstrip("0") in str(l["numero"]) or q in fz.data_longa(l["data_vencimento"])]
    total_pag = max(1, math.ceil(len(filtradas) / POR_PAGINA))
    pagina = request.args.get("pagina", type=int)
    if pagina is None:
        idx = next((n for n, l in enumerate(filtradas) if l["status"] == "proxima"), 0) if not q else 0
        pagina = idx // POR_PAGINA + 1
    pagina = max(1, min(pagina, total_pag))
    ini = (pagina - 1) * POR_PAGINA
    return render_template("parcelas.html", fin=fin, est=est, sistema=sistema, simulado=simulado, resumo=resumo,
                           linhas=filtradas[ini:ini + POR_PAGINA], ini=ini, n_filtradas=len(filtradas),
                           pag=pagina, total_pag=total_pag, status=status, q=q, com_tr=com_tr,
                           pagina="Planilha de parcelas")


@app.route("/financiamentos/<int:fid>/amortizacoes", methods=["GET", "POST"])
@login_required
def amortizacoes(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    ctx = est["ctx"]
    sim = None
    form = {"modalidade": "prazo"}
    if ctx:
        valor_ini = fz.parse_num(request.args.get("valor")) or fz.sugestao_amortizacao(est["saldo"])
        form.update({"saldo": num_br(ctx["saldo"]), "prazo": str(ctx["n_rest"]), "taxa": num_br(ctx["i"] * 100, 2),
                     "sistema": ctx["sistema"], "valor": num_br(valor_ini)})
        sim_ctx = dict(ctx)
        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form if k in request.form})
        if request.method == "POST" and request.form.get("acao") == "simular":
            saldo, prazo, taxa = num("saldo"), num("prazo"), num("taxa")
            if not saldo or saldo <= 0 or not prazo or prazo < 1 or prazo != int(prazo) or taxa is None or taxa < 0:
                flash("Confira saldo devedor, prazo restante e taxa de juros.", "erro")
            else:
                # taxa exibida com 2 casas: se só difere pelo arredondamento, vale a do contrato
                i = ctx["i"] if abs(taxa / 100 - ctx["i"]) <= 0.0000501 else taxa / 100
                saldo = ctx["saldo"] if abs(saldo - ctx["saldo"]) < 0.005 else saldo
                sim_ctx.update({"saldo": saldo, "n_rest": int(prazo), "i": i,
                                "sistema": form["sistema"] if form["sistema"] in ("SAC", "PRICE") else ctx["sistema"]})
        valor = fz.parse_num(form["valor"]) or 0
        if valor > 0:
            sim = fz.simular_amortizacao(sim_ctx, valor, form["modalidade"] if form["modalidade"] in ("prazo", "parcela") else "prazo")
        sim_personalizada = any(sim_ctx[c] != ctx[c] for c in ("saldo", "n_rest", "i", "sistema"))
    else:
        sim_personalizada = False
    return render_template("amortizacoes.html", fin=fin, est=est, form=form, sim=sim,
                           sim_personalizada=sim_personalizada, pagina="Amortizações")


@app.route("/financiamentos/<int:fid>/recomendacao", methods=["GET", "POST"])
@login_required
def recomendacao(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    form = {"caixa": "", "despesas": "", "meses_reserva": "6", "rend": "", "renda": ""}
    rec = None
    if request.method == "POST" and est["ctx"]:
        form.update({k: request.form.get(k, "").strip() for k in form})
        caixa, despesas, rend, meses = num("caixa"), num("despesas"), num("rend"), num("meses_reserva", 6)
        if caixa is None or despesas is None or rend is None:
            flash("Preencha dinheiro disponível, despesas mensais e rendimento do investimento.", "erro")
        elif caixa < 0 or despesas < 0 or meses < 0 or meses > 60:
            flash("Confira os valores informados.", "erro")
        else:
            rec = fz.recomendar(est["ctx"], caixa, despesas, int(meses), rend / 100, num("renda"))
    return render_template("recomendacao.html", fin=fin, est=est, form=form, rec=rec, pagina="Devo amortizar?")


@app.post("/financiamentos/<int:fid>/amortizacoes/registrar")
@login_required
def aplicar_amortizacao(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    valor = num("valor")
    modalidade = request.form.get("modalidade")
    if not est["ctx"] or not valor or valor <= 0 or modalidade not in ("prazo", "parcela"):
        flash("Dados de amortização inválidos.", "erro")
        return redirect(url_for("amortizacoes", fid=fid))
    s = fz.simular_amortizacao(est["ctx"], valor, modalidade)
    k = fin["parcelas_pagas"]
    db = get_db()
    db.execute("DELETE FROM parcelas WHERE financiamento_id = ? AND numero > ?", (fid, k))
    salvar_parcelas(db, fid, s["nova"])
    db.execute("UPDATE financiamentos SET prazo_meses = ? WHERE id = ?", (k + len(s["nova"]), fid))
    db.execute(
        "INSERT INTO amortizacoes (financiamento_id, data, apos_parcela, valor, modalidade, saldo_antes, "
        "prazo_antes, prazo_depois, parcela_antes, parcela_depois, economia) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (fid, date.today().isoformat(), k, s["valor"], modalidade, est["saldo"], s["totais_base"]["meses"],
         s["totais_nova"]["meses"], s["totais_base"]["primeira"], s["totais_nova"]["primeira"], s["economia"]))
    db.commit()
    flash("Financiamento quitado!" if s["quitacao"] else
          f"Amortização de {fz.brl(s['valor'])} registrada. Economia estimada: {fz.brl(s['economia'])}.", "ok")
    return redirect(url_for("amortizacoes", fid=fid) + "#historico")


@app.route("/financiamentos/<int:fid>/editar", methods=["GET", "POST"])
@login_required
def editar(fid):
    fin = get_fin(fid)
    db = get_db()
    maximo = db.execute("SELECT COALESCE(MAX(numero), 0) FROM parcelas WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    minimo = db.execute("SELECT COALESCE(MAX(apos_parcela), 0) FROM amortizacoes WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()[:100]
        n = num("parcelas_pagas")
        indexador = request.form.get("indexador", fin["indexador"])
        projecao = request.form.get("projecao_tr", fin["projecao_tr"])
        extrato = num("parcela_extrato") or None
        if not nome:
            flash("Informe o nome do imóvel.", "erro")
        elif n is None or n != int(n) or not (minimo <= n <= maximo):
            flash(f"Parcelas pagas deve ser um número entre {minimo} e {maximo}.", "erro")
        elif indexador not in ("nenhum", "TR") or projecao not in ("media12", "zero"):
            flash("Escolha a atualização monetária do contrato.", "erro")
        elif extrato is not None and extrato <= fin["encargos_mensais"]:
            flash("O valor da parcela do extrato deve ser maior que os seguros e taxas mensais.", "erro")
        else:
            n = int(n)
            if extrato and fin["ajuste_parcela"] and fin["ajuste_parcela"] != n + 1 and extrato == fin["ajuste_valor"]:
                extrato = None  # valor pré-preenchido de outra parcela (mudou só o nº de pagas): não reaplica
            if extrato:
                ajuste = (n + 1, extrato)
            elif fin["ajuste_parcela"] and fin["ajuste_parcela"] <= n:
                ajuste = (fin["ajuste_parcela"], fin["ajuste_valor"])  # parcela já paga: fica no histórico
            else:
                ajuste = (0, 0.0)
            db.execute("UPDATE financiamentos SET nome = ?, instituicao = ?, numero_contrato = ?, parcelas_pagas = ?, "
                       "indexador = ?, projecao_tr = ?, ajuste_parcela = ?, ajuste_valor = ?, tr_assinatura = '' "
                       "WHERE id = ?",
                       (nome, request.form.get("instituicao", "").strip()[:100],
                        request.form.get("numero_contrato", "").strip()[:40], n, indexador, projecao,
                        ajuste[0], ajuste[1], fid))
            mudou_ajuste = ajuste != (fin["ajuste_parcela"], fin["ajuste_valor"])
            if indexador == "nenhum" and (fin["indexador"] == "TR" or mudou_ajuste):
                # sem TR: recalcula as parcelas ainda não pagas sem correção (com TR, sincronizar_tr cuida disso)
                _regerar_restantes(db, get_fin(fid), n)
            db.commit()
            flash("Contrato atualizado.", "ok")
            return redirect(url_for("visao_geral", fid=fid))
    return render_template("editar.html", fin=fin, minimo=minimo, maximo=maximo, pagina="Editar contrato")


@app.post("/financiamentos/<int:fid>/excluir")
@login_required
def excluir(fid):
    get_fin(fid)
    db = get_db()
    db.execute("DELETE FROM financiamentos WHERE id = ?", (fid,))
    db.commit()
    session.pop("fin_atual", None)
    flash("Contrato excluído.", "ok")
    return redirect(url_for("contratos"))


# ---------------------------------------------------------------- importação / exportação / ajuda

@app.route("/importar", methods=["GET", "POST"])
@login_required
def importar():
    if request.method == "POST":
        arq = request.files.get("arquivo")
        if not arq or not arq.filename:
            flash("Selecione um arquivo.", "erro")
            return render_template("importar.html", pagina="Importar planilha")
        try:
            dados = importar_planilha(arq.filename, arq.read())
        except ImportacaoErro as e:
            flash(str(e), "erro")
            return render_template("importar.html", pagina="Importar planilha")
        pagas = int(max(0, min(num("parcelas_pagas", 0), dados["prazo_meses"])))
        nome = request.form.get("nome", "").strip()[:100] or arq.filename
        db = get_db()
        cur = db.execute(
            "INSERT INTO financiamentos (usuario_id, nome, instituicao, valor_financiado, taxa_mensal, taxa_anual, "
            "prazo_meses, prazo_original, sistema, encargos_mensais, data_primeira, parcelas_pagas, origem) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'importado')",
            (g.user["id"], nome, request.form.get("instituicao", "").strip()[:100], dados["valor_financiado"],
             dados["taxa_mensal"], fz.mensal_para_anual(dados["taxa_mensal"]), dados["prazo_meses"],
             dados["prazo_meses"], dados["sistema"], dados["encargos_mensais"], dados["data_primeira"], pagas))
        salvar_parcelas(db, cur.lastrowid, dados["parcelas"])
        db.commit()
        flash(f"Planilha importada: {dados['prazo_meses']} parcelas, sistema {dados['sistema']} detectado, "
              f"taxa {fz.pct(dados['taxa_mensal'], 4)} a.m.", "ok")
        return redirect(url_for("visao_geral", fid=cur.lastrowid))
    return render_template("importar.html", pagina="Importar planilha")


def _csv_br(cabecalho, linhas):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(cabecalho)
    for l in linhas:
        w.writerow([f"{v:.2f}".replace(".", ",") if isinstance(v, float) else v for v in l])
    return "﻿" + buf.getvalue()


CABECALHO = ["Numero", "Vencimento", "Parcela", "Amortizacao", "Juros", "Encargos", "Saldo devedor"]


@app.route("/modelo.csv")
@login_required
def modelo():
    exemplo = fz.gerar_tabela(100000.0, 0.01, 12, "SAC", fz.add_months(date.today(), 1), 25.0)
    linhas = [[0, "", "", "", "", "", 100000.0]] + [
        [p["numero"], fz.add_months(date.today(), p["numero"]).strftime("%d/%m/%Y"), p["valor_parcela"],
         p["amortizacao"], p["juros"], p["encargos"], p["saldo_devedor"]] for p in exemplo]
    return Response(_csv_br(CABECALHO, linhas), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=modelo_financiamento.csv"})


@app.route("/financiamentos/<int:fid>/exportar.csv")
@login_required
def exportar(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    linhas = [[l["numero"], "/".join(reversed(l["data_vencimento"].split("-"))), l["valor_parcela"],
               l["amortizacao"], l["juros"], l["encargos"], l["saldo_devedor"], float(l.get("correcao") or 0)]
              for l in est["linhas"]]
    return Response(_csv_br(CABECALHO + ["Correcao TR"], linhas), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=financiamento_{fid}.csv"})


@app.route("/ajuda")
@login_required
def ajuda():
    return render_template("ajuda.html", pagina="Central de ajuda")


if __name__ == "__main__":
    app.run(debug=True)
