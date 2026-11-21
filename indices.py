"""TR (Taxa Referencial) do Banco Central, com cache no SQLite.

Fonte: API SGS do Banco Central, série 226 — TR para o período que começa em cada data
(ex.: data 10/11/2026, dataFim 10/12/2026, valor 0,1693 = % do período).
Nos contratos habitacionais o saldo é corrigido no "dia de aniversário" pela TR do período
que termina naquele dia, então a parcela que vence em D usa a TR com início em D − 1 mês.
"""
import json
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from finance import add_months

SERIE_TR = 226
URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados?formato=json&dataInicial={ini}&dataFinal={fim}"
ESPERA_APOS_FALHA = timedelta(minutes=30)


class TRIndisponivel(Exception):
    pass


def _consultar(inicio: date, fim: date):
    url = URL.format(serie=SERIE_TR, ini=inicio.strftime("%d/%m/%Y"), fim=fim.strftime("%d/%m/%Y"))
    req = urllib.request.Request(url, headers={"User-Agent": "Amortiza/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # O BCB responde 304 (ou 404) quando a data final passa do último dia publicado
        # ou quando não há dados no intervalo.
        if e.code in (304, 404):
            return None
        raise TRIndisponivel(f"Banco Central respondeu {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TRIndisponivel(str(e)) from e


def _baixar(inicio: date, fim: date):
    """Baixa a série entre as datas. Retorna lista de (data ISO, valor em %)."""
    for _ in range(10):  # recua a data final até o último dia publicado
        if fim < inicio:
            return []
        dados = _consultar(inicio, fim)
        if dados is not None:
            return [(datetime.strptime(d["data"], "%d/%m/%Y").date().isoformat(), float(d["valor"])) for d in dados]
        fim -= timedelta(days=1)
    return []


def _meta(db, chave, valor=None):
    if valor is None:
        r = db.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
        return r[0] if r else None
    db.execute("INSERT OR REPLACE INTO meta (chave, valor) VALUES (?, ?)", (chave, valor))


def _faixas(inicio, fim, anos=5):
    """Quebra o intervalo em pedaços (a API limita o tamanho de cada consulta)."""
    while inicio <= fim:
        parte = min(fim, add_months(inicio, 12 * anos) - timedelta(days=1))
        yield inicio, parte
        inicio = parte + timedelta(days=1)


def atualizar(db, desde: date):
    """Garante a TR em cache desde `desde` até hoje (consulta a internet no máximo 1x por dia).

    Retorna {"ok": bool, "erro": str|None, "ultima": data ISO do último valor em cache ou None}.
    """
    hoje = date.today()
    agora = datetime.now()
    desde = min(desde, add_months(hoje, -14))  # garante 12 meses de histórico para a projeção
    lo, hi = db.execute("SELECT MIN(data), MAX(data) FROM indice_tr").fetchone()
    pedidos = []
    if lo is None:
        pedidos.append((desde, hoje))
    else:
        if desde < date.fromisoformat(lo):
            pedidos.append((desde, date.fromisoformat(lo) - timedelta(days=1)))
        if _meta(db, "tr_checado") != hoje.isoformat():
            pedidos.append((date.fromisoformat(hi) - timedelta(days=7), hoje))

    falha = _meta(db, "tr_falha")
    erro = None
    if pedidos and falha and agora - datetime.fromisoformat(falha) < ESPERA_APOS_FALHA:
        erro = "Banco Central indisponível no momento"
    elif pedidos:
        try:
            for ini, fim in pedidos:
                for a, b in _faixas(ini, fim):
                    db.executemany("INSERT OR REPLACE INTO indice_tr (data, valor) VALUES (?, ?)", _baixar(a, b))
            _meta(db, "tr_checado", hoje.isoformat())
            db.execute("DELETE FROM meta WHERE chave = 'tr_falha'")
        except TRIndisponivel as e:
            _meta(db, "tr_falha", agora.isoformat(timespec="seconds"))
            erro = f"Não foi possível consultar a TR no Banco Central ({e})"
        db.commit()
    ultima = db.execute("SELECT MAX(data) FROM indice_tr").fetchone()[0]
    return {"ok": erro is None, "erro": erro, "ultima": ultima}


# ---------------------------------------------------------------- Selic e CDI (para comparar com investimentos)

SERIE_SELIC_META = 432   # meta Selic definida pelo Copom, % a.a.
SERIE_CDI = 4389         # CDI anualizado base 252, % a.a. (referência dos CDBs "% do CDI")
URL_ULTIMOS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados/ultimos/10?formato=json"


def _ultimo_valor(serie):
    """Último valor publicado da série até hoje: (valor em %, data ISO)."""
    req = urllib.request.Request(URL_ULTIMOS.format(serie=serie),
                                 headers={"User-Agent": "Amortiza/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            dados = json.load(r)
    except urllib.error.HTTPError as e:
        raise TRIndisponivel(f"Banco Central respondeu {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TRIndisponivel(str(e)) from e
    hoje = date.today().isoformat()
    # a meta Selic vem com datas futuras (vale até a próxima reunião do Copom): fica só o que já vigora
    pontos = sorted((datetime.strptime(d["data"], "%d/%m/%Y").date().isoformat(), float(d["valor"])) for d in dados)
    validos = [p for p in pontos if p[0] <= hoje] or pontos
    if not validos:
        raise TRIndisponivel("série sem dados")
    data, valor = validos[-1]
    return valor, data


def taxas_mercado(db):
    """Selic meta e CDI atuais (% a.a.), consultados no máximo 1x por dia e guardados no SQLite.

    Retorna {"selic", "selic_data", "cdi", "cdi_data", "erro"} — valores None se nunca foi possível consultar.
    """
    salvo = json.loads(_meta(db, "mercado") or "{}")
    hoje, agora = date.today().isoformat(), datetime.now()
    erro = None
    if salvo.get("checado") != hoje:
        falha = _meta(db, "mercado_falha")
        if falha and agora - datetime.fromisoformat(falha) < ESPERA_APOS_FALHA:
            erro = "Banco Central indisponível no momento"
        else:
            try:
                selic, selic_data = _ultimo_valor(SERIE_SELIC_META)
                cdi, cdi_data = _ultimo_valor(SERIE_CDI)
                salvo = {"selic": selic, "selic_data": selic_data, "cdi": cdi, "cdi_data": cdi_data, "checado": hoje}
                _meta(db, "mercado", json.dumps(salvo))
                db.execute("DELETE FROM meta WHERE chave = 'mercado_falha'")
            except TRIndisponivel as e:
                _meta(db, "mercado_falha", agora.isoformat(timespec="seconds"))
                erro = f"Não foi possível consultar a Selic no Banco Central ({e})"
            db.commit()
    return {"selic": salvo.get("selic"), "selic_data": salvo.get("selic_data"),
            "cdi": salvo.get("cdi"), "cdi_data": salvo.get("cdi_data"), "erro": erro}


class TabelaTR:
    """Consulta a TR em cache e projeta meses futuros."""

    def __init__(self, db, desde: date):
        self.valores = {d: v / 100 for d, v in db.execute("SELECT data, valor FROM indice_tr WHERE data >= ?",
                                                          ((desde - timedelta(days=400)).isoformat(),))}
        self.ultima = max(self.valores) if self.valores else None

    def em(self, inicio: date):
        """TR (fração) do período que começa em `inicio`; None se ainda não publicada."""
        if not self.ultima or inicio.isoformat() > self.ultima:
            return None
        for atraso in range(4):  # dias sem publicação (ex.: 29 a 31) usam o dia anterior
            v = self.valores.get((inicio - timedelta(days=atraso)).isoformat())
            if v is not None:
                return v
        return None

    def media_12(self, dia: int):
        """Média da TR nos últimos 12 meses, no dia de aniversário do contrato."""
        if not self.ultima:
            return 0.0
        ultima = date.fromisoformat(self.ultima)
        ancora = date(2000, 1, dia)  # janeiro tem 31 dias; add_months ajusta o dia nos meses curtos
        meses = (ultima.year - 2000) * 12 + ultima.month - 1
        vals = []
        for m in range(13):
            v = self.em(add_months(ancora, meses - m))
            if v is not None:
                vals.append(v)
            if len(vals) == 12:
                break
        return sum(vals) / len(vals) if vals else 0.0

    def fatores(self, vencimentos, projecao="media12"):
        """Para cada vencimento, a TR aplicada ao saldo naquele mês (fração).

        Retorna (lista de fatores, quantos são valores publicados).
        """
        if not vencimentos:
            return [], 0
        dia = date.fromisoformat(str(vencimentos[0])[:10]).day
        futura = self.media_12(dia) if projecao == "media12" else 0.0
        fatores, conhecidos = [], 0
        for v in vencimentos:
            tr = self.em(add_months(date.fromisoformat(str(v)[:10]), -1))
            if tr is None:
                tr = futura
            else:
                conhecidos += 1
            fatores.append(tr)
        return fatores, conhecidos
