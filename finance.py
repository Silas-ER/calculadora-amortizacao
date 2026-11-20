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


def pmt(saldo: float, i: float, n: int) -> float:
    """Parcela fixa (sem encargos) da tabela PRICE."""
    if n <= 0:
        return 0.0
    if i == 0:
        return saldo / n
    return saldo * i / (1 - (1 + i) ** -n)


# ---------------------------------------------------------------- tabelas

def gerar_tabela(saldo, i, n, sistema, data_primeira, encargos=0.0, numero_inicial=1):
    """Gera a evolução do financiamento.

    Cada linha: numero, data_vencimento, amortizacao, juros, encargos,
    valor_parcela (amortização + juros + encargos) e saldo_devedor (após o pagamento).
    """
    linhas = []
    if n <= 0 or saldo <= 0:
        return linhas
    parcela_price = pmt(saldo, i, n)
    amort_sac = saldo / n
    for k in range(n):
        juros = saldo * i
        amort = (parcela_price - juros) if sistema == "PRICE" else amort_sac
        if k == n - 1:
            amort = saldo  # zera resíduos de arredondamento
        saldo -= amort
        linhas.append({
            "numero": numero_inicial + k,
            "data_vencimento": add_months(data_primeira, k).isoformat(),
            "amortizacao": round(amort, 2),
            "juros": round(juros, 2),
            "encargos": round(encargos, 2),
            "valor_parcela": round(amort + juros + encargos, 2),
            "saldo_devedor": round(max(saldo, 0.0), 2),
        })
    return linhas


def totais(linhas):
    return {
        "juros": sum(l["juros"] for l in linhas),
        "encargos": sum(l["encargos"] for l in linhas),
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
                        ctx["data_prox"], ctx["encargos"], ctx["num_ini"])


def simular_amortizacao(ctx, valor, modalidade):
    """Compara o cenário atual com uma amortização extra.

    ctx: saldo, i, n_rest, sistema, encargos, data_prox, num_ini
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
                            ctx["encargos"], ctx["num_ini"])
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
    fatia_juros = base[0]["juros"] / parcela_atual if parcela_atual else 0
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
        motivos.append(
            f"Hoje {pct(fatia_juros, 1)} da próxima parcela é juros. Quanto antes amortizar, maior o efeito: "
            "os juros incidem sobre o saldo devedor.")
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
