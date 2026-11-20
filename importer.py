"""Importação de planilhas (CSV ou XLSX) com a evolução do financiamento."""
import csv
import io
import statistics
import unicodedata
from datetime import date, datetime

from finance import add_months, parse_num


class ImportacaoErro(Exception):
    pass


SINONIMOS = {
    "numero": {"numero", "n", "no", "num", "nparcela", "numeroparcela", "nrparcela", "mes", "periodo", "parcelan"},
    "data": {"data", "vencimento", "datavencimento", "dtvencimento", "datadevencimento"},
    "valor_parcela": {"parcela", "valorparcela", "prestacao", "valorprestacao", "pagamento", "valorpago", "valor",
                      "valortotal", "totalparcela"},
    "juros": {"juros", "valorjuros"},
    "amortizacao": {"amortizacao", "amort", "valoramortizacao"},
    "encargos": {"encargos", "seguro", "seguros", "taxas", "tarifas", "taxaadm", "taxaadministracao",
                 "segurostaxas", "seguroetaxas"},
    "saldo_devedor": {"saldo", "saldodevedor", "saldofinal", "saldoapos", "saldodevedorfinal", "saldoatual"},
}
OBRIGATORIAS = ("juros", "amortizacao", "saldo_devedor")


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return "".join(c for c in s if c.isalnum())


def _ler_linhas(nome_arquivo, conteudo):
    ext = nome_arquivo.lower().rsplit(".", 1)[-1] if "." in nome_arquivo else ""
    if ext in ("xlsx", "xlsm"):
        from openpyxl import load_workbook
        try:
            wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        except Exception as e:
            raise ImportacaoErro(f"Não foi possível abrir o arquivo Excel: {e}")
        return [list(r) for r in wb.active.iter_rows(values_only=True)]
    if ext in ("csv", "txt"):
        try:
            texto = conteudo.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = conteudo.decode("latin-1")
        try:
            dialeto = csv.Sniffer().sniff(texto[:4096], delimiters=";,\t")
            delim = dialeto.delimiter
        except csv.Error:
            delim = ";"
        return list(csv.reader(io.StringIO(texto), delimiter=delim))
    if ext == "xls":
        raise ImportacaoErro("Formato .xls antigo não suportado: salve a planilha como .xlsx ou .csv.")
    raise ImportacaoErro("Envie um arquivo .xlsx ou .csv.")


def _parse_data(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _achar_cabecalho(linhas):
    for idx, linha in enumerate(linhas[:30]):
        mapa = {}
        for col, celula in enumerate(linha):
            chave = _norm(celula)
            for campo, nomes in SINONIMOS.items():
                if chave in nomes and campo not in mapa:
                    mapa[campo] = col
        if all(c in mapa for c in OBRIGATORIAS):
            return idx, mapa
    raise ImportacaoErro(
        "Não encontrei o cabeçalho. A planilha precisa ter pelo menos as colunas "
        "'Juros', 'Amortização' e 'Saldo devedor'. Baixe o modelo para ver o formato.")


def importar_planilha(nome_arquivo, conteudo, data_primeira_padrao=None):
    linhas = _ler_linhas(nome_arquivo, conteudo)
    idx, mapa = _achar_cabecalho(linhas)

    def cel(linha, campo):
        col = mapa.get(campo)
        return linha[col] if col is not None and col < len(linha) else None

    saldo_inicial = None
    brutas = []
    for linha in linhas[idx + 1:]:
        if not any(c not in (None, "") for c in linha):
            continue
        if "numero" in mapa:
            num_raw = cel(linha, "numero")
            num = parse_num(num_raw)
            if num_raw not in (None, "") and num is None:
                continue  # linha de "Total" ou texto
        else:
            num = None
        juros = parse_num(cel(linha, "juros"))
        amort = parse_num(cel(linha, "amortizacao"))
        saldo = parse_num(cel(linha, "saldo_devedor"))
        if saldo is None:
            continue
        if (num == 0 or (not brutas and not juros and not amort)):
            saldo_inicial = saldo
            continue
        if juros is None or amort is None:
            continue
        valor = parse_num(cel(linha, "valor_parcela"))
        encargos = parse_num(cel(linha, "encargos"))
        if encargos is None:
            encargos = max(0.0, round(valor - juros - amort, 2)) if valor is not None else 0.0
        if valor is None:
            valor = juros + amort + encargos
        brutas.append({
            "data": _parse_data(cel(linha, "data")),
            "juros": juros, "amortizacao": amort, "encargos": encargos,
            "valor_parcela": valor, "saldo_devedor": max(saldo, 0.0),
        })

    if len(brutas) < 2:
        raise ImportacaoErro("A planilha precisa ter pelo menos 2 parcelas com juros, amortização e saldo.")

    valor_financiado = saldo_inicial or brutas[0]["saldo_devedor"] + brutas[0]["amortizacao"]

    # taxa mensal = juros / saldo anterior (mediana para tolerar arredondamentos)
    razoes, saldo_ant = [], valor_financiado
    for b in brutas:
        if saldo_ant > 0 and b["juros"] > 0:
            razoes.append(b["juros"] / saldo_ant)
        saldo_ant = b["saldo_devedor"]
    taxa_mensal = statistics.median(razoes) if razoes else 0.0

    amorts = [b["amortizacao"] for b in brutas[:-1]]
    media = statistics.mean(amorts)
    variacao = statistics.pstdev(amorts) / media if media else 1
    sistema = "SAC" if variacao < 0.01 else "PRICE"

    data_primeira = brutas[0]["data"] or data_primeira_padrao or date.today()
    parcelas = []
    for k, b in enumerate(brutas):
        parcelas.append({
            "numero": k + 1,
            "data_vencimento": (b["data"] or add_months(data_primeira, k)).isoformat(),
            "valor_parcela": round(b["valor_parcela"], 2),
            "amortizacao": round(b["amortizacao"], 2),
            "juros": round(b["juros"], 2),
            "encargos": round(b["encargos"], 2),
            "saldo_devedor": round(b["saldo_devedor"], 2),
        })

    return {
        "valor_financiado": round(valor_financiado, 2),
        "taxa_mensal": taxa_mensal,
        "prazo_meses": len(parcelas),
        "sistema": sistema,
        "encargos_mensais": parcelas[0]["encargos"],
        "data_primeira": data_primeira.isoformat(),
        "parcelas": parcelas,
    }
