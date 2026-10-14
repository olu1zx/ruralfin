"""Prepara uma instância PostgreSQL isolada do projeto, sem alterar o serviço existente."""
import json
import os
import secrets
import socket
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from dotenv import dotenv_values

RAIZ = Path(__file__).resolve().parent
LOCAL = RAIZ / '.local-postgres'
BIN = Path(os.getenv('POSTGRES_BIN', r'C:\Program Files\PostgreSQL\16\bin'))


def executar(*args):
    if args[0] == 'pg_ctl.exe':
        # No Windows, o processo postgres pode herdar as pipes e impedir o EOF.
        # Um arquivo de log permite que pg_ctl termine sem aguardar o servidor.
        with (LOCAL / 'controle.log').open('a', encoding='utf-8') as log:
            resultado = subprocess.run([str(BIN / args[0]), *map(str, args[1:])], stdout=log, stderr=log,
                                       timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        if resultado.returncode:
            raise RuntimeError('Falha no controle do banco. Consulte .local-postgres/controle.log.')
        return resultado
    resultado = subprocess.run([str(BIN / args[0]), *map(str, args[1:])],
                               capture_output=True, text=True, encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if resultado.returncode:
        raise RuntimeError(resultado.stderr or resultado.stdout)
    return resultado


def iniciar():
    if not (LOCAL / 'data' / 'PG_VERSION').exists():
        raise RuntimeError('Execute primeiro: python banco_local.py configurar')
    resultado = subprocess.run([str(BIN / 'pg_ctl.exe'), '-D', str(LOCAL / 'data'), 'status'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if resultado.returncode:
        executar('pg_ctl.exe', '-D', LOCAL / 'data', '-l', LOCAL / 'postgres.log', '-w', 'start')
    print('PostgreSQL do RuralFin iniciado.')


def configurar():
    existente = (LOCAL / 'data' / 'PG_VERSION').exists()
    if existente and (RAIZ / '.env').exists():
        iniciar()
        return
    if (RAIZ / '.env').exists():
        raise RuntimeError('Já existe um .env. Preserve sua configuração e siga o README para usar outro banco.')
    if not (BIN / 'initdb.exe').exists():
        raise RuntimeError('PostgreSQL não encontrado. Defina POSTGRES_BIN com a pasta bin da instalação.')
    porta = 5433
    with socket.socket() as teste:
        if not existente and teste.connect_ex(('127.0.0.1', porta)) == 0:
            raise RuntimeError('A porta 5433 está ocupada. Use a conexão existente ou altere a porta deste script.')
    LOCAL.mkdir(exist_ok=True)
    senha_admin = (LOCAL / 'admin-password.txt').read_text(encoding='utf-8') if existente else secrets.token_urlsafe(32)
    senha_app = secrets.token_urlsafe(32)
    segredo = secrets.token_hex(32)
    arquivo_senha = LOCAL / 'admin-password.txt'
    if not existente:
        arquivo_senha.write_text(senha_admin, encoding='utf-8')
        executar('initdb.exe', '-D', LOCAL / 'data', '-U', 'ruralfin_admin', '--pwfile', arquivo_senha,
                 '--auth-host=scram-sha-256', '--auth-local=scram-sha-256', '--encoding=UTF8', '--locale=C')
        with (LOCAL / 'data' / 'postgresql.conf').open('a', encoding='utf-8') as config:
            config.write("\n# Instância local RuralFin\nlisten_addresses = '127.0.0.1'\nport = 5433\n")
    iniciar()
    with psycopg.connect(host='127.0.0.1', port=porta, dbname='postgres', user='ruralfin_admin',
                         password=senha_admin, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE ruralfin_app LOGIN PASSWORD {}').format(sql.Literal(senha_app)))
        conn.execute('CREATE DATABASE ruralfin OWNER ruralfin_app')
    (RAIZ / '.env').write_text(
        f'PGHOST=127.0.0.1\nPGPORT={porta}\nPGDATABASE=ruralfin\nPGUSER=ruralfin_app\nPGPASSWORD={senha_app}\n'
        f'SECRET_KEY={segredo}\nPORT=5001\nAPP_USERNAME=admin\nAPP_PASSWORD=\nAPP_ENV=development\n'
        'GEMINI_MODELS=gemini-3.8-flash,gemini-3.5-flash\nGEMINI_API_KEY=\n', encoding='utf-8')
    (RAIZ / 'pgadmin-servidor.json').write_text(json.dumps({'Servers': {'1': {
        'Name': 'RuralFin local', 'Group': 'Servers', 'Host': '127.0.0.1', 'Port': porta,
        'MaintenanceDB': 'ruralfin', 'Username': 'ruralfin_app', 'SSLMode': 'prefer', 'SavePassword': False,
    }}}, indent=2), encoding='utf-8')
    print('Banco ruralfin e usuário ruralfin_app criados. Conexão salva no .env (senha não exibida).')


def parar():
    executar('pg_ctl.exe', '-D', LOCAL / 'data', '-m', 'fast', '-w', 'stop')
    print('PostgreSQL do RuralFin parado.')


def backup():
    configuracao = dotenv_values(RAIZ / '.env')
    ambiente = os.environ.copy()
    ambiente.update({k: v for k, v in configuracao.items() if k.startswith('PG') and v is not None})
    from datetime import datetime
    pasta = RAIZ / 'tmp' / 'backups'
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f'ruralfin-{datetime.now():%Y%m%d-%H%M%S}.dump'
    subprocess.run([str(BIN / 'pg_dump.exe'), '-Fc', '-f', str(destino)], env=ambiente, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    print(f'Backup criado em: {destino}. Inclui os PDFs armazenados no banco.')


if __name__ == '__main__':
    import sys
    comandos = {'configurar': configurar, 'iniciar': iniciar, 'parar': parar, 'backup': backup}
    if len(sys.argv) != 2 or sys.argv[1] not in comandos:
        raise SystemExit('Uso: python banco_local.py configurar|iniciar|parar|backup')
    comandos[sys.argv[1]]()
