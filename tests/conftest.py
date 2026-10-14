"""Cada teste usa seu próprio esquema PostgreSQL; os cadastros reais são preservados."""
import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from psycopg import sql

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from app import app, conectar, inicializar


@pytest.fixture
def ambiente(monkeypatch):
    esquema = 'teste_' + uuid.uuid4().hex
    app.config.update(TESTING=True, APP_PASSWORD='')
    with app.app_context(), conectar() as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(esquema)))
    monkeypatch.setenv('PGOPTIONS', f'-c search_path={esquema}')
    with app.app_context():
        inicializar()
    yield app
    with app.app_context(), conectar() as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(esquema)))


@pytest.fixture
def cliente(ambiente):
    cliente = ambiente.test_client()
    html = cliente.get('/').get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    cliente.environ_base['HTTP_X_CSRF_TOKEN'] = token
    return cliente


@pytest.fixture
def nota():
    return {'numeroNotaFiscal': 'EXEMPLO-001', 'dataEmissao': '14/10/2026', 'fornecedor': {'razaoSocial': 'IGUAÇU MÁQUINAS LTDA', 'fantasia': 'IGUAÇU', 'cnpj': '11.111.111/0001-00'}, 'faturado': {'nomeCompleto': 'BELTRANO DA SILVA', 'cpf': '999.999.999-99'}, 'descricaoProdutos': 'Óleo diesel para operação de máquinas agrícolas', 'valorTotal': 1250.0, 'classificacaoDespesa': 'MANUTENÇÃO E OPERAÇÃO', 'parcelas': [{'numero': 1, 'dataVencimento': '27/10/2026', 'valor': 1250.0}]}
