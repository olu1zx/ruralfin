"""
RuralFin - Extração de Dados de Nota Fiscal com IA (Gemini)
=============================================================
Backend em Flask. Responsável por:
  1. Servir a página HTML (interface do usuário).
  2. Receber o PDF enviado pelo navegador.
  3. Chamar a API do Gemini no servidor (evita bloqueio de CORS
     do navegador e mantém a chave de API fora do código-fonte
     que o cliente/navegador consegue enxergar).
  4. Devolver o JSON extraído para a tela.

Como executar:
  pip install -r requirements.txt
  python app.py
  Configure o banco conforme README.md e execute flask --app app init-db.
  Depois abra http://127.0.0.1:5001 no navegador.
"""

import base64
import json
import time
import os
import secrets
from pathlib import Path
from decimal import Decimal
from datetime import date, datetime

import requests
import psycopg
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from db import registrar_comandos
from financeiro import financeiro
from regras import ErroValidacao

load_dotenv(Path(__file__).parent / '.env')


class RuralJSON(DefaultJSONProvider):
    @staticmethod
    def default(valor):
        if isinstance(valor, Decimal):
            return str(valor)
        if isinstance(valor, (date, datetime)):
            return valor.isoformat()
        return DefaultJSONProvider.default(valor)

app = Flask(__name__, static_folder='public/static', static_url_path='/static')
app.json = RuralJSON(app)
producao = os.getenv('APP_ENV') == 'production'
if producao and (not os.getenv('APP_PASSWORD') or not os.getenv('SECRET_KEY')):
    raise RuntimeError('Na hospedagem, configure APP_PASSWORD e SECRET_KEY antes de iniciar.')
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    DATABASE_URL=os.getenv('DATABASE_URL'),
    PDF_MAX_MB=4 if os.getenv('VERCEL') else 15,
    MAX_CONTENT_LENGTH=int(4.4 * 1024 * 1024) if os.getenv('VERCEL') else 16 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=producao,
    APP_PASSWORD=os.getenv('APP_PASSWORD', ''),
    APP_USERNAME=os.getenv('APP_USERNAME', 'admin'),
)
app.register_blueprint(financeiro)
registrar_comandos(app)


@app.context_processor
def tokens_template():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return {'csrf_token': session['csrf'], 'login_ativo': bool(app.config['APP_PASSWORD']),
            'pdf_max_mb': app.config['PDF_MAX_MB']}


@app.after_request
def proteger_cache(resposta):
    if request.endpoint != 'static':
        resposta.headers['Cache-Control'] = 'no-store'
    return resposta


@app.before_request
def proteger_acesso():
    if request.endpoint in ('login', 'static'):
        return None
    if app.config['APP_PASSWORD'] and not session.get('usuario'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Faça login para acessar o sistema.'}), 401
        return redirect(url_for('login'))
    if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        recebido = request.headers.get('X-CSRF-Token', request.form.get('csrf_token', ''))
        esperado = session.get('csrf', '')
        if not esperado or not secrets.compare_digest(recebido, esperado):
            return jsonify({'error': 'A sessão expirou. Recarregue a página e tente novamente.'}), 403


@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None
    if request.method == 'POST':
        token = request.form.get('csrf_token', '')
        if not session.get('csrf') or not secrets.compare_digest(token, session['csrf']):
            erro = 'Recarregue a página para renovar a sessão.'
        elif not app.config['APP_PASSWORD']:
            return redirect(url_for('index'))
        elif secrets.compare_digest(request.form.get('usuario', ''), app.config['APP_USERNAME']) and secrets.compare_digest(
            request.form.get('senha', ''), app.config['APP_PASSWORD']
        ):
            session.clear()
            session['usuario'] = app.config['APP_USERNAME']
            session['csrf'] = secrets.token_urlsafe(32)
            return redirect(url_for('index'))
        else:
            erro = 'Usuário ou senha incorretos.'
    return render_template('login.html', erro=erro)


@app.post('/sair')
def sair():
    session.clear()
    return redirect(url_for('login'))


@app.errorhandler(ErroValidacao)
def erro_validacao(erro):
    return jsonify({'error': str(erro)}), 400


@app.errorhandler(psycopg.errors.UniqueViolation)
def erro_duplicado(erro):
    return jsonify({'error': 'Registro já cadastrado (documento, classificação, nota ou PDF duplicado). Consulte os cadastros, inclusive os inativos.'}), 409


@app.errorhandler(psycopg.OperationalError)
def erro_conexao(erro):
    return jsonify({'error': 'Não foi possível conectar ao PostgreSQL. Confira a conexão no .env e se o banco está iniciado.'}), 503


@app.errorhandler(psycopg.Error)
def erro_banco(erro):
    # Registra apenas a classe do erro; dados financeiros e credenciais não vão ao log.
    app.logger.error('Falha no banco: %s', type(erro).__name__)
    return jsonify({'error': 'Não foi possível concluir a operação no banco. Execute flask init-db e confira os dados.'}), 500


@app.errorhandler(HTTPException)
def erro_http(erro):
    if request.path.startswith('/api/'):
        mensagem = f"Arquivo muito grande. Envie um PDF de até {app.config['PDF_MAX_MB']} MB." if erro.code == 413 else erro.description
        return jsonify({'error': mensagem}), erro.code
    return erro

# ---------------------------------------------------------------
# Configuração da IA (Gemini)
# ---------------------------------------------------------------
# A chave de API pode ser informada na tela ou pela variável GEMINI_API_KEY.
# Ela nunca é armazenada nos cadastros financeiros.

# Tenta o modelo principal; se estiver sobrecarregado (503) ou indisponível (404),
# tenta o próximo da lista.
GEMINI_MODELS = [m.strip() for m in os.getenv('GEMINI_MODELS', 'gemini-3.8-flash,gemini-3.5-flash').split(',') if m.strip()]

def gemini_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Categorias de despesa definidas no projeto (o Gemini classifica a
# nota fiscal em uma destas, com base nos produtos identificados).
CATEGORIAS_DESPESA = [
    "INSUMOS AGRÍCOLAS",
    "MANUTENÇÃO E OPERAÇÃO",
    "RECURSOS HUMANOS",
    "SERVIÇOS OPERACIONAIS",
    "INFRAESTRUTURA E UTILIDADES",
    "ADMINISTRATIVAS",
    "SEGUROS E PROTEÇÃO",
    "IMPOSTOS E TAXAS",
    "INVESTIMENTOS",
]

# Schema estruturado que força o Gemini a devolver exatamente os
# campos exigidos pelo projeto, no formato correto.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "numeroNotaFiscal": {"type": "string"},
        "dataEmissao": {"type": "string", "description": "Formato DD/MM/AAAA"},
        "fornecedor": {
            "type": "object",
            "properties": {
                "razaoSocial": {"type": "string"},
                "fantasia": {"type": "string"},
                "cnpj": {"type": "string"},
            },
            "required": ["razaoSocial", "cnpj"],
        },
        "faturado": {
            "type": "object",
            "properties": {
                "nomeCompleto": {"type": "string"},
                "cpf": {"type": "string"},
            },
        },
        "descricaoProdutos": {
            "type": "string",
            "description": "Resumo dos produtos/itens da nota",
        },
        "valorTotal": {"type": "number"},
        "classificacaoDespesa": {"type": "string", "enum": CATEGORIAS_DESPESA},
        "parcelas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "numero": {"type": "integer"},
                    "dataVencimento": {
                        "type": "string",
                        "description": "Formato DD/MM/AAAA",
                    },
                    "valor": {"type": "number"},
                },
            },
        },
    },
    "required": [
        "numeroNotaFiscal",
        "dataEmissao",
        "fornecedor",
        "valorTotal",
        "classificacaoDespesa",
        "parcelas",
    ],
}


def build_prompt() -> str:
    categorias_txt = "\n".join(f"  - {c}" for c in CATEGORIAS_DESPESA)
    return f"""Você é um sistema de extração de dados financeiros para uma propriedade rural.
Analise o PDF de nota fiscal (NF-e/DANFE) anexado e extraia os dados abaixo, retornando SOMENTE um JSON válido no formato definido pelo schema.

Regras importantes:
- "fornecedor" é a empresa/pessoa que EMITIU a nota (emitente).
- "faturado" é o destinatário/comprador da nota (cliente).
- "descricaoProdutos" deve ser um resumo textual curto dos itens/produtos da nota (não crie uma lista de objetos, apenas texto).
- "valorTotal" é o valor total da nota fiscal (número, sem "R$", use ponto como separador decimal).
- Para "parcelas": se a nota tiver apenas uma condição de pagamento/vencimento, gere um array com 1 item. Se houver várias parcelas/duplicatas, gere um item por parcela.
- "classificacaoDespesa": classifique a despesa em UMA das categorias a seguir, com base nos produtos/serviços da nota:
{categorias_txt}
- Se algum dado não estiver presente no documento, retorne string vazia "" (ou 0 para números), nunca invente valores."""


@app.route("/")
def index():
    return render_template("index.html")


@app.get('/cadastros/<nome>')
def cadastros(nome):
    titulos = {'pessoas': 'Pessoas', 'classificacoes': 'Classificações', 'contas': 'Contas'}
    if nome not in titulos:
        return 'Página não encontrada.', 404
    return render_template('cadastros.html', recurso=nome, titulo=titulos[nome])


@app.route("/api/extrair", methods=["POST"])
def extrair():
    api_key = request.form.get("apiKey", "").strip() or os.getenv('GEMINI_API_KEY', '')
    if not api_key:
        return jsonify({"error": "Informe sua chave de API do Gemini."}), 400

    if "pdf" not in request.files:
        return jsonify({"error": "Nenhum arquivo PDF foi enviado."}), 400

    arquivo = request.files["pdf"]
    pdf_bytes = arquivo.read()
    if not pdf_bytes:
        return jsonify({"error": "O arquivo enviado está vazio."}), 400
    if not pdf_bytes.startswith(b'%PDF-'):
        return jsonify({'error': 'O arquivo enviado não é um PDF válido.'}), 400
    if len(pdf_bytes) > app.config['PDF_MAX_MB'] * 1024 * 1024:
        return jsonify({'error': f"O PDF deve ter no máximo {app.config['PDF_MAX_MB']} MB."}), 413

    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": build_prompt()},
                    {"inlineData": {"mimeType": "application/pdf", "data": pdf_b64}},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    # Tenta cada modelo da lista, com repetição em caso de sobrecarga (503).
    ultimo_erro = None
    for modelo in GEMINI_MODELS:
        for tentativa in range(1, 3):  # até 2 tentativas por modelo
            try:
                resp = requests.post(
                    gemini_url(modelo), json=body, headers=headers, timeout=60
                )
            except requests.exceptions.RequestException:
                ultimo_erro = 'Falha de conexão com a API do Gemini. Tente novamente.'
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    partes = data['candidates'][0]['content']['parts']
                    texto_json = ''.join(p['text'] for p in partes if 'text' in p and not p.get('thought'))
                except (KeyError, IndexError, TypeError, ValueError):
                    ultimo_erro = "A IA não retornou nenhum dado."
                    continue

                try:
                    extraido = json.loads(texto_json)
                except json.JSONDecodeError:
                    ultimo_erro = "A resposta da IA não é um JSON válido."
                    continue
                if not isinstance(extraido, dict):
                    ultimo_erro = 'A IA retornou uma estrutura inválida. Tente novamente.'
                    continue

                return jsonify(extraido)

            if resp.status_code == 503:
                # Modelo sobrecarregado: espera um pouco e tenta de novo / próximo modelo.
                ultimo_erro = (
                    f"Modelo {modelo} sobrecarregado (503). Tentando alternativa..."
                )
                time.sleep(2)
                continue

            if resp.status_code == 404:
                # Modelo indisponível/descontinuado: pula direto para o próximo modelo.
                ultimo_erro = f"Modelo {modelo} indisponível (404). Tentando alternativa..."
                break

            # Outros erros (401, 400, 429 etc.) não valem a pena repetir.
            mensagens = {400: 'A API rejeitou a solicitação. Confira o modelo configurado e o PDF.',
                        401: 'Chave da API do Gemini inválida.', 403: 'A chave não tem acesso ao Gemini.',
                        429: 'Limite da API do Gemini atingido. Aguarde ou confira sua cota.'}
            return jsonify({'error': mensagens.get(resp.status_code, f'A API do Gemini retornou erro {resp.status_code}.')}), 502

    return jsonify({"error": ultimo_erro or "Falha desconhecida ao consultar o Gemini."}), 502


if __name__ == "__main__":
    app.run(host='127.0.0.1', debug=False, port=int(os.getenv('PORT', '5001')))
