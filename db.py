"""Conexão PostgreSQL e inicialização do esquema usado pelo RuralFin."""
from pathlib import Path
import os

import click
import psycopg
from psycopg.rows import dict_row
from flask import current_app


def conectar():
    # O bloco `with conectar()` confirma a transação ou desfaz tudo em caso de erro.
    url = current_app.config.get('DATABASE_URL')
    if url:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    return psycopg.connect(
        host=os.getenv('PGHOST', '127.0.0.1'), port=os.getenv('PGPORT', '5433'),
        dbname=os.getenv('PGDATABASE', 'ruralfin'), user=os.getenv('PGUSER', 'ruralfin_app'),
        password=os.getenv('PGPASSWORD', ''), row_factory=dict_row, connect_timeout=5,
    )


def inicializar():
    sql = (Path(__file__).parent / 'sql' / '001_schema.sql').read_text(encoding='utf-8')
    with conectar() as conn:
        conn.execute(sql)


def registrar_comandos(app):
    @app.cli.command('init-db')
    def init_db():
        """Cria as tabelas, sem apagar registros já existentes."""
        inicializar()
        click.echo('Banco RuralFin inicializado com sucesso.')
