"""Cálculos financeiros: tabelas SAC/PRICE, simulação de amortização e recomendações."""
import calendar
import math
from datetime import date


# ---------------------------------------------------------------- utilidades

def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def anual_para_mensal(taxa_anual: float) -> float:
    return (1 + taxa_anual) ** (1 / 12) - 1


def mensal_para_anual(taxa_mensal: float) -> float:
    return (1 + taxa_mensal) ** 12 - 1


def taxa_mensal(taxa_pct: float, tipo: str) -> float:
    """Converte a taxa digitada para taxa efetiva mensal.

    efetiva  -> % a.a. efetiva:  (1 + a)^(1/12) - 1
    nominal  -> % a.a. nominal com capitalização mensal (padrão dos contratos habitacionais): a / 12
    mensal   -> % a.m.
    """
    t = taxa_pct / 100
    if tipo == "nominal":
        return t / 12
    if tipo == "mensal":
        return t
    return anual_para_mensal(t)


def pmt(saldo: float, i: float, n: int) -> float:
    """Parcela fixa (sem encargos) da tabela PRICE."""
    if n <= 0:
        return 0.0
    if i == 0:
        return saldo / n
    return saldo * i / (1 - (1 + i) ** -n)


# ---------------------------------------------------------------- tabelas

def correcao_para_parcela(saldo, i, restantes, sistema, prestacao):
    """Correção do saldo que faz a prestação (sem seguros/taxas) dar exatamente o valor informado.

    Calibra pelo extrato quando o banco corrige diferente da TR cheia (ex.: TR proporcional no 1º mês).
    """
    fator = pmt(1.0, i, restantes) if sistema == "PRICE" else 1 / restantes + i
    return round(prestacao / fator - saldo, 2)


def gerar_tabela(saldo, i, n, sistema, data_primeira, encargos=0.0, numero_inicial=1, correcoes=None, alvos=None):
    """Gera a evolução do financiamento.

    alvos: valores reais de algumas parcelas (extrato/boleto), {número: {"parcela": x, "saldo": y}}
    (um número sozinho vale como "parcela"). Com "parcela", a correção do mês passa a ser a que
    reproduz o valor cobrado; com "saldo", o saldo após o pagamento é o informado e a diferença
    entra na correção do mês. As parcelas seguintes partem desses valores.

    correcoes: TR (fração) de cada mês. Antes de cada parcela o saldo é atualizado pela TR,
    os juros incidem sobre o saldo atualizado e a parcela é recalculada pelo prazo restante
    (Price: nova prestação; SAC: saldo / meses restantes) — como nos contratos do SFH.
    Sem correção, o resultado é idêntico às tabelas SAC e Price tradicionais.

    Cada linha: numero, data_vencimento, correcao, amortizacao, juros, encargos,
    valor_parcela (amortização + juros + encargos) e saldo_devedor (após o pagamento).
    """
    linhas = []
    saldo = round(saldo, 2)
    if n <= 0 or saldo <= 0:
        return linhas
    encargos = round(encargos, 2)
    for k in range(n):
        # tudo em centavos, mês a mês (como o banco): saldo anterior + correção − amortização = saldo
        restantes = n - k
        a = (alvos or {}).get(numero_inicial + k)
        if isinstance(a, (int, float)):
            a = {"parcela": a}
        alvo = (a or {}).get("parcela")
        alvo_saldo = (a or {}).get("saldo")
        if alvo and alvo - encargos > 0:
            # parcela informada pelo extrato: a correção do mês é a que reproduz esse valor
            correcao = correcao_para_parcela(saldo, i, restantes, sistema, alvo - encargos)
        else:
            alvo = None
            tr = correcoes[k] if correcoes and k < len(correcoes) else 0.0
            correcao = round(saldo * tr, 2)
        saldo = round(saldo + correcao, 2)
        juros = round(saldo * i, 2)
        if alvo:
            amort = round(alvo - encargos - juros, 2)
        else:
            amort = round((pmt(saldo, i, restantes) - juros) if sistema == "PRICE" else saldo / restantes, 2)
        if k == n - 1 or amort > saldo:
            amort = saldo  # última parcela zera o saldo
        saldo = round(saldo - amort, 2)
        if alvo_saldo is not None and alvo_saldo > 0 and k < n - 1:
            # saldo informado pelo extrato: a diferença para o calculado entra como correção do mês
            correcao = round(correcao + alvo_saldo - saldo, 2)
            saldo = round(alvo_saldo, 2)
        linhas.append({
            "numero": numero_inicial + k,
            "data_vencimento": add_months(data_primeira, k).isoformat(),
            "correcao": correcao,
            "amortizacao": amort,
            "juros": juros,
            "encargos": encargos,
            "valor_parcela": round(amort + juros + encargos, 2),
            "saldo_devedor": saldo,
        })
    return linhas


def totais(linhas):
    return {
        "juros": sum(l["juros"] for l in linhas),
        "encargos": sum(l["encargos"] for l in linhas),
        "correcao": sum(l.get("correcao", 0) for l in linhas),
        "amortizacao": sum(l["amortizacao"] for l in linhas),
        "total": sum(l["valor_parcela"] for l in linhas),
        "meses": len(linhas),
        "primeira": linhas[0]["valor_parcela"] if linhas else 0.0,
        "ultima": linhas[-1]["valor_parcela"] if linhas else 0.0,
    }


def custo_efetivo_mensal(saldo, fluxos):
    """Taxa mensal (TIR) que iguala o saldo devedor às parcelas futuras (inclui encargos)."""
    if saldo <= 0 or not fluxos:
        return 0.0

    def vp(r):
        return sum(f / (1 + r) ** (k + 1) for k, f in enumerate(fluxos))

    if vp(0.0) <= saldo:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if vp(mid) > saldo:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------- amortização

def _prazo_mantendo_parcela(ctx, novo_saldo):
    saldo, i, n = ctx["saldo"], ctx["i"], ctx["n_rest"]
    if ctx["sistema"] == "PRICE":
        p = pmt(saldo, i, n)
        if i == 0:
            meses = math.ceil(novo_saldo / p - 1e-9)
        else:
            meses = math.ceil(-math.log(1 - novo_saldo * i / p) / math.log(1 + i) - 1e-9)
    else:  # SAC: mantém a amortização mensal constante
        meses = math.ceil(novo_saldo / (saldo / n) - 1e-9)
    return max(1, min(meses, n))


def tabela_base(ctx):
    return gerar_tabela(ctx["saldo"], ctx["i"], ctx["n_rest"], ctx["sistema"],
                        ctx["data_prox"], ctx["encargos"], ctx["num_ini"], ctx.get("correcoes"))


def simular_amortizacao(ctx, valor, modalidade):
    """Compara o cenário atual com uma amortização extra.

    ctx: saldo (após a última parcela paga), i, n_rest, sistema, encargos, data_prox, num_ini,
         correcoes (TR de cada mês restante, opcional)
    modalidade: 'prazo' (mantém parcela, reduz prazo) ou 'parcela' (mantém prazo, reduz parcela)
    """
    base = tabela_base(ctx)
    valor = max(0.0, min(valor, ctx["saldo"]))
    novo_saldo = ctx["saldo"] - valor
    if novo_saldo < 0.01:
        nova, quitacao = [], True
    else:
        quitacao = False
        meses = _prazo_mantendo_parcela(ctx, novo_saldo) if modalidade == "prazo" else ctx["n_rest"]
        nova = gerar_tabela(novo_saldo, ctx["i"], meses, ctx["sistema"], ctx["data_prox"],
                            ctx["encargos"], ctx["num_ini"], ctx.get("correcoes"))
    tb, tn = totais(base), totais(nova)
    economia = tb["total"] - tn["total"] - valor
    return {
        "valor": valor,
        "modalidade": modalidade,
        "quitacao": quitacao,
        "base": base,
        "nova": nova,
        "totais_base": tb,
        "totais_nova": tn,
        "economia": economia,
        "meses_reduzidos": tb["meses"] - tn["meses"],
        "reducao_parcela": tb["primeira"] - tn["primeira"],
        "retorno": economia / valor if valor else 0.0,
    }


def _ctx_em(ctx, base, m):
    """Contexto do financiamento daqui a m parcelas, se nada mudar (para comparar "agora" x "depois")."""
    return {**ctx, "saldo": base[m - 1]["saldo_devedor"] if m else ctx["saldo"], "n_rest": ctx["n_rest"] - m,
            "data_prox": add_months(ctx["data_prox"], m), "num_ini": ctx["num_ini"] + m,
            "correcoes": ctx["correcoes"][m:] if ctx.get("correcoes") else None}


def fase_do_financiamento(fracao_juros):
    if fracao_juros >= 0.5:
        return "inicio"   # mais juros que amortização: amortizar tem o maior efeito
    if fracao_juros >= 0.25:
        return "meio"
    return "fim"


def momento_amortizacao(ctx, valor_ref=1000.0):
    """Quanto vale amortizar agora, olhando para a composição das parcelas.

    - fracao_juros: parte da próxima prestação (sem seguros) que é juros;
    - virada: primeira parcela em que a amortização passa a ser maior que os juros;
    - efeito: amortizando valor_ref hoje (reduzindo o prazo), quanto deixa de ser pago em parcelas;
    - marcos: o mesmo valor amortizado agora, em 1, 5 e 10 anos — mostra que quanto antes, melhor.
    """
    base = tabela_base(ctx)
    if not base:
        return None
    valor_ref = min(valor_ref, ctx["saldo"])

    def composicao(l):
        aj = l["amortizacao"] + l["juros"]
        return l["juros"] / aj if aj else 0.0

    virada = next((l for l in base if l["amortizacao"] >= l["juros"]), None)
    marcos = []
    for m, rotulo in ((0, "Agora"), (12, "Daqui a 1 ano"), (60, "Daqui a 5 anos"), (120, "Daqui a 10 anos")):
        if m >= len(base) - 1:
            break
        s = simular_amortizacao(_ctx_em(ctx, base, m), valor_ref, "prazo")
        evitado = s["totais_base"]["total"] - s["totais_nova"]["total"]
        marcos.append({"rotulo": rotulo, "meses": m, "numero": base[m]["numero"], "data": base[m]["data_vencimento"],
                       "fracao_juros": composicao(base[m]), "evitado": evitado, "economia": s["economia"],
                       "multiplicador": evitado / valor_ref if valor_ref else 0.0,
                       "meses_reduzidos": s["meses_reduzidos"]})
    agora = marcos[0]
    return {
        "proxima": base[0], "fracao_juros": agora["fracao_juros"], "fase": fase_do_financiamento(agora["fracao_juros"]),
        "virada": virada, "ja_virou": virada is base[0],
        "valor_ref": valor_ref, "efeito": agora, "marcos": marcos,
    }


def aliquota_ir(anos):
    """Tabela regressiva do IR sobre renda fixa (CDB, Tesouro etc.), pelo prazo da aplicação."""
    dias = anos * 365
    if dias <= 180:
        return 0.225
    if dias <= 360:
        return 0.20
    if dias <= 720:
        return 0.175
    return 0.15


def amortizar_ou_investir(i_mensal, tr_mensal, valor, taxa_anual, tipo, anos):
    """Compara amortizar `valor` hoje com investir o mesmo valor por `anos`.

    Amortizar "rende" exatamente o custo da dívida que deixa de existir (juros do contrato + TR),
    sem risco e sem IR. Investir rende a taxa informada; se for bruta, desconta o IR no resgate.
    taxa_anual: fração (0.12 = 12% a.a.); tipo: 'bruta' (sujeita a IR) ou 'liquida' (isenta ou já líquida).
    """
    custo_divida = ((1 + i_mensal) * (1 + tr_mensal)) ** 12 - 1
    ir = aliquota_ir(anos) if tipo == "bruta" else 0.0
    bruto_final = valor * (1 + taxa_anual) ** anos
    investir_final = valor + (bruto_final - valor) * (1 - ir)
    rend_liquido = (investir_final / valor) ** (1 / anos) - 1 if valor and anos else 0.0
    amortizar_final = valor * (1 + custo_divida) ** anos  # juros + correção que deixam de incidir sobre o valor
    # taxa bruta que empata: (1 + t)^n − 1 = ((1 + c)^n − 1) / (1 − IR)
    empate = ((1 + ((1 + custo_divida) ** anos - 1) / (1 - ir)) ** (1 / anos) - 1) if anos else custo_divida
    return {
        "valor": valor, "anos": anos, "tipo": tipo, "taxa": taxa_anual, "ir": ir,
        "custo_divida": custo_divida, "rend_liquido": rend_liquido,
        "amortizar_final": amortizar_final, "investir_final": investir_final,
        "ganho_amortizar": amortizar_final - valor, "ganho_investir": investir_final - valor,
        "diferenca": amortizar_final - investir_final,
        "vence": "amortizar" if amortizar_final >= investir_final else "investir",
        "taxa_empate": empate,
    }


def termina_antes_de(linhas, data_limite):
    """Data da última parcela, se o contrato terminar antes de data_limite (ex.: 5 anos da contratação)."""
    if not linhas:
        return None
    fim = date.fromisoformat(linhas[-1]["data_vencimento"])
    return fim if fim < data_limite else None


def recomendar(ctx, caixa, despesas_mensais, meses_reserva, rend_anual_liquido, renda_mensal=None):
    """Diz se vale amortizar, quanto e em qual modalidade."""
    base = tabela_base(ctx)
    if not base:
        return {"decisao": "quitado", "motivos": ["Este financiamento já está quitado."]}

    cet_m = custo_efetivo_mensal(ctx["saldo"], [l["valor_parcela"] for l in base])
    cet_a = mensal_para_anual(cet_m)
    reserva = despesas_mensais * meses_reserva
    livre = max(0.0, caixa - reserva)
    spread = cet_a - rend_anual_liquido
    parcela_atual = base[0]["valor_parcela"]
    prestacao = base[0]["juros"] + base[0]["amortizacao"]  # sem seguros e taxas
    fatia_juros = base[0]["juros"] / prestacao if prestacao else 0
    motivos = []

    if livre <= 0:
        decisao, valor = "nao", 0.0
        motivos.append(
            f"Sua reserva de emergência ideal é {brl(reserva)} ({meses_reserva} meses de despesas). "
            f"Faltam {brl(reserva - caixa)} para completá-la: priorize a reserva antes de amortizar.")
    elif spread <= 0:
        decisao, valor = "nao", 0.0
        motivos.append(
            f"O custo efetivo da dívida ({pct(cet_a)} a.a.) é menor ou igual ao rendimento líquido "
            f"do seu investimento ({pct(rend_anual_liquido)} a.a.). Vale mais a pena manter o dinheiro investido.")
    elif spread < 0.01:
        decisao, valor = "parcial", livre * 0.5
        motivos.append(
            f"A dívida custa {pct(cet_a)} a.a. e o investimento rende {pct(rend_anual_liquido)} a.a.: "
            "diferença pequena (menos de 1 ponto percentual). Amortizar metade do valor livre equilibra "
            "economia de juros e liquidez.")
    else:
        decisao, valor = "sim", livre
        motivos.append(
            f"A dívida custa {pct(cet_a)} a.a., mais que os {pct(rend_anual_liquido)} a.a. líquidos do "
            f"investimento (diferença de {pct(spread)}). Cada real amortizado \"rende\" o custo da dívida, "
            "sem risco e sem imposto.")
        motivos.append(f"Você tem {brl(livre)} acima da reserva de emergência ({brl(reserva)}).")

    valor = min(valor, ctx["saldo"])

    comprometimento = parcela_atual / renda_mensal if renda_mensal else None
    if comprometimento is not None and comprometimento > 0.30:
        modalidade = "parcela"
        motivos.append(
            f"A parcela compromete {pct(comprometimento, 1)} da sua renda (acima de 30%). "
            "Sugerimos reduzir o valor da parcela para aliviar o orçamento.")
    else:
        modalidade = "prazo"
        if decisao != "nao":
            motivos.append("Reduzir o prazo gera a maior economia total de juros.")

    if decisao != "nao":
        fase = fase_do_financiamento(fatia_juros)
        if fase == "inicio":
            motivos.append(
                f"{pct(fatia_juros, 0)} da próxima parcela é juros: o contrato está na fase em que amortizar tem o "
                "maior efeito. Cada real amortizado agora elimina parcelas do fim, cheias de juros.")
        elif fase == "meio":
            motivos.append(
                f"{pct(fatia_juros, 0)} da próxima parcela é juros. Amortizar ainda compensa bem, e quanto antes, melhor.")
        else:
            motivos.append(
                f"Só {pct(fatia_juros, 0)} da próxima parcela é juros: a maior parte já abate a dívida, "
                "então o ganho por real amortizado é menor.")
        if ctx["n_rest"] <= 12:
            motivos.append("Restam poucas parcelas, então o impacto da amortização é pequeno.")

    sims = {}
    if valor > 0:
        sims = {m: simular_amortizacao(ctx, valor, m) for m in ("prazo", "parcela")}

    sensibilidade = []
    if livre > 0:
        vistos = set()
        for frac in (0.1, 0.25, 0.5, 0.75, 1.0):
            v = round(min(livre * frac, ctx["saldo"]), 2)
            if v <= 0 or v in vistos:
                continue
            vistos.add(v)
            s = simular_amortizacao(ctx, v, "prazo")
            sensibilidade.append({"valor": v, "economia": s["economia"],
                                  "meses": s["meses_reduzidos"], "retorno": s["retorno"]})

    return {
        "decisao": decisao, "valor": valor, "modalidade": modalidade, "motivos": motivos,
        "cet_mensal": cet_m, "cet_anual": cet_a, "reserva": reserva, "livre": livre,
        "spread": spread, "parcela_atual": parcela_atual, "simulacoes": sims,
        "sensibilidade": sensibilidade,
    }


# ---------------------------------------------------------------- formatação

def brl(v):
    if v is None:
        return "-"
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def pct(v, casas=2):
    if v is None:
        return "-"
    return f"{v * 100:.{casas}f}".replace(".", ",") + "%"


MESES_ABREV = ["jan.", "fev.", "mar.", "abr.", "mai.", "jun.", "jul.", "ago.", "set.", "out.", "nov.", "dez."]
_EXTENSO = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez", "onze"]


def data_longa(s):
    """'2025-02-10' -> '10 fev. 2025'"""
    if not s:
        return "-"
    d = s if isinstance(s, date) else date.fromisoformat(str(s)[:10])
    return f"{d.day:02d} {MESES_ABREV[d.month - 1]} {d.year}"


def meses_extenso(n):
    """20 -> 'Um ano e oito meses'"""
    anos, meses = divmod(int(n), 12)
    partes = []
    if anos:
        partes.append(f"{_EXTENSO[anos] if anos < 12 else anos} {'ano' if anos == 1 else 'anos'}")
    if meses:
        partes.append(f"{_EXTENSO[meses]} {'mês' if meses == 1 else 'meses'}")
    txt = " e ".join(partes) or "zero meses"
    return txt[0].upper() + txt[1:]


def sugestao_amortizacao(saldo):
    """Valor ilustrativo para o card de 'próximo passo': ~6% do saldo, arredondado."""
    if saldo <= 0:
        return 0.0
    passo = 5000 if saldo > 100000 else 1000
    return float(min(saldo, max(passo, round(saldo * 0.06 / passo) * passo)))


def parse_num(v):
    """Aceita 1234.56, 1.234,56, 'R$ 1.234,56', 12,5% etc."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace("%", "").replace("\xa0", "").replace(" ", "")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s and len(s.split(".")[1]) == 3 and s.split(".")[0].lstrip("-") not in ("", "0"):
        s = s.replace(".", "")  # "80.000" no padrão brasileiro = oitenta mil
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n
