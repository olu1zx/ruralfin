-- RuralFin - Etapa 2. Executar no banco ruralfin, pelo pgAdmin ou flask init-db.
-- Valores financeiros usam NUMERIC, sem arredondamento de ponto flutuante.
CREATE TABLE IF NOT EXISTS pessoas (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tipo VARCHAR(20) NOT NULL CHECK (tipo IN ('CLIENTE-FORNECEDOR', 'FATURADO')),
    razao_social VARCHAR(200) NOT NULL CHECK (length(trim(razao_social)) > 0),
    fantasia VARCHAR(200) NOT NULL DEFAULT '',
    documento VARCHAR(14) NOT NULL CHECK (documento ~ '^([0-9]{11}|[0-9]{14})$'),
    status VARCHAR(7) NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO','INATIVO')),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tipo, documento),
    CHECK (tipo <> 'FATURADO' OR length(documento) = 11)
);

CREATE TABLE IF NOT EXISTS classificacao (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tipo VARCHAR(7) NOT NULL CHECK (tipo IN ('DESPESA','RECEITA')),
    descricao VARCHAR(120) NOT NULL CHECK (length(trim(descricao)) > 0),
    chave_descricao VARCHAR(120) NOT NULL,
    status VARCHAR(7) NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO','INATIVO')),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tipo, chave_descricao)
);

CREATE TABLE IF NOT EXISTS movimentocontas (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tipo VARCHAR(8) NOT NULL CHECK (tipo IN ('APAGAR','ARECEBER')),
    pessoa_id BIGINT NOT NULL REFERENCES pessoas(id) ON DELETE RESTRICT,
    faturado_id BIGINT NOT NULL REFERENCES pessoas(id) ON DELETE RESTRICT,
    numero_nota_fiscal VARCHAR(60) NOT NULL,
    data_emissao DATE NOT NULL,
    descricao_produtos TEXT NOT NULL DEFAULT '',
    valor_total NUMERIC(14,2) NOT NULL CHECK (valor_total > 0),
    status VARCHAR(7) NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO','INATIVO')),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tipo, pessoa_id, numero_nota_fiscal)
);

CREATE TABLE IF NOT EXISTS parcelacontas (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    movimento_id BIGINT NOT NULL REFERENCES movimentocontas(id) ON DELETE RESTRICT,
    identificacao VARCHAR(20) NOT NULL,
    numero INTEGER NOT NULL CHECK (numero > 0),
    data_vencimento DATE NOT NULL,
    valor NUMERIC(14,2) NOT NULL CHECK (valor > 0),
    status VARCHAR(7) NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO','INATIVO')),
    UNIQUE (movimento_id, numero)
);

CREATE TABLE IF NOT EXISTS movimento_classificacao (
    movimento_id BIGINT NOT NULL REFERENCES movimentocontas(id) ON DELETE RESTRICT,
    classificacao_id BIGINT NOT NULL REFERENCES classificacao(id) ON DELETE RESTRICT,
    PRIMARY KEY (movimento_id, classificacao_id)
);

CREATE TABLE IF NOT EXISTS documentos (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    movimento_id BIGINT NOT NULL UNIQUE REFERENCES movimentocontas(id) ON DELETE RESTRICT,
    nome_arquivo VARCHAR(255) NOT NULL,
    conteudo BYTEA NOT NULL,
    sha256 CHAR(64) NOT NULL UNIQUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Histórico de inclusão, edição, inativação e reativação.
CREATE TABLE IF NOT EXISTS auditoria (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entidade VARCHAR(40) NOT NULL,
    registro_id BIGINT NOT NULL,
    operacao VARCHAR(20) NOT NULL,
    usuario VARCHAR(100) NOT NULL,
    ocorrido_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    dados JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pessoas_status_nome ON pessoas(status, razao_social);
CREATE INDEX IF NOT EXISTS idx_classificacao_status_descricao ON classificacao(status, descricao);
CREATE INDEX IF NOT EXISTS idx_contas_status_emissao ON movimentocontas(status, data_emissao);
CREATE INDEX IF NOT EXISTS idx_parcelas_vencimento ON parcelacontas(data_vencimento);

-- Mesmo no pgAdmin, um DELETE físico das entidades principais é recusado.
CREATE OR REPLACE FUNCTION impedir_exclusao_fisica() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Cadastros não podem ser excluídos. Altere o status para INATIVO.';
END;
$$ LANGUAGE plpgsql;
DO $$
DECLARE tabela TEXT;
BEGIN
    FOREACH tabela IN ARRAY ARRAY['pessoas','classificacao','movimentocontas','parcelacontas'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'sem_delete_' || tabela
                       AND tgrelid = tabela::regclass) THEN
            EXECUTE format('CREATE TRIGGER %I BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION impedir_exclusao_fisica()', 'sem_delete_' || tabela, tabela);
        END IF;
    END LOOP;
END $$;
