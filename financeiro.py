"""Rotas da segunda etapa: consultas, cadastros e lançamento financeiro."""
import hashlib
import json
from decimal import Decimal
from datetime import date, datetime
from io import BytesIO

from flask import Blueprint, current_app, jsonify, request, send_file, session
from psycopg.types.json import Jsonb
from werkzeug.utils import secure_filename

from db import conectar
from regras import ErroValidacao, chave_descricao, documento, objeto, texto, validar_nota

financeiro = Blueprint('financeiro', __name__)


def serializar(valor):
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (date, datetime)):
        return valor.isoformat()
    raise TypeError('Valor não serializável.')


def auditar(conn, entidade, registro_id, operacao, dados=None):
    payload = json.loads(json.dumps(dados or {}, default=serializar))
    conn.execute('INSERT INTO auditoria (entidade, registro_id, operacao, usuario, dados) VALUES (%s,%s,%s,%s,%s)',
                 (entidade, registro_id, operacao, session.get('usuario', 'usuario-local'), Jsonb(payload)))


def procurar_pessoa(conn, tipo, doc, bloquear=False):
    return conn.execute('SELECT * FROM pessoas WHERE tipo=%s AND documento=%s' + (' FOR UPDATE' if bloquear else ''),
                        (tipo, doc)).fetchone()


def procurar_classificacao(conn, categoria, bloquear=False):
    return conn.execute('SELECT * FROM classificacao WHERE tipo=%s AND chave_descricao=%s' + (' FOR UPDATE' if bloquear else ''),
                        (categoria['tipo'], categoria['chave'])).fetchone()


def resultado_consulta(registro, nome, doc=None):
    return {'nome': nome, 'documento': doc, 'existe': registro is not None,
            'id': registro['id'] if registro else None, 'status': registro['status'] if registro else None,
            'nomeCadastrado': registro.get('razao_social', registro.get('descricao')) if registro else None,
            'mensagem': f"EXISTE - ID: {registro['id']} ({registro['status']})" if registro else 'NÃO EXISTE'}


@financeiro.post('/api/analisar')
def analisar():
    nota = validar_nota(request.get_json(silent=True))
    with conectar() as conn:
        f = procurar_pessoa(conn, 'CLIENTE-FORNECEDOR', nota['fornecedor']['cnpj'])
        r = procurar_pessoa(conn, 'FATURADO', nota['faturado']['cpf'])
        categorias = [resultado_consulta(procurar_classificacao(conn, c), c['descricao']) for c in nota['classificacoes']]
        fornecedor = resultado_consulta(f, nota['fornecedor']['razaoSocial'], nota['fornecedor']['cnpj'])
        faturado = resultado_consulta(r, nota['faturado']['nomeCompleto'], nota['faturado']['cpf'])
        duplicada = None
        if f:
            duplicada = conn.execute('SELECT id,status FROM movimentocontas WHERE tipo=%s AND pessoa_id=%s AND numero_nota_fiscal=%s',
                                     (nota['tipo'], f['id'], nota['numeroNotaFiscal'])).fetchone()
    inativos = [c for c in [fornecedor, faturado, *categorias] if c['status'] == 'INATIVO']
    return jsonify({'fornecedor': fornecedor, 'faturado': faturado, 'classificacoes': categorias,
                    'duplicada': duplicada, 'podeLancar': not inativos and not duplicada,
                    'orientacao': 'Reative os cadastros inativos antes de lançar.' if inativos else
                                 'Esta nota já está cadastrada. Consulte Contas.' if duplicada else
                                 'Os cadastros inexistentes serão criados junto com a conta.'})


def garantir_pessoa(conn, tipo, nome, doc, fantasia=''):
    registro = conn.execute('INSERT INTO pessoas(tipo,razao_social,fantasia,documento) VALUES (%s,%s,%s,%s) '
                            'ON CONFLICT(tipo,documento) DO NOTHING RETURNING *', (tipo, nome, fantasia, doc)).fetchone()
    if registro:
        auditar(conn, 'pessoas', registro['id'], 'CRIAR', registro)
    else:
        registro = procurar_pessoa(conn, tipo, doc, bloquear=True)
    if registro['status'] != 'ATIVO':
        raise ErroValidacao(f"{registro['razao_social']} está INATIVO. Reative o cadastro antes de lançar.")
    return registro['id']


def garantir_classificacao(conn, categoria):
    registro = conn.execute('INSERT INTO classificacao(tipo,descricao,chave_descricao) VALUES (%s,%s,%s) '
                            'ON CONFLICT(tipo,chave_descricao) DO NOTHING RETURNING *',
                            (categoria['tipo'], categoria['descricao'], categoria['chave'])).fetchone()
    if registro:
        auditar(conn, 'classificacao', registro['id'], 'CRIAR', registro)
    else:
        registro = procurar_classificacao(conn, categoria, bloquear=True)
    if registro['status'] != 'ATIVO':
        raise ErroValidacao(f"A classificação {registro['descricao']} está INATIVA. Reative-a antes de lançar.")
    return registro['id']


def gravar_parcelas(conn, movimento_id, parcelas, edicao=False):
    # Ao editar, as parcelas anteriores continuam no banco como INATIVAS.
    # Reutiliza números existentes e inativa as parcelas que deixaram de compor a conta.
    if edicao:
        conn.execute("UPDATE parcelacontas SET status='INATIVO' WHERE movimento_id=%s", (movimento_id,))
    for p in parcelas:
        identificacao = 'ÚNICA' if len(parcelas) == 1 else f"{p['numero']}/{len(parcelas)}"
        conn.execute('INSERT INTO parcelacontas(movimento_id,identificacao,numero,data_vencimento,valor) VALUES (%s,%s,%s,%s,%s) '
                     "ON CONFLICT(movimento_id,numero) DO UPDATE SET identificacao=EXCLUDED.identificacao, "
                     "data_vencimento=EXCLUDED.data_vencimento,valor=EXCLUDED.valor,status='ATIVO'",
                     (movimento_id, identificacao, p['numero'], p['dataVencimento'], p['valor']))


def validar_pdf(arquivo):
    if not arquivo or not arquivo.filename:
        return None
    conteudo = arquivo.read()
    if not conteudo.startswith(b'%PDF-'):
        raise ErroValidacao('O arquivo deve ser um PDF válido.')
    if len(conteudo) > current_app.config['PDF_MAX_MB'] * 1024 * 1024:
        raise ErroValidacao(f"O PDF deve ter no máximo {current_app.config['PDF_MAX_MB']} MB.")
    return (secure_filename(arquivo.filename)[:255] or 'nota-fiscal.pdf', conteudo)


def obter_dados():
    if request.is_json:
        return request.get_json(silent=True), None
    try:
        dados = json.loads(request.form.get('dados', ''))
    except (json.JSONDecodeError, TypeError):
        raise ErroValidacao('Os dados da nota devem estar em JSON válido.')
    return dados, validar_pdf(request.files.get('pdf'))


@financeiro.post('/api/lancar')
def lancar():
    dados, pdf = obter_dados()
    nota = validar_nota(dados)
    if request.form.get('origem') == 'extracao' and not pdf:
        raise ErroValidacao('Selecione novamente o PDF original para vinculá-lo ao lançamento.')
    with conectar() as conn:
        fornecedor_id = garantir_pessoa(conn, 'CLIENTE-FORNECEDOR', nota['fornecedor']['razaoSocial'],
                                         nota['fornecedor']['cnpj'], nota['fornecedor']['fantasia'])
        faturado_id = garantir_pessoa(conn, 'FATURADO', nota['faturado']['nomeCompleto'], nota['faturado']['cpf'])
        classificacoes_ids = [garantir_classificacao(conn, c) for c in nota['classificacoes']]
        registro = conn.execute('INSERT INTO movimentocontas(tipo,pessoa_id,faturado_id,numero_nota_fiscal,data_emissao,descricao_produtos,valor_total) '
                                'VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                                (nota['tipo'], fornecedor_id, faturado_id, nota['numeroNotaFiscal'], nota['dataEmissao'],
                                 nota['descricaoProdutos'], nota['valorTotal'])).fetchone()
        movimento_id = registro['id']
        for classificacao_id in classificacoes_ids:
            conn.execute('INSERT INTO movimento_classificacao VALUES (%s,%s)', (movimento_id, classificacao_id))
        gravar_parcelas(conn, movimento_id, nota['parcelas'])
        if pdf:
            conn.execute('INSERT INTO documentos(movimento_id,nome_arquivo,conteudo,sha256) VALUES (%s,%s,%s,%s)',
                         (movimento_id, pdf[0], pdf[1], hashlib.sha256(pdf[1]).hexdigest()))
        auditar(conn, 'movimentocontas', movimento_id, 'CRIAR', nota)
    return jsonify({'mensagem': 'Registro lançado com sucesso.', 'id': movimento_id,
                    'fornecedorId': fornecedor_id, 'faturadoId': faturado_id,
                    'classificacoesIds': classificacoes_ids, 'quantidadeParcelas': len(nota['parcelas'])}), 201


RECURSOS = {
    'pessoas': {'tabela': 'pessoas', 'colunas': ['id', 'tipo', 'razao_social', 'fantasia', 'documento', 'status'],
                'busca': ['razao_social', 'fantasia', 'documento', 'tipo'], 'ordem': 'razao_social'},
    'classificacoes': {'tabela': 'classificacao', 'colunas': ['id', 'tipo', 'descricao', 'status'],
                       'busca': ['descricao', 'tipo'], 'ordem': 'descricao'},
    'contas': {'tabela': 'movimentocontas', 'colunas': ['id', 'tipo', 'numero_nota_fiscal', 'data_emissao', 'valor_total', 'status'],
               'busca': ['numero_nota_fiscal', 'descricao_produtos', 'tipo'], 'ordem': 'data_emissao'},
}


def recurso(nome):
    if nome not in RECURSOS:
        raise ErroValidacao('Cadastro inválido.')
    return RECURSOS[nome]


def numero_parametro(chave, padrao, minimo, maximo):
    try:
        valor = int(request.args.get(chave, padrao))
    except ValueError:
        raise ErroValidacao(f'{chave}: informe um número inteiro.')
    if not minimo <= valor <= maximo:
        raise ErroValidacao(f'{chave}: informe um valor entre {minimo} e {maximo}.')
    return valor


@financeiro.get('/api/cadastros/<nome>')
def listar(nome):
    config = recurso(nome)
    busca = request.args.get('busca', '').strip()
    if not busca and request.args.get('todos') != '1':
        return jsonify({'registros': [], 'total': 0, 'pagina': 1})
    status = request.args.get('status', 'ATIVO')
    if status not in ('ATIVO', 'INATIVO', 'TODOS'):
        raise ErroValidacao('Filtro de situação inválido.')
    ordem = request.args.get('ordem', config['ordem'])
    permitidas = config['colunas'] + (['pessoa', 'faturado'] if nome == 'contas' else [])
    if ordem not in permitidas:
        raise ErroValidacao('Coluna de ordenação inválida.')
    direcao = request.args.get('direcao', 'asc').lower()
    if direcao not in ('asc', 'desc'):
        raise ErroValidacao('Direção de ordenação inválida.')
    pagina = numero_parametro('pagina', 1, 1, 100000)
    limite = numero_parametro('limite', 25, 1, 100)
    filtros, params = [], []
    if status != 'TODOS':
        filtros.append('t.status=%s')
        params.append(status)
    tipo = request.args.get('tipo', '')
    if tipo:
        filtros.append('t.tipo=%s')
        params.append(tipo)
    # Todos os termos são procurados, inclusive em colunas diferentes.
    campos_busca = ['t.' + c for c in config['busca']]
    origem = config['tabela'] + ' t'
    colunas = ','.join('t.' + c for c in config['colunas'])
    if nome == 'contas':
        origem += ' JOIN pessoas p ON p.id=t.pessoa_id JOIN pessoas f ON f.id=t.faturado_id'
        campos_busca += ['p.razao_social', 'p.documento', 'f.razao_social', 'f.documento']
        colunas += ',p.razao_social AS pessoa,f.razao_social AS faturado'
    coluna_ordem = {'pessoa': 'p.razao_social', 'faturado': 'f.razao_social'}.get(ordem, 't.' + ordem)
    if len(busca) > 200:
        raise ErroValidacao('A busca deve ter no máximo 200 caracteres.')
    for termo in busca.split():
        filtros.append('(' + ' OR '.join(c + ' ILIKE %s' for c in campos_busca) + ')')
        params.extend(['%' + termo + '%'] * len(campos_busca))
    where = ' WHERE ' + ' AND '.join(filtros) if filtros else ''
    with conectar() as conn:
        total = conn.execute('SELECT count(*) AS n FROM ' + origem + where, params).fetchone()['n']
        registros = conn.execute('SELECT ' + colunas + ' FROM ' + origem + where +
                                  f' ORDER BY {coluna_ordem} {direcao},t.id LIMIT %s OFFSET %s',
                                  [*params, limite, (pagina - 1) * limite]).fetchall()
    return jsonify({'registros': registros, 'total': total, 'pagina': pagina, 'limite': limite})


@financeiro.get('/api/cadastros/<nome>/<int:registro_id>')
def consultar(nome, registro_id):
    config = recurso(nome)
    with conectar() as conn:
        registro = conn.execute(f"SELECT * FROM {config['tabela']} WHERE id=%s", (registro_id,)).fetchone()
        if not registro:
            return jsonify({'error': 'Registro não encontrado.'}), 404
        if nome == 'contas':
            registro['fornecedor'] = conn.execute('SELECT razao_social AS "razaoSocial",fantasia,documento AS cnpj FROM pessoas WHERE id=%s',
                                                  (registro['pessoa_id'],)).fetchone()
            registro['faturado'] = conn.execute('SELECT razao_social AS "nomeCompleto",documento AS cpf FROM pessoas WHERE id=%s',
                                                (registro['faturado_id'],)).fetchone()
            registro['classificacoes'] = [r['descricao'] for r in conn.execute('SELECT c.descricao FROM classificacao c '
                'JOIN movimento_classificacao mc ON mc.classificacao_id=c.id WHERE mc.movimento_id=%s ORDER BY c.id', (registro_id,))]
            registro['parcelas'] = conn.execute('SELECT numero,data_vencimento AS "dataVencimento",valor FROM parcelacontas '
                "WHERE movimento_id=%s AND status='ATIVO' ORDER BY numero", (registro_id,)).fetchall()
            registro['documentoId'] = conn.execute('SELECT id,nome_arquivo FROM documentos WHERE movimento_id=%s', (registro_id,)).fetchone()
    return jsonify(registro)


def validar_cadastro(nome, dados):
    dados = objeto(dados, 'Cadastro')
    if nome == 'pessoas':
        tipo = dados.get('tipo')
        if tipo not in ('CLIENTE-FORNECEDOR', 'FATURADO'):
            raise ErroValidacao('Tipo da pessoa inválido.')
        return {'tipo': tipo, 'razao_social': texto(dados.get('razao_social', ''), 'Nome / razão social'),
                'fantasia': texto(dados.get('fantasia', ''), 'Nome fantasia', obrigatorio=False),
                'documento': documento(dados.get('documento', ''), 'CPF/CNPJ', cpf=tipo == 'FATURADO')}
    tipo = dados.get('tipo')
    if tipo not in ('DESPESA', 'RECEITA'):
        raise ErroValidacao('Tipo da classificação inválido.')
    descricao = texto(dados.get('descricao', ''), 'Descrição', 120)
    return {'tipo': tipo, 'descricao': descricao, 'chave_descricao': chave_descricao(descricao)}


@financeiro.post('/api/cadastros/<nome>')
def criar(nome):
    config = recurso(nome)
    if nome == 'contas':
        return lancar()
    dados = validar_cadastro(nome, request.get_json(silent=True))
    with conectar() as conn:
        colunas = ','.join(dados)
        campos = ','.join(['%s'] * len(dados))
        registro = conn.execute(f"INSERT INTO {config['tabela']}({colunas}) VALUES ({campos}) RETURNING *", list(dados.values())).fetchone()
        auditar(conn, config['tabela'], registro['id'], 'CRIAR', registro)
    return jsonify(registro), 201


@financeiro.put('/api/cadastros/<nome>/<int:registro_id>')
def editar(nome, registro_id):
    config = recurso(nome)
    dados = request.get_json(silent=True)
    validados = validar_nota(dados) if nome == 'contas' else validar_cadastro(nome, dados)
    with conectar() as conn:
        anterior = conn.execute(f"SELECT * FROM {config['tabela']} WHERE id=%s FOR UPDATE", (registro_id,)).fetchone()
        if not anterior:
            return jsonify({'error': 'Registro não encontrado.'}), 404
        if nome == 'contas':
            if anterior['status'] != 'ATIVO':
                raise ErroValidacao('Reative a conta antes de editar.')
            nota = validados
            pessoa = garantir_pessoa(conn, 'CLIENTE-FORNECEDOR', nota['fornecedor']['razaoSocial'], nota['fornecedor']['cnpj'], nota['fornecedor']['fantasia'])
            faturado = garantir_pessoa(conn, 'FATURADO', nota['faturado']['nomeCompleto'], nota['faturado']['cpf'])
            categorias = [garantir_classificacao(conn, c) for c in nota['classificacoes']]
            conn.execute('UPDATE movimentocontas SET tipo=%s,pessoa_id=%s,faturado_id=%s,numero_nota_fiscal=%s,data_emissao=%s,'
                         'descricao_produtos=%s,valor_total=%s,atualizado_em=now() WHERE id=%s',
                         (nota['tipo'], pessoa, faturado, nota['numeroNotaFiscal'], nota['dataEmissao'], nota['descricaoProdutos'], nota['valorTotal'], registro_id))
            conn.execute('DELETE FROM movimento_classificacao WHERE movimento_id=%s', (registro_id,))
            for categoria in categorias:
                conn.execute('INSERT INTO movimento_classificacao VALUES (%s,%s)', (registro_id, categoria))
            gravar_parcelas(conn, registro_id, nota['parcelas'], edicao=True)
        else:
            if anterior['tipo'] != validados['tipo']:
                if nome == 'pessoas':
                    vinculo = conn.execute('SELECT id FROM movimentocontas WHERE pessoa_id=%s OR faturado_id=%s LIMIT 1', (registro_id, registro_id)).fetchone()
                else:
                    vinculo = conn.execute('SELECT movimento_id FROM movimento_classificacao WHERE classificacao_id=%s LIMIT 1', (registro_id,)).fetchone()
                if vinculo:
                    raise ErroValidacao('O tipo não pode ser alterado porque o cadastro está vinculado a uma conta.')
            atribuicoes = ','.join(c + '=%s' for c in validados)
            conn.execute(f"UPDATE {config['tabela']} SET {atribuicoes},atualizado_em=now() WHERE id=%s", [*validados.values(), registro_id])
        auditar(conn, config['tabela'], registro_id, 'EDITAR', {'antes': anterior, 'depois': validados})
    return jsonify({'mensagem': 'Registro atualizado com sucesso.', 'id': registro_id})


def alterar_status(nome, registro_id, novo_status):
    config = recurso(nome)
    with conectar() as conn:
        registro = conn.execute(f"SELECT * FROM {config['tabela']} WHERE id=%s FOR UPDATE", (registro_id,)).fetchone()
        if not registro:
            return jsonify({'error': 'Registro não encontrado.'}), 404
        if nome == 'contas' and novo_status == 'ATIVO':
            pessoas = conn.execute('SELECT id FROM pessoas WHERE id IN (%s,%s) AND status=%s FOR UPDATE',
                                   (registro['pessoa_id'], registro['faturado_id'], 'INATIVO')).fetchall()
            categorias = conn.execute("SELECT c.id FROM classificacao c JOIN movimento_classificacao mc ON mc.classificacao_id=c.id "
                                       "WHERE mc.movimento_id=%s AND c.status='INATIVO' FOR UPDATE OF c", (registro_id,)).fetchall()
            if pessoas or categorias:
                raise ErroValidacao('Reative as pessoas e classificações vinculadas antes de reativar a conta.')
        conn.execute(f"UPDATE {config['tabela']} SET status=%s,atualizado_em=now() WHERE id=%s", (novo_status, registro_id))
        if registro['status'] != novo_status:
            auditar(conn, config['tabela'], registro_id, 'REATIVAR' if novo_status == 'ATIVO' else 'INATIVAR', {'antes': registro['status'], 'depois': novo_status})
    return jsonify({'mensagem': 'Registro reativado.' if novo_status == 'ATIVO' else 'Registro inativado. Histórico preservado.', 'id': registro_id})


@financeiro.delete('/api/cadastros/<nome>/<int:registro_id>')
def inativar(nome, registro_id):
    return alterar_status(nome, registro_id, 'INATIVO')


@financeiro.post('/api/cadastros/<nome>/<int:registro_id>/reativar')
def reativar(nome, registro_id):
    return alterar_status(nome, registro_id, 'ATIVO')


@financeiro.get('/api/contas/<int:registro_id>/documento')
def baixar_documento(registro_id):
    with conectar() as conn:
        doc = conn.execute('SELECT nome_arquivo,conteudo FROM documentos WHERE movimento_id=%s', (registro_id,)).fetchone()
    if not doc:
        return jsonify({'error': 'Esta conta não possui PDF vinculado.'}), 404
    return send_file(BytesIO(bytes(doc['conteudo'])), mimetype='application/pdf', download_name=doc['nome_arquivo'], as_attachment=True)


@financeiro.get('/api/saude')
def saude():
    with conectar() as conn:
        conn.execute('SELECT id FROM pessoas LIMIT 1')
    return jsonify({'status': 'ok', 'banco': 'PostgreSQL'})
