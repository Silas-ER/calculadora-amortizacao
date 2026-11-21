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


def _alvos(db, fid):
    """Valores reais informados por parcela (extrato/boleto): {numero: {"parcela": x, "saldo": y}}."""
    return {r["numero"]: {"parcela": r["valor_parcela"], "saldo": r["saldo_devedor"]}
            for r in db.execute("SELECT * FROM ajustes_parcela WHERE financiamento_id = ?", (fid,))}


def _salvar_ajuste(db, fid, numero, parcela=None, saldo=None):
    """Grava (ou remove, se ambos vazios) os valores reais de uma parcela."""
    if parcela is None and saldo is None:
        db.execute("DELETE FROM ajustes_parcela WHERE financiamento_id = ? AND numero = ?", (fid, numero))
    else:
        db.execute("INSERT OR REPLACE INTO ajustes_parcela (financiamento_id, numero, valor_parcela, saldo_devedor) "
                   "VALUES (?, ?, ?, ?)", (fid, numero, parcela, saldo))


def _fatores_tr(db, fin, vencimentos):
    desde = fz.add_months(date.fromisoformat(fin["data_primeira"]), -1)
    return indices.TabelaTR(db, desde).fatores(vencimentos, fin["projecao_tr"])[0]


def recalcular_desde(db, fin, numero, correcoes=None):
    """Recalcula as parcelas a partir de `numero` (inclusive), partindo do saldo antes dela.

    Usa a TR (se o contrato tiver) e os valores reais informados em ajustes_parcela.
    """
    restantes = [l for l in _ler_parcelas(db, fin["id"]) if l["numero"] >= numero]
    if not restantes:
        return
    prox = restantes[0]
    if correcoes is None and fin["indexador"] == "TR":
        correcoes = _fatores_tr(db, fin, [l["data_vencimento"] for l in restantes])
    novas = fz.gerar_tabela(_saldo_antes(prox), fin["taxa_mensal"], len(restantes), fin["sistema"],
                            date.fromisoformat(prox["data_vencimento"]), prox["encargos"], prox["numero"],
                            correcoes, _alvos(db, fin["id"]))
    db.execute("DELETE FROM parcelas WHERE financiamento_id = ? AND numero >= ?", (fin["id"], prox["numero"]))
    salvar_parcelas(db, fin["id"], novas)


def _regerar_restantes(db, fin, k, correcoes=None):
    """Recalcula as parcelas posteriores a k a partir do saldo atual."""
    recalcular_desde(db, fin, k + 1, correcoes)


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
    alvos = _alvos(db, fin["id"])
    assinatura = (f"{status['ultima']}|{fin['projecao_tr']}|{k}|{len(linhas)}|"
                  + ";".join(f"{n}:{a['parcela']}:{a['saldo']}" for n, a in sorted(alvos.items())))
    if restantes and assinatura != fin["tr_assinatura"]:
        _regerar_restantes(db, fin, k, fatores)
        db.execute("UPDATE financiamentos SET tr_assinatura = ? WHERE id = ?", (assinatura, fin["id"]))
        db.commit()
        linhas = _ler_parcelas(db, fin["id"])
    # parcelas com valores do extrato: as simulações usam a correção efetivamente aplicada naquele mês
    restantes = [l for l in linhas if l["numero"] > k]
    for idx, l in enumerate(restantes):
        if l["numero"] in alvos and idx < len(fatores):
            base = _saldo_antes(l)
            fatores[idx] = l["correcao"] / base if base else 0.0
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
        "amortizacoes": amortizacoes, "tr": tr, "ajustes": _alvos(db, fin["id"]),
        "minimo_edicao": max((a["apos_parcela"] for a in amortizacoes), default=0),
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


CAMPOS_CONTRATO = ("nome", "instituicao", "numero_contrato", "valor", "taxa", "tipo_taxa", "prazo", "sistema",
                   "data_primeira", "encargos", "parcelas_pagas", "saldo_atual", "prazo_restante", "parcela_extrato",
                   "indexador", "projecao_tr")
# campos que definem a tabela inteira: mudar qualquer um recalcula todas as parcelas
CAMPOS_CONDICOES = ("valor", "taxa", "tipo_taxa", "prazo", "sistema", "data_primeira", "encargos")


def _ler_contrato(form):
    """Valida o formulário de contrato (cadastro e edição). Retorna (erro, dados)."""
    p = fz.parse_num
    d = {"valor": p(form["valor"]), "taxa": p(form["taxa"]), "prazo": p(form["prazo"]),
         "encargos": p(form["encargos"]) or 0.0, "pagas": p(form["parcelas_pagas"]) or 0,
         "saldo_atual": p(form["saldo_atual"]), "prazo_rest": p(form["prazo_restante"]),
         "extrato": p(form["parcela_extrato"]) or None, "data_primeira": None}
    erro = None
    if not form["nome"]:
        erro = "Dê um nome ao imóvel."
    elif not d["valor"] or d["valor"] <= 0:
        erro = "Informe o valor financiado."
    elif d["taxa"] is None or d["taxa"] < 0 or d["taxa"] > 100:
        erro = "Informe a taxa de juros."
    elif form["tipo_taxa"] not in ("anual", "nominal", "mensal"):
        erro = "Escolha o tipo da taxa."
    elif not d["prazo"] or d["prazo"] < 1 or d["prazo"] > 600 or d["prazo"] != int(d["prazo"]):
        erro = "Informe um prazo entre 1 e 600 meses."
    elif form["sistema"] not in ("SAC", "PRICE"):
        erro = "Escolha SAC ou Price."
    elif d["encargos"] < 0:
        erro = "Seguros e taxas não podem ser negativos."
    elif d["pagas"] < 0 or d["pagas"] != int(d["pagas"]) or d["pagas"] > d["prazo"]:
        erro = "Parcelas pagas deve ser um número entre 0 e o prazo."
    elif d["saldo_atual"] is not None and d["saldo_atual"] <= 0:
        erro = "O saldo devedor atual deve ser maior que zero. Deixe em branco para calcular pelas condições originais."
    elif d["prazo_rest"] is not None and (d["saldo_atual"] is None or d["prazo_rest"] < 1 or d["prazo_rest"] > 600
                                         or d["prazo_rest"] != int(d["prazo_rest"])):
        erro = "Parcelas restantes só valem junto com o saldo devedor atual (entre 1 e 600)."
    elif form["indexador"] not in ("nenhum", "TR") or form["projecao_tr"] not in ("media12", "zero"):
        erro = "Escolha a atualização monetária do contrato."
    elif d["extrato"] is not None and d["extrato"] <= d["encargos"]:
        erro = "O valor da parcela do extrato deve ser maior que os seguros e taxas mensais."
    try:
        d["data_primeira"] = date.fromisoformat(form["data_primeira"])
    except ValueError:
        erro = erro or "Informe a data da 1ª parcela."
    if not erro:
        d["prazo"], d["pagas"] = int(d["prazo"]), int(d["pagas"])
        d["prazo_rest"] = int(d["prazo_rest"]) if d["prazo_rest"] else None
        d["i"] = fz.taxa_mensal(d["taxa"], form["tipo_taxa"])
    return erro, d


def _montar_parcelas(db, d, form, pagas_existentes=None, restantes_existentes=None, alvos=None):
    """Gera a tabela do contrato. Retorna (linhas, aviso_tr).

    pagas_existentes: mantém essas linhas (parcelas já pagas) e recalcula só o restante a partir do saldo atual.
    alvos: valores reais por parcela (ajustes_parcela). Sem eles, usa o "valor da próxima parcela" do formulário.
    """
    pagas, data1, i, sistema = d["pagas"], d["data_primeira"], d["i"], form["sistema"]
    usa_saldo = d["saldo_atual"] is not None
    if usa_saldo:
        restantes = d["prazo_rest"] or restantes_existentes or d["prazo"] - pagas
    else:
        restantes = d["prazo"] - pagas
    fatores, tr_erro = None, None
    if form["indexador"] == "TR":
        desde = fz.add_months(data1, -1)
        tr_erro = indices.atualizar(db, desde)["erro"]
        venc = [fz.add_months(data1, m) for m in range(max(d["prazo"], pagas + restantes))]
        fatores, _ = indices.TabelaTR(db, desde).fatores(venc, form["projecao_tr"])
    if alvos is None:
        alvos = {pagas + 1: {"parcela": d["extrato"]}} if d["extrato"] else None
    if pagas_existentes is not None:
        linhas = pagas_existentes
    else:
        linhas = fz.gerar_tabela(d["valor"], i, d["prazo"], sistema, data1, d["encargos"], 1, fatores, alvos)
    if usa_saldo:
        linhas = linhas[:pagas] + fz.gerar_tabela(d["saldo_atual"], i, restantes, sistema, fz.add_months(data1, pagas),
                                                  d["encargos"], pagas + 1, fatores[pagas:] if fatores else None, alvos)
    return linhas, tr_erro


def _avisar_tr(tr_erro):
    if tr_erro:
        flash(f"{tr_erro}. As parcelas usam a TR projetada e serão atualizadas na próxima consulta.", "erro")


@app.route("/financiamentos/cadastrar", methods=["GET", "POST"])
@login_required
def cadastrar():
    form = {k: "" for k in CAMPOS_CONTRATO}
    form.update({"tipo_taxa": "anual", "sistema": "SAC", "encargos": "0", "parcelas_pagas": "0",
                 "indexador": "TR", "projecao_tr": "media12"})
    erro = None
    if request.method == "POST":
        form.update({k: request.form.get(k, form[k]).strip() for k in CAMPOS_CONTRATO})
        erro, d = _ler_contrato(form)
        if not erro:
            db = get_db()
            linhas, tr_erro = _montar_parcelas(db, d, form)
            cur = db.execute(
                "INSERT INTO financiamentos (usuario_id, nome, instituicao, numero_contrato, valor_financiado, taxa_mensal, "
                "taxa_anual, taxa_informada, tipo_taxa, prazo_meses, prazo_original, sistema, encargos_mensais, "
                "data_primeira, parcelas_pagas, indexador, projecao_tr, origem) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'manual')",
                (g.user["id"], form["nome"][:100], form["instituicao"][:100], form["numero_contrato"][:40], d["valor"],
                 d["i"], fz.mensal_para_anual(d["i"]), d["taxa"], form["tipo_taxa"], len(linhas), d["prazo"],
                 form["sistema"], d["encargos"], d["data_primeira"].isoformat(), d["pagas"], form["indexador"],
                 form["projecao_tr"]))
            if d["extrato"]:
                _salvar_ajuste(db, cur.lastrowid, d["pagas"] + 1, d["extrato"])
            salvar_parcelas(db, cur.lastrowid, linhas)
            db.commit()
            flash("Contrato cadastrado.", "ok")
            _avisar_tr(tr_erro)
            return redirect(url_for("visao_geral", fid=cur.lastrowid))
    return render_template("cadastrar.html", form=form, erro=erro, pagina="Cadastrar contrato")


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
    hoje = date.today().isoformat()
    for l in linhas:
        l["status"] = "paga" if l["numero"] <= k else ("proxima" if l["numero"] == k + 1 else "prevista")
        l["extrato"] = not simulado and est["ajustes"].get(l["numero"])
        l["tr_projetada"] = com_tr and not l["extrato"] and l["numero"] > k + conhecidos
        l["editavel"] = not simulado and l["numero"] > est["minimo_edicao"]
        # marcar como paga: parcelas vencidas e a próxima; desmarcar: só a última paga
        l["pode_pagar"] = not simulado and l["numero"] > k and (l["data_vencimento"] <= hoje or l["numero"] == k + 1)
        l["pode_desfazer"] = not simulado and l["numero"] == k and k > est["minimo_edicao"]

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
    sim, simulou = None, False
    form = {"modalidade": "prazo", "valor": ""}
    if ctx:
        # simulação sempre com os dados do contrato salvo; o usuário escolhe só o valor e a modalidade
        valor_ini = fz.parse_num(request.args.get("valor")) or fz.sugestao_amortizacao(est["saldo"])
        form["valor"] = num_br(valor_ini)
        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form if k in request.form})
            simulou = True
        if form["modalidade"] not in ("prazo", "parcela"):
            form["modalidade"] = "prazo"
        valor = fz.parse_num(form["valor"]) or 0
        if valor > 0:
            sim = fz.simular_amortizacao(ctx, valor, form["modalidade"])
        elif simulou:
            flash("Informe quanto deseja amortizar.", "erro")
            simulou = False
    return render_template("amortizacoes.html", fin=fin, est=est, form=form, sim=sim, simulou=simulou,
                           aviso5=aviso_5_anos(fin, sim), pagina="Amortizações")


def aviso_5_anos(fin, sim):
    """Data em que o contrato terminaria, se a amortização simulada o quitar em menos de 5 anos da contratação.

    A data de contratação não é guardada: usamos um mês antes da 1ª parcela (aproximação).
    """
    if not sim:
        return None
    inicio = fz.add_months(date.fromisoformat(fin["data_primeira"]), -1)
    limite = fz.add_months(inicio, 60)
    if sim["quitacao"]:
        return date.today() if date.today() < limite else None
    if sim["modalidade"] != "prazo":
        return None
    return fz.termina_antes_de(sim["nova"], limite)


@app.route("/financiamentos/<int:fid>/recomendacao", methods=["GET", "POST"])
@login_required
def recomendacao(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    momento = fz.momento_amortizacao(est["ctx"]) if est["ctx"] else None
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
    aviso5 = aviso_5_anos(fin, rec["simulacoes"].get(rec["modalidade"])) if rec and rec.get("simulacoes") else None

    # amortizar ou investir? (formulário GET, independente do de cima)
    tr_mensal = est["tr"]["media"] if est["tr"] and fin["projecao_tr"] == "media12" else 0.0
    comp_form = {"valor": request.args.get("cv", ""), "taxa": request.args.get("ct", ""),
                 "tipo": request.args.get("ctipo", "bruta"), "anos": request.args.get("ca", "5")}
    comp = None
    if est["ctx"] and (comp_form["valor"] or comp_form["taxa"]):
        v, t, a = fz.parse_num(comp_form["valor"]), fz.parse_num(comp_form["taxa"]), fz.parse_num(comp_form["anos"])
        if not v or v <= 0 or t is None or t < 0 or t > 100 or not a or not 1 <= a <= 50 or a != int(a):
            flash("Para comparar, informe o valor, a taxa do investimento e um prazo entre 1 e 50 anos.", "erro")
        else:
            comp = fz.amortizar_ou_investir(fin["taxa_mensal"], tr_mensal, min(v, est["saldo"]), t / 100,
                                            "liquida" if comp_form["tipo"] == "liquida" else "bruta", int(a))
            comp["limitado"] = v > est["saldo"]
    return render_template("recomendacao.html", fin=fin, est=est, form=form, rec=rec, momento=momento,
                           aviso5=aviso5, comp=comp, comp_form=comp_form, tr_mensal=tr_mensal,
                           pagina="Devo amortizar?")


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
    # valores de extrato das parcelas futuras eram do cenário sem esta amortização
    db.execute("DELETE FROM ajustes_parcela WHERE financiamento_id = ? AND numero > ?", (fid, k))
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


def _volta_planilha(fid, numero):
    """Volta para a página da planilha onde está a parcela."""
    return redirect(url_for("parcelas", fid=fid, pagina=(numero - 1) // POR_PAGINA + 1) + f"#p{numero}")


def _parcela_ou_404(db, fid, numero):
    linha = db.execute("SELECT * FROM parcelas WHERE financiamento_id = ? AND numero = ?", (fid, numero)).fetchone()
    if not linha:
        abort(404)
    return dict(linha)


@app.post("/financiamentos/<int:fid>/parcelas/<int:numero>/pagar")
@login_required
def marcar_paga(fid, numero):
    """Marca a parcela (e as anteriores) como paga, ou desfaz a última marcação."""
    fin = get_fin(fid)
    db = get_db()
    _parcela_ou_404(db, fid, numero)
    minimo = db.execute("SELECT COALESCE(MAX(apos_parcela), 0) FROM amortizacoes WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    k = fin["parcelas_pagas"]
    if request.form.get("acao") == "desfazer":
        if numero != k or numero <= minimo:
            flash("Só é possível desmarcar a última parcela paga.", "erro")
            return _volta_planilha(fid, numero)
        novo = numero - 1
        flash(f"Parcela {numero:03d} voltou a ficar em aberto.", "ok")
    else:
        if numero <= k:
            return _volta_planilha(fid, numero)
        novo = numero
        flash(f"Parcela {numero:03d} marcada como paga." if numero == k + 1
              else f"Parcelas {k + 1:03d} a {numero:03d} marcadas como pagas.", "ok")
    db.execute("UPDATE financiamentos SET parcelas_pagas = ?, tr_assinatura = '' WHERE id = ?", (novo, fid))
    db.commit()
    return _volta_planilha(fid, numero)


@app.post("/financiamentos/<int:fid>/parcelas/<int:numero>/editar")
@login_required
def editar_parcela(fid, numero):
    """Informa os valores reais de uma parcela (extrato/boleto) e recalcula todas as seguintes."""
    fin = get_fin(fid)
    db = get_db()
    linha = _parcela_ou_404(db, fid, numero)
    minimo = db.execute("SELECT COALESCE(MAX(apos_parcela), 0) FROM amortizacoes WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    if numero <= minimo:
        flash("Esta parcela é anterior a uma amortização registrada e não pode ser alterada.", "erro")
        return _volta_planilha(fid, numero)
    atual = _alvos(db, fid).get(numero, {})
    if request.form.get("acao") == "remover":
        _salvar_ajuste(db, fid, numero)
        msg = f"Parcela {numero:03d} voltou ao valor calculado."
    else:
        parcela, saldo = num("valor_parcela"), num("saldo_devedor")
        if parcela is not None and parcela <= linha["encargos"]:
            flash("A prestação deve ser maior que os seguros e taxas do mês.", "erro")
            return _volta_planilha(fid, numero)
        if saldo is not None and saldo <= 0:
            flash("O saldo devedor deve ser maior que zero.", "erro")
            return _volta_planilha(fid, numero)
        # campo pré-preenchido com o valor calculado e não alterado: não vira ajuste
        if parcela is not None and abs(parcela - linha["valor_parcela"]) < 0.005 and not atual.get("parcela"):
            parcela = None
        if saldo is not None and abs(saldo - linha["saldo_devedor"]) < 0.005 and not atual.get("saldo"):
            saldo = None
        _salvar_ajuste(db, fid, numero, parcela, saldo)
        msg = f"Parcela {numero:03d} atualizada. As parcelas seguintes foram recalculadas."
    recalcular_desde(db, fin, numero)
    if request.form.get("paga") and numero > fin["parcelas_pagas"]:
        db.execute("UPDATE financiamentos SET parcelas_pagas = ? WHERE id = ?", (numero, fid))
    db.execute("UPDATE financiamentos SET tr_assinatura = '' WHERE id = ?", (fid,))
    db.commit()
    flash(msg, "ok")
    return _volta_planilha(fid, numero)


def _form_do_contrato(fin, ajustes):
    """Valores atuais do contrato no formato do formulário."""
    prox = (ajustes.get(fin["parcelas_pagas"] + 1) or {}).get("parcela")
    if fin["tipo_taxa"]:
        taxa, tipo = fin["taxa_informada"], fin["tipo_taxa"]
    else:  # contratos antigos, importados ou do simulador: mostra a taxa efetiva anual
        taxa, tipo = fin["taxa_anual"] * 100, "anual"
    return {
        "nome": fin["nome"], "instituicao": fin["instituicao"], "numero_contrato": fin["numero_contrato"],
        "valor": num_br(fin["valor_financiado"], 2), "taxa": num_br(taxa, 2), "tipo_taxa": tipo,
        "prazo": str(fin["prazo_original"] or fin["prazo_meses"]), "sistema": fin["sistema"],
        "data_primeira": fin["data_primeira"], "encargos": num_br(fin["encargos_mensais"], 2),
        "parcelas_pagas": str(fin["parcelas_pagas"]), "saldo_atual": "", "prazo_restante": "",
        "parcela_extrato": num_br(prox, 2) if prox else "",
        "indexador": fin["indexador"], "projecao_tr": fin["projecao_tr"],
    }


@app.route("/financiamentos/<int:fid>/editar", methods=["GET", "POST"])
@login_required
def editar(fid):
    fin = get_fin(fid)
    db = get_db()
    maximo = db.execute("SELECT COALESCE(MAX(numero), 0) FROM parcelas WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    minimo = db.execute("SELECT COALESCE(MAX(apos_parcela), 0) FROM amortizacoes WHERE financiamento_id = ?",
                        (fid,)).fetchone()[0]
    tem_amortizacoes = db.execute("SELECT 1 FROM amortizacoes WHERE financiamento_id = ? LIMIT 1",
                                  (fid,)).fetchone() is not None
    ajustes = _alvos(db, fid)
    atual = _form_do_contrato(fin, ajustes)
    form, erro = dict(atual), None
    if request.method == "POST":
        form.update({k: request.form.get(k, atual[k]).strip() for k in CAMPOS_CONTRATO})
        numericos = ("valor", "taxa", "prazo", "encargos")
        mudou = [k for k in CAMPOS_CONDICOES
                 if (fz.parse_num(form[k]) != fz.parse_num(atual[k]) if k in numericos else form[k] != atual[k])]
        erro, d = _ler_contrato(form)
        if not erro:
            n = d["pagas"]
            limite = d["prazo"] if mudou else maximo
            if mudou and tem_amortizacoes:
                erro = ("Este contrato tem amortizações registradas, que dependem das condições atuais. "
                        "Para mudar valor, taxa, prazo, sistema, data ou seguros, cadastre o contrato novamente.")
            elif not (minimo <= n <= limite):
                erro = f"Parcelas pagas deve ser um número entre {minimo} e {limite}."
        if not erro:
            # "valor da próxima parcela": vale para a parcela n + 1 (os valores de outras parcelas ficam como estão)
            extrato = d["extrato"]
            prox_atual = (ajustes.get(fin["parcelas_pagas"] + 1) or {}).get("parcela")
            saldo_prox = (ajustes.get(n + 1) or {}).get("saldo")
            if extrato and n != fin["parcelas_pagas"] and extrato == prox_atual:
                pass  # valor pré-preenchido de outra parcela (mudou só o nº de pagas): não reaplica
            elif extrato:
                _salvar_ajuste(db, fid, n + 1, extrato, saldo_prox)
            elif prox_atual and n == fin["parcelas_pagas"]:
                _salvar_ajuste(db, fid, n + 1, None, saldo_prox)  # campo apagado: remove o valor
            tr_erro = None
            if mudou or d["saldo_atual"] is not None:
                # recalcula a tabela: tudo (condições novas) ou só a partir do saldo atual informado
                manter = None if mudou else _ler_parcelas(db, fid)[:n]
                if not mudou:  # condições de sempre: usa os valores exatos do contrato (o formulário arredonda)
                    d.update(i=fin["taxa_mensal"], encargos=fin["encargos_mensais"])
                linhas, tr_erro = _montar_parcelas(db, d, form, manter, maximo - n, _alvos(db, fid))
                db.execute("DELETE FROM parcelas WHERE financiamento_id = ?", (fid,))
                salvar_parcelas(db, fid, linhas)
            db.execute(
                "UPDATE financiamentos SET nome = ?, instituicao = ?, numero_contrato = ?, valor_financiado = ?, "
                "taxa_mensal = ?, taxa_anual = ?, taxa_informada = ?, tipo_taxa = ?, prazo_meses = ?, prazo_original = ?, "
                "sistema = ?, encargos_mensais = ?, data_primeira = ?, parcelas_pagas = ?, indexador = ?, projecao_tr = ?, "
                "tr_assinatura = '' WHERE id = ?",
                (form["nome"][:100], form["instituicao"][:100], form["numero_contrato"][:40],
                 d["valor"] if mudou else fin["valor_financiado"],
                 d["i"] if mudou else fin["taxa_mensal"],
                 fz.mensal_para_anual(d["i"]) if mudou else fin["taxa_anual"],
                 d["taxa"] if mudou else fin["taxa_informada"],
                 form["tipo_taxa"] if mudou else fin["tipo_taxa"],
                 db.execute("SELECT COUNT(*) FROM parcelas WHERE financiamento_id = ?", (fid,)).fetchone()[0],
                 d["prazo"] if mudou else fin["prazo_original"],
                 form["sistema"] if mudou else fin["sistema"],
                 d["encargos"] if mudou else fin["encargos_mensais"],
                 d["data_primeira"].isoformat() if mudou else fin["data_primeira"],
                 n, form["indexador"], form["projecao_tr"], fid))
            if form["indexador"] == "nenhum" and not mudou and d["saldo_atual"] is None and (
                    fin["indexador"] == "TR" or _alvos(db, fid) != ajustes):
                # sem TR: recalcula as parcelas ainda não pagas (com TR, sincronizar_tr cuida disso)
                _regerar_restantes(db, get_fin(fid), n)
            db.commit()
            flash("Contrato recalculado com as novas condições." if mudou else "Contrato atualizado.", "ok")
            _avisar_tr(tr_erro)
            return redirect(url_for("visao_geral", fid=fid))
    return render_template("cadastrar.html", form=form, erro=erro, fin=fin, editando=True, minimo=minimo,
                           maximo=maximo, tem_amortizacoes=tem_amortizacoes, pagina="Editar contrato")


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
