import csv
import io
import math
import os
import re
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import finance as fz
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
    g.user = db.execute("SELECT id, email, nome FROM usuarios WHERE id = ?", (uid,)).fetchone() if uid else None
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


# ---------------------------------------------------------------- helpers de dados

def get_fin(fid):
    fin = get_db().execute("SELECT * FROM financiamentos WHERE id = ? AND usuario_id = ?",
                           (fid, g.user["id"])).fetchone()
    if fin is None:
        abort(404)
    session["fin_atual"] = fid
    g.fin_nav = fin
    return fin


def carregar_estado(fin):
    db = get_db()
    linhas = [dict(r) for r in db.execute(
        "SELECT * FROM parcelas WHERE financiamento_id = ? ORDER BY numero", (fin["id"],))]
    amortizacoes = [dict(r) for r in db.execute(
        "SELECT * FROM amortizacoes WHERE financiamento_id = ? ORDER BY id DESC", (fin["id"],))]
    k = fin["parcelas_pagas"]
    restantes = [l for l in linhas if l["numero"] > k]
    pagas = [l for l in linhas if l["numero"] <= k]
    if restantes:
        prox = restantes[0]
        saldo = prox["saldo_devedor"] + prox["amortizacao"]
        ctx = {"saldo": saldo, "i": fin["taxa_mensal"], "n_rest": len(restantes), "sistema": fin["sistema"],
               "encargos": prox["encargos"], "data_prox": date.fromisoformat(prox["data_vencimento"]),
               "num_ini": prox["numero"]}
    else:
        prox, saldo, ctx = None, 0.0, None
    return {
        "linhas": linhas, "restantes": restantes, "pagas": pagas, "proxima": prox, "saldo": saldo, "ctx": ctx,
        "amortizacoes": amortizacoes,
        "juros_restantes": sum(l["juros"] for l in restantes),
        "total_pago": sum(l["valor_parcela"] for l in pagas) + sum(a["valor"] for a in amortizacoes),
        "economia_total": sum(a["economia"] for a in amortizacoes),
        "progresso": 1 - saldo / fin["valor_financiado"] if fin["valor_financiado"] else 0,
    }


def salvar_parcelas(db, fid, parcelas):
    db.executemany(
        "INSERT INTO parcelas (financiamento_id, numero, data_vencimento, valor_parcela, amortizacao, "
        "juros, encargos, saldo_devedor) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(fid, p["numero"], p["data_vencimento"], p["valor_parcela"], p["amortizacao"], p["juros"],
          p["encargos"], p["saldo_devedor"]) for p in parcelas])


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
        form.update({"valor": num_br(f["valor_financiado"]), "taxa": num_br(f["taxa_anual"] * 100, 4),
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
            i = fz.anual_para_mensal(taxa / 100) if form["tipo_taxa"] == "anual" else taxa / 100
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
    for l in linhas:
        l["status"] = "paga" if l["numero"] <= k else ("proxima" if l["numero"] == k + 1 else "prevista")

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
                           pag=pagina, total_pag=total_pag, status=status, q=q, pagina="Planilha de parcelas")


@app.route("/financiamentos/<int:fid>/amortizacoes", methods=["GET", "POST"])
@login_required
def amortizacoes(fid):
    fin = get_fin(fid)
    est = carregar_estado(fin)
    ctx = est["ctx"]
    sim = rec = None
    form = {"caixa": "", "despesas": "", "meses_reserva": "6", "rend": "", "renda": "", "modalidade": "prazo"}
    if ctx:
        valor_ini = fz.parse_num(request.args.get("valor")) or fz.sugestao_amortizacao(est["saldo"])
        form.update({"saldo": num_br(ctx["saldo"]), "prazo": str(ctx["n_rest"]), "taxa": num_br(ctx["i"] * 100, 4),
                     "sistema": ctx["sistema"], "valor": num_br(valor_ini)})
        sim_ctx = dict(ctx)
        if request.method == "POST":
            form.update({k: request.form.get(k, "").strip() for k in form if k in request.form})
        if request.method == "POST" and request.form.get("acao") == "simular":
            saldo, prazo, taxa = num("saldo"), num("prazo"), num("taxa")
            if not saldo or saldo <= 0 or not prazo or prazo < 1 or prazo != int(prazo) or taxa is None or taxa < 0:
                flash("Confira saldo devedor, prazo restante e taxa de juros.", "erro")
            else:
                sim_ctx.update({"saldo": saldo, "n_rest": int(prazo), "i": taxa / 100,
                                "sistema": form["sistema"] if form["sistema"] in ("SAC", "PRICE") else ctx["sistema"]})
        if request.method == "POST" and request.form.get("acao") == "recomendar":
            caixa, despesas, rend = num("caixa"), num("despesas"), num("rend")
            if caixa is None or despesas is None or rend is None:
                flash("Preencha dinheiro disponível, despesas mensais e rendimento do investimento.", "erro")
            else:
                rec = fz.recomendar(ctx, caixa, despesas, int(num("meses_reserva", 6)), rend / 100, num("renda"))
        valor = fz.parse_num(form["valor"]) or 0
        if valor > 0:
            sim = fz.simular_amortizacao(sim_ctx, valor, form["modalidade"] if form["modalidade"] in ("prazo", "parcela") else "prazo")
        sim_personalizada = any(sim_ctx[c] != ctx[c] for c in ("saldo", "n_rest", "i", "sistema"))
    else:
        sim_personalizada = False
    return render_template("amortizacoes.html", fin=fin, est=est, form=form, sim=sim, rec=rec,
                           sim_personalizada=sim_personalizada, pagina="Amortizações")


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
        if not nome:
            flash("Informe o nome do imóvel.", "erro")
        elif n is None or n != int(n) or not (minimo <= n <= maximo):
            flash(f"Parcelas pagas deve ser um número entre {minimo} e {maximo}.", "erro")
        else:
            db.execute("UPDATE financiamentos SET nome = ?, instituicao = ?, numero_contrato = ?, parcelas_pagas = ? "
                       "WHERE id = ?", (nome, request.form.get("instituicao", "").strip()[:100],
                                        request.form.get("numero_contrato", "").strip()[:40], int(n), fid))
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
               l["amortizacao"], l["juros"], l["encargos"], l["saldo_devedor"]] for l in est["linhas"]]
    return Response(_csv_br(CABECALHO, linhas), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=financiamento_{fid}.csv"})


@app.route("/ajuda")
@login_required
def ajuda():
    return render_template("ajuda.html", pagina="Central de ajuda")


if __name__ == "__main__":
    app.run(debug=True)
