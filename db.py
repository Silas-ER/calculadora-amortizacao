import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    nome          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    foto          BLOB,
    foto_tipo     TEXT,
    foto_versao   INTEGER NOT NULL DEFAULT 0,
    criado_em     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS financiamentos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id       INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    nome             TEXT NOT NULL,
    valor_financiado REAL NOT NULL,
    taxa_mensal      REAL NOT NULL,
    taxa_anual       REAL NOT NULL,
    prazo_meses      INTEGER NOT NULL,
    prazo_original   INTEGER NOT NULL,
    instituicao      TEXT NOT NULL DEFAULT '',
    numero_contrato  TEXT NOT NULL DEFAULT '',
    sistema          TEXT NOT NULL CHECK (sistema IN ('SAC', 'PRICE')),
    encargos_mensais REAL NOT NULL DEFAULT 0,
    data_primeira    TEXT NOT NULL,
    parcelas_pagas   INTEGER NOT NULL DEFAULT 0,
    origem           TEXT NOT NULL DEFAULT 'calculado',
    indexador        TEXT NOT NULL DEFAULT 'nenhum',   -- 'nenhum' ou 'TR'
    projecao_tr      TEXT NOT NULL DEFAULT 'media12',  -- TR futura: 'media12' ou 'zero'
    tr_assinatura    TEXT NOT NULL DEFAULT '',         -- controla quando recalcular as parcelas
    ajuste_parcela   INTEGER NOT NULL DEFAULT 0,       -- parcela cujo valor veio do extrato (0 = nenhuma)
    ajuste_valor     REAL NOT NULL DEFAULT 0,          -- valor total dessa parcela no extrato
    taxa_informada   REAL NOT NULL DEFAULT 0,          -- taxa como o usuário digitou (%)
    tipo_taxa        TEXT NOT NULL DEFAULT '',         -- 'anual' (efetiva), 'nominal', 'mensal' ou '' (não informado)
    criado_em        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS parcelas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    financiamento_id INTEGER NOT NULL REFERENCES financiamentos(id) ON DELETE CASCADE,
    numero           INTEGER NOT NULL,
    data_vencimento  TEXT NOT NULL,
    valor_parcela    REAL NOT NULL,
    amortizacao      REAL NOT NULL,
    juros            REAL NOT NULL,
    encargos         REAL NOT NULL DEFAULT 0,
    correcao         REAL NOT NULL DEFAULT 0,          -- atualização monetária (TR) somada ao saldo no mês
    saldo_devedor    REAL NOT NULL,
    UNIQUE (financiamento_id, numero)
);

-- TR (Banco Central, série SGS 226): valor em % para o período que começa em `data`
CREATE TABLE IF NOT EXISTS indice_tr (
    data  TEXT PRIMARY KEY,
    valor REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS amortizacoes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    financiamento_id INTEGER NOT NULL REFERENCES financiamentos(id) ON DELETE CASCADE,
    data             TEXT NOT NULL,
    apos_parcela     INTEGER NOT NULL,
    valor            REAL NOT NULL,
    modalidade       TEXT NOT NULL CHECK (modalidade IN ('prazo', 'parcela')),
    saldo_antes      REAL NOT NULL,
    prazo_antes      INTEGER NOT NULL,
    prazo_depois     INTEGER NOT NULL,
    parcela_antes    REAL NOT NULL,
    parcela_depois   REAL NOT NULL,
    economia         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS redefinicoes_senha (
    token_hash TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    expira_em  TEXT NOT NULL,
    usado      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fin_usuario ON financiamentos(usuario_id);
CREATE INDEX IF NOT EXISTS idx_parc_fin ON parcelas(financiamento_id, numero);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _colunas(db, tabela):
    return {r[1] for r in db.execute(f"PRAGMA table_info({tabela})")}


def _migrar(db):
    """Atualiza bancos criados por versões anteriores do app."""
    cols = _colunas(db, "usuarios")
    if "username" in cols:
        db.execute("ALTER TABLE usuarios RENAME COLUMN username TO email")
    if "nome" not in cols:
        db.execute("ALTER TABLE usuarios ADD COLUMN nome TEXT NOT NULL DEFAULT ''")
    if "foto" not in cols:
        db.execute("ALTER TABLE usuarios ADD COLUMN foto BLOB")
        db.execute("ALTER TABLE usuarios ADD COLUMN foto_tipo TEXT")
        db.execute("ALTER TABLE usuarios ADD COLUMN foto_versao INTEGER NOT NULL DEFAULT 0")
    cols = _colunas(db, "financiamentos")
    if "prazo_original" not in cols:
        db.execute("ALTER TABLE financiamentos ADD COLUMN prazo_original INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE financiamentos SET prazo_original = prazo_meses")
    for col in ("instituicao", "numero_contrato"):
        if col not in cols:
            db.execute(f"ALTER TABLE financiamentos ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    for col, padrao in (("indexador", "nenhum"), ("projecao_tr", "media12"), ("tr_assinatura", "")):
        if col not in cols:
            db.execute(f"ALTER TABLE financiamentos ADD COLUMN {col} TEXT NOT NULL DEFAULT '{padrao}'")
    if "ajuste_parcela" not in cols:
        db.execute("ALTER TABLE financiamentos ADD COLUMN ajuste_parcela INTEGER NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE financiamentos ADD COLUMN ajuste_valor REAL NOT NULL DEFAULT 0")
    if "tipo_taxa" not in cols:
        db.execute("ALTER TABLE financiamentos ADD COLUMN taxa_informada REAL NOT NULL DEFAULT 0")
        db.execute("ALTER TABLE financiamentos ADD COLUMN tipo_taxa TEXT NOT NULL DEFAULT ''")
    cols_parcelas = _colunas(db, "parcelas")
    if cols_parcelas and "correcao" not in cols_parcelas:
        db.execute("ALTER TABLE parcelas ADD COLUMN correcao REAL NOT NULL DEFAULT 0")
    db.commit()


def init_db(app):
    with app.app_context():
        db = get_db()
        if "usuarios" in {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            _migrar(db)
        db.executescript(SCHEMA)
