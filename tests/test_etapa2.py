from copy import deepcopy
from io import BytesIO
import json
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app import conectar


def test_versao_mostra_commit_publicado_e_data_em_brasilia(monkeypatch):
    from app import app, informacoes_versao
    codigo = 'a' * 40
    monkeypatch.setenv('APP_COMMIT_SHA', codigo)
    monkeypatch.setenv('APP_COMMIT_DATE', '2026-10-19T01:30:00+00:00')
    monkeypatch.setitem(app.config, 'APP_PASSWORD', '')
    informacoes_versao.cache_clear()
    try:
        versao = informacoes_versao()
        assert versao['codigo'] == 'aaaaaaa'
        assert versao['atualizado'] == '18/10/2026 às 22:30'
        assert versao['commit_url'].endswith('/commit/' + codigo)
        cliente = app.test_client()
        for rota in ('/', '/cadastros/pessoas', '/cadastros/classificacoes', '/cadastros/contas'):
            html = cliente.get(rota).get_data(as_text=True)
            assert html.count('class="release-info"') == 1
            assert 'aria-label="Versão do sistema"' in html
            assert '18/10/2026 às 22:30' in html and versao['commit_url'] in html
        assert 'class="release-info"' not in cliente.get('/login').get_data(as_text=True)
    finally:
        informacoes_versao.cache_clear()


def test_versao_sem_metadados_nao_inventa_data(monkeypatch):
    from app import informacoes_versao
    monkeypatch.setenv('VERCEL', '1')
    monkeypatch.delenv('APP_COMMIT_SHA', raising=False)
    monkeypatch.delenv('APP_COMMIT_DATE', raising=False)
    monkeypatch.delenv('VERCEL_GIT_COMMIT_SHA', raising=False)
    informacoes_versao.cache_clear()
    try:
        versao = informacoes_versao()
        assert versao['codigo'] == 'local' and versao['commit_url'] is None
        assert versao['atualizado_iso'] is None and versao['atualizado'] == 'Não informada'
    finally:
        informacoes_versao.cache_clear()


def quantidade(app, tabela):
    with app.app_context(), conectar() as conn:
        return conn.execute(f'SELECT count(*) AS n FROM {tabela}').fetchone()['n']


def test_consulta_nao_cria_e_lancamento_reutiliza(cliente, nota, ambiente):
    consulta = cliente.post('/api/analisar', json=nota)
    assert consulta.status_code == 200
    assert consulta.json['fornecedor']['mensagem'] == 'NÃO EXISTE'
    assert consulta.json['podeLancar']
    assert quantidade(ambiente, 'pessoas') == 0
    registro = cliente.post('/api/lancar', json=nota)
    assert registro.status_code == 201
    assert quantidade(ambiente, 'pessoas') == 2
    assert quantidade(ambiente, 'parcelacontas') == 1
    assert quantidade(ambiente, 'auditoria') == 4
    consulta = cliente.post('/api/analisar', json=nota).json
    assert consulta['fornecedor']['existe']
    assert consulta['duplicada']['id'] == registro.json['id']
    assert not consulta['podeLancar']
    outra = deepcopy(nota)
    outra['numeroNotaFiscal'] = 'EXEMPLO-002'
    outra['fornecedor']['cnpj'] = '11111111000100'
    outra['classificacaoDespesa'] = 'manutencao e operacao'
    assert cliente.post('/api/lancar', json=outra).status_code == 201
    assert quantidade(ambiente, 'pessoas') == 2
    assert quantidade(ambiente, 'classificacao') == 1


def test_multiplas_parcelas_e_classificacoes(cliente, nota, ambiente):
    nota['classificacoes'] = ['MANUTENÇÃO E OPERAÇÃO', 'INVESTIMENTOS']
    nota['parcelas'] = [{'valor':'625.00','dataVencimento':'27/10/2026'}, {'valor':'625.00','dataVencimento':'27/11/2026'}]
    resposta = cliente.post('/api/lancar', json=nota)
    assert resposta.status_code == 201
    assert quantidade(ambiente, 'movimento_classificacao') == 2
    conta = cliente.get(f"/api/cadastros/contas/{resposta.json['id']}").json
    assert len(conta['parcelas']) == 2
    assert conta['valor_total'] == '1250.00'


@pytest.mark.parametrize('alteracao', ['soma','data','documento','vencimentos','categoria','centavos','zero','estrutura'])
def test_dados_inconsistentes_nao_gravam(cliente, nota, ambiente, alteracao):
    if alteracao == 'soma': nota['parcelas'][0]['valor'] = 1200
    if alteracao == 'data': nota['dataEmissao'] = '31/02/2026'
    if alteracao == 'documento': nota['faturado']['cpf'] = ''
    if alteracao == 'vencimentos': nota['parcelas'] *= 2
    if alteracao == 'categoria': nota['classificacoes'] = ['INVESTIMENTOS','investimentos']
    if alteracao == 'centavos': nota['valorTotal'] = '1250.001'
    if alteracao == 'zero': nota['valorTotal'] = 0
    if alteracao == 'estrutura': nota['fornecedor'] = []
    assert cliente.post('/api/lancar', json=nota).status_code == 400
    assert quantidade(ambiente, 'pessoas') == 0
    assert quantidade(ambiente, 'movimentocontas') == 0


def test_duplicata_desfaz_novas_classificacoes(cliente, nota, ambiente):
    assert cliente.post('/api/lancar', json=nota).status_code == 201
    nota['classificacaoDespesa'] = 'NOVA CATEGORIA'
    assert cliente.post('/api/lancar', json=nota).status_code == 409
    assert quantidade(ambiente, 'classificacao') == 1
    assert quantidade(ambiente, 'movimentocontas') == 1


def test_inativacao_reativacao_e_bloqueio(cliente, nota, ambiente):
    registro = cliente.post('/api/lancar', json=nota).json
    pessoa = registro['fornecedorId']
    assert cliente.delete(f'/api/cadastros/pessoas/{pessoa}').status_code == 200
    nota['numeroNotaFiscal'] = 'EXEMPLO-002'
    assert not cliente.post('/api/analisar', json=nota).json['podeLancar']
    assert cliente.post('/api/lancar', json=nota).status_code == 400
    assert quantidade(ambiente, 'movimentocontas') == 1
    assert cliente.post(f'/api/cadastros/pessoas/{pessoa}/reativar').status_code == 200
    assert cliente.post('/api/lancar', json=nota).status_code == 201
    conta = registro['id']
    assert cliente.delete(f'/api/cadastros/contas/{conta}').status_code == 200
    assert quantidade(ambiente, 'parcelacontas') == 2
    assert cliente.post(f'/api/cadastros/contas/{conta}/reativar').status_code == 200


def test_nao_exclui_fisicamente(cliente, nota, ambiente):
    cliente.post('/api/lancar', json=nota)
    with pytest.raises(psycopg.errors.RaiseException), ambiente.app_context(), conectar() as conn:
        conn.execute('DELETE FROM pessoas')
    assert quantidade(ambiente, 'pessoas') == 2


def test_busca_ordenacao_e_tabela_vazia(cliente, nota):
    cliente.post('/api/lancar', json=nota)
    assert cliente.get('/api/cadastros/pessoas').json['registros'] == []
    resultado = cliente.get('/api/cadastros/pessoas?busca=IGUAÇU+11111111').json
    assert resultado['total'] == 1
    assert cliente.get('/api/cadastros/contas?busca=BELTRANO+EXEMPLO').json['total'] == 1
    assert cliente.get('/api/cadastros/pessoas?todos=1&ordem=id&direcao=desc&limite=1').json['total'] == 2
    assert cliente.get('/api/cadastros/pessoas?todos=1&ordem=DROP').status_code == 400
    assert cliente.get('/api/cadastros/pessoas?todos=1&pagina=abc').status_code == 400


def test_edicao_preserva_historico_de_parcelas(cliente, nota, ambiente):
    nota['parcelas'] = [{'valor':625,'dataVencimento':'27/10/2026'}, {'valor':625,'dataVencimento':'27/11/2026'}]
    id = cliente.post('/api/lancar', json=nota).json['id']
    nota['parcelas'] = [{'valor':1250,'dataVencimento':'27/10/2026'}]
    assert cliente.put(f'/api/cadastros/contas/{id}', json=nota).status_code == 200
    assert len(cliente.get(f'/api/cadastros/contas/{id}').json['parcelas']) == 1
    assert quantidade(ambiente, 'parcelacontas') == 2
    assert quantidade(ambiente, 'movimentocontas') == 1


def test_receita_e_tipo_vinculado(cliente, nota):
    nota['tipo'] = 'ARECEBER'
    nota['classificacoes'] = ['VENDA DE PRODUÇÃO']
    registro = cliente.post('/api/lancar', json=nota)
    assert registro.status_code == 201
    classificacao_id = registro.json['classificacoesIds'][0]
    assert cliente.get(f'/api/cadastros/classificacoes/{classificacao_id}').json['tipo'] == 'RECEITA'
    assert cliente.put(f'/api/cadastros/classificacoes/{classificacao_id}', json={'tipo':'DESPESA','descricao':'VENDA DE PRODUÇÃO'}).status_code == 400


def test_pdf_vinculado_e_duplicado_atomico(cliente, nota, ambiente):
    pdf = b'%PDF-1.4\n%%EOF'
    registro = cliente.post('/api/lancar', data={'dados':json.dumps(nota),'origem':'extracao','pdf':(BytesIO(pdf),'nota.pdf')})
    assert registro.status_code == 201
    baixado = cliente.get(f"/api/contas/{registro.json['id']}/documento")
    assert baixado.data == pdf
    nota['numeroNotaFiscal'] = 'OUTRA'
    nota['fornecedor']['cnpj'] = '22222222000100'
    duplicado = cliente.post('/api/lancar', data={'dados':json.dumps(nota),'pdf':(BytesIO(pdf),'outra.pdf')})
    assert duplicado.status_code == 409
    assert quantidade(ambiente, 'pessoas') == 2


def test_crud_status_nao_pode_ser_injetado(cliente):
    registro = cliente.post('/api/cadastros/classificacoes', json={'tipo':'DESPESA','descricao':'Teste','status':'INATIVO'}).json
    assert registro['status'] == 'ATIVO'
    id = registro['id']
    assert cliente.put(f'/api/cadastros/classificacoes/{id}',json={'tipo':'DESPESA','descricao':'Renomeada','status':'INATIVO'}).status_code == 200
    assert cliente.get(f'/api/cadastros/classificacoes/{id}').json['status'] == 'ATIVO'


def test_csrf_e_login(cliente, ambiente, monkeypatch):
    sem_token = ambiente.test_client()
    assert sem_token.post('/api/lancar',json={}).status_code == 403
    monkeypatch.setitem(ambiente.config,'APP_PASSWORD','senha-teste')
    monkeypatch.setitem(ambiente.config,'APP_USERNAME','admin')
    assert cliente.get('/api/cadastros/pessoas?todos=1').status_code == 401
    html = cliente.get('/login').get_data(as_text=True)
    assert 'class="header-user"' not in html
    token = re.search(r'name="csrf_token" value="([^"]+)"',html).group(1)
    assert cliente.post('/login',data={'usuario':'admin','senha':'senha-teste','csrf_token':token}).status_code == 302
    assert cliente.get('/api/saude').status_code == 200
    for rota in ('/', '/cadastros/pessoas', '/cadastros/classificacoes', '/cadastros/contas'):
        html = cliente.get(rota).get_data(as_text=True)
        assert 'class="header-user"' in html and '<strong>admin</strong>' in html
    with cliente.session_transaction() as sess:
        token = sess['csrf']
    assert cliente.post('/sair', data={'csrf_token': token}, headers={'X-CSRF-Token': token}).status_code == 302
    assert 'class="header-user"' not in cliente.get('/login').get_data(as_text=True)


def test_lancamentos_concorrentes_nao_duplicam(ambiente, nota):
    def enviar(_):
        with ambiente.test_client() as client:
            client.get('/')
            with client.session_transaction() as sess: token=sess['csrf']
            return client.post('/api/lancar',json=nota,headers={'X-CSRF-Token':token}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        respostas=list(pool.map(enviar,range(2)))
    assert sorted(respostas) == [201,409]
    assert quantidade(ambiente,'pessoas') == 2
    assert quantidade(ambiente,'movimentocontas') == 1


def test_extracao_preservada_sem_chamar_google(cliente, nota, monkeypatch):
    import app as modulo
    class Resposta:
        status_code=200
        def json(self):return {'candidates':[{'content':{'parts':[{'text':json.dumps(nota)}]}}]}
    monkeypatch.setattr(modulo.requests,'post',lambda *a,**k:Resposta())
    resposta=cliente.post('/api/extrair',data={'apiKey':'teste','pdf':(BytesIO(b'%PDF-1.4\n%%EOF'),'nota.pdf')})
    assert resposta.status_code==200
    assert {k:v for k,v in resposta.json.items() if k!='_ruralfin'}==nota
    assert resposta.json['_ruralfin']['pdfSha256']
    assert resposta.headers['Cache-Control']=='no-store'


def test_limite_pdf_antes_da_chamada_gemini(cliente, ambiente, monkeypatch):
    monkeypatch.setitem(ambiente.config,'PDF_MAX_MB',1)
    resposta=cliente.post('/api/extrair',data={'apiKey':'teste','pdf':(BytesIO(b'%PDF-'+b'0'*(1024*1024)),'grande.pdf')})
    assert resposta.status_code==413
    assert '1 MB' in resposta.json['error']


def test_extracao_usa_alternativa_apos_sobrecarga(cliente, nota, monkeypatch):
    import app as modulo
    chamadas, esperas = [], []
    class Resposta:
        def __init__(self, status): self.status_code = status
        def json(self): return {'candidates': [{'content': {'parts': [{'text': json.dumps(nota)}]}}]}
    def enviar(url, **kwargs):
        chamadas.append(url)
        return Resposta(503 if len(chamadas) <= 2 else 200)
    monkeypatch.setattr(modulo, 'GEMINI_MODELS', ['principal', 'alternativo'])
    monkeypatch.setattr(modulo.requests, 'post', enviar)
    monkeypatch.setattr(modulo.time, 'sleep', esperas.append)
    resposta = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert resposta.status_code == 200 and {k:v for k,v in resposta.json.items() if k!='_ruralfin'} == nota
    assert len(chamadas) == 3 and 'alternativo:generateContent' in chamadas[-1]
    assert len(esperas) == 1 and 2 <= esperas[0] < 3


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
def test_extracao_encerra_tentativas_com_aviso_final(cliente, monkeypatch, status):
    import app as modulo
    chamadas, esperas = [], []
    class Resposta: status_code = status
    def enviar(url, **kwargs):
        chamadas.append(url)
        return Resposta()
    monkeypatch.setattr(modulo, 'GEMINI_MODELS', ['principal', 'alternativo'])
    monkeypatch.setattr(modulo.requests, 'post', enviar)
    monkeypatch.setattr(modulo.time, 'sleep', esperas.append)
    resposta = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert resposta.status_code == (429 if status == 429 else 503)
    assert 'Tentando alternativa' not in resposta.json['error']
    assert len(chamadas) == 4 and len(esperas) == 2


def test_extracao_nao_repete_erro_de_chave(cliente, monkeypatch):
    import app as modulo
    chamadas = []
    class Resposta: status_code = 403
    def enviar(url, **kwargs):
        chamadas.append(url)
        return Resposta()
    monkeypatch.setattr(modulo.requests, 'post', enviar)
    resposta = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert resposta.status_code == 502 and len(chamadas) == 1
    assert 'chave' in resposta.json['error']


@pytest.mark.parametrize('campo,valor,esperado', [
    ('cpfCnpj', '999.999.999-99', '99999999999'),
    ('cpfCnpj', '22.222.222/0001-00', '22222222000100'),
    ('cpf', '999.999.999-99', '99999999999'),
    ('cnpj', '22.222.222/0001-00', '22222222000100'),
])
def test_faturado_cpf_cnpj_consulta_gravacao_edicao(cliente, nota, ambiente, campo, valor, esperado):
    nota['faturado'] = {'nomeCompleto': 'DESTINATÁRIO DE TESTE', campo: valor}
    consulta = cliente.post('/api/analisar', json=nota)
    assert consulta.status_code == 200 and consulta.json['podeLancar']
    assert consulta.json['faturado']['documento'] == esperado
    registro = cliente.post('/api/lancar', json=nota)
    assert registro.status_code == 201
    id = registro.json['id']
    reaberta = cliente.get(f'/api/cadastros/contas/{id}').json
    assert reaberta['faturado']['cpfCnpj'] == esperado
    assert cliente.put(f'/api/cadastros/contas/{id}', json=nota).status_code == 200
    consulta = cliente.post('/api/analisar', json=nota).json
    assert consulta['faturado']['existe']
    assert quantidade(ambiente, 'pessoas') == 2


def test_cadastro_faturado_aceita_cnpj_e_recusa_documento_invalido(cliente):
    cadastro = {'tipo': 'FATURADO', 'razao_social': 'EMPRESA FICTÍCIA', 'documento': '22.222.222/0001-00'}
    resposta = cliente.post('/api/cadastros/pessoas', json=cadastro)
    assert resposta.status_code == 201 and resposta.json['documento'] == '22222222000100'
    cadastro['documento'] = '123456789012'
    assert cliente.put(f"/api/cadastros/pessoas/{resposta.json['id']}", json=cadastro).status_code == 400


def test_migracao_faturado_preserva_registros_e_aceita_cnpj(ambiente):
    from app import inicializar
    with ambiente.app_context(), conectar() as conn:
        conn.execute("ALTER TABLE pessoas ADD CONSTRAINT pessoas_check CHECK (tipo <> 'FATURADO' OR length(documento) = 11)")
        id = conn.execute("INSERT INTO pessoas(tipo,razao_social,documento) VALUES ('FATURADO','CPF ANTERIOR','99999999999') RETURNING id").fetchone()['id']
    with ambiente.app_context():
        inicializar()
        inicializar()
        with conectar() as conn:
            assert conn.execute('SELECT documento FROM pessoas WHERE id=%s', (id,)).fetchone()['documento'] == '99999999999'
            conn.execute("INSERT INTO pessoas(tipo,razao_social,documento) VALUES ('FATURADO','CNPJ NOVO','22222222000100')")
    assert quantidade(ambiente, 'pessoas') == 2


def test_extracao_cnpj_destinatario_chega_ao_lancamento(cliente, nota, monkeypatch):
    import app as modulo
    nota['faturado'] = {'nomeCompleto': 'DESTINATÁRIO EMPRESA FICTÍCIA', 'cpfCnpj': '22.222.222/0001-00'}
    class Resposta:
        status_code = 200
        def json(self): return {'candidates': [{'content': {'parts': [{'text': json.dumps(nota)}]}}]}
    def enviar(url, **kwargs):
        schema = kwargs['json']['generationConfig']['responseSchema']
        assert 'faturado' in schema['required']
        assert 'produtos' in schema['required']
        assert 'cpfCnpj' in schema['properties']['faturado']['required']
        assert 'DESTINATÁRIO/REMETENTE' in kwargs['json']['contents'][0]['parts'][0]['text']
        return Resposta()
    monkeypatch.setattr(modulo.requests, 'post', enviar)
    extraida = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert extraida.status_code == 200
    assert cliente.post('/api/analisar', json=extraida.json).json['podeLancar']
    assert cliente.post('/api/lancar', json=extraida.json).status_code == 201


def test_lista_produtos_preserva_itens_na_gravacao_e_edicao(cliente, nota):
    nota['produtos'] = ['Kit hidráulico, com conexões', 'Filtro de óleo']
    nota['descricaoProdutos'] = 'Texto anterior'
    r = cliente.post('/api/lancar', json=nota)
    assert r.status_code == 201
    url = f"/api/cadastros/contas/{r.json['id']}"
    conta = cliente.get(url).json
    assert conta['produtos'] == nota['produtos']
    assert conta['descricao_produtos'] == 'Kit hidráulico, com conexões\nFiltro de óleo'
    nota['produtos'] = ['Filtro de óleo']
    assert cliente.put(url, json=nota).status_code == 200
    assert cliente.get(url).json['produtos'] == ['Filtro de óleo']


def test_json_salvo_recupera_sem_gemini_e_vincula_pdf(cliente, nota, monkeypatch):
    import app as modulo
    def proibido(*a, **k): raise AssertionError('Recuperação não deve chamar o Gemini.')
    monkeypatch.setattr(modulo.requests, 'post', proibido)
    pdf = b'%PDF-1.4\nEXEMPLO-FICTICIO\n%%EOF'
    nota['_ruralfin'] = {'pdfSha256': hashlib.sha256(pdf).hexdigest(), 'origem': 'gemini'}
    r = cliente.post('/api/revisar-json', data={'dados': json.dumps(nota), 'pdf': (BytesIO(pdf), 'nota.pdf')})
    assert r.status_code == 200 and r.json['_ruralfin']['origem'] == 'json_salvo'
    assert cliente.post('/api/analisar', json=r.json).json['podeLancar']
    registro = cliente.post('/api/lancar', data={'dados': json.dumps(r.json), 'origem': 'json_salvo', 'pdf': (BytesIO(pdf), 'nota.pdf')})
    assert registro.status_code == 201
    assert cliente.get(f"/api/contas/{registro.json['id']}/documento").data == pdf


@pytest.mark.parametrize('erro', ['pdf_diferente', 'sem_pdf', 'sem_vinculo', 'json_invalido', 'estrutura_invalida'])
def test_json_salvo_recusa_arquivo_incorreto_sem_gravar(cliente, nota, ambiente, erro):
    pdf = b'%PDF-1.4\n%%EOF'
    nota['_ruralfin'] = {'pdfSha256': hashlib.sha256(pdf).hexdigest()}
    if erro == 'sem_vinculo': nota.pop('_ruralfin')
    bruto = '{invalido' if erro == 'json_invalido' else '{}' if erro == 'estrutura_invalida' else json.dumps(nota)
    dados = {'dados': bruto}
    if erro != 'sem_pdf': dados['pdf'] = (BytesIO(pdf + (b'outro' if erro == 'pdf_diferente' else b'')), 'nota.pdf')
    assert cliente.post('/api/revisar-json', data=dados).status_code == 400
    assert quantidade(ambiente, 'movimentocontas') == 0


def test_json_salvo_exige_pdf_no_lancamento(cliente, nota):
    assert cliente.post('/api/lancar', data={'dados': json.dumps(nota), 'origem': 'json_salvo'}).status_code == 400


def test_extracao_limita_espera_apos_timeout(cliente, monkeypatch):
    import app as modulo
    from types import SimpleNamespace
    tempos = iter([0, 0, 85])
    chamadas = []
    def enviar(*a, **k):
        chamadas.append(k['timeout'])
        raise modulo.requests.exceptions.Timeout()
    monkeypatch.setattr(modulo, 'time', SimpleNamespace(monotonic=lambda: next(tempos), sleep=lambda t: None))
    monkeypatch.setattr(modulo.requests, 'post', enviar)
    r = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert r.status_code == 504 and 'JSON' in r.json['error']
    assert chamadas == [(5, 25)]


@pytest.mark.parametrize('conteudo', ['{}', '[]', '{incompleto', '{"numeroNotaFiscal":55}'])
def test_extracao_rejeita_resposta_fora_da_estrutura(cliente, monkeypatch, conteudo):
    import app as modulo
    class Resposta:
        status_code = 200
        def json(self): return {'candidates': [{'content': {'parts': [{'text': conteudo}]}}]}
    monkeypatch.setattr(modulo.requests, 'post', lambda *a, **k: Resposta())
    monkeypatch.setattr(modulo.time, 'sleep', lambda t: None)
    r = cliente.post('/api/extrair', data={'apiKey': 'teste', 'pdf': (BytesIO(b'%PDF-1.4\n%%EOF'), 'nota.pdf')})
    assert r.status_code == 502 and 'error' in r.json


def test_id_ordena_ascendente_e_descendente_em_todos_os_cadastros(cliente, nota):
    cliente.post('/api/lancar', json=nota)
    nota['numeroNotaFiscal'] = 'OUTRA-NOTA'
    nota['classificacaoDespesa'] = 'INVESTIMENTOS'
    nota['dataEmissao'] = '13/10/2026'
    cliente.post('/api/lancar', json=nota)
    for recurso in ('pessoas', 'classificacoes', 'contas'):
        crescente = cliente.get(f'/api/cadastros/{recurso}?todos=1&ordem=id&direcao=asc').json['registros']
        decrescente = cliente.get(f'/api/cadastros/{recurso}?todos=1&ordem=id&direcao=desc').json['registros']
        assert [r['id'] for r in crescente] == sorted(r['id'] for r in crescente)
        assert [r['id'] for r in decrescente] == list(reversed([r['id'] for r in crescente]))
        padrao = cliente.get(f'/api/cadastros/{recurso}?todos=1').json['registros']
        assert [r['id'] for r in padrao] == [r['id'] for r in crescente]
        termo = {'pessoas': 'A', 'classificacoes': 'DESPESA', 'contas': 'IGUA'}[recurso]
        busca = cliente.get(f'/api/cadastros/{recurso}', query_string={'busca': termo}).json['registros']
        assert len(busca) >= 2
        assert [r['id'] for r in busca] == sorted(r['id'] for r in busca)
        paginas = [cliente.get(f'/api/cadastros/{recurso}?todos=1&limite=1&pagina={p}').json['registros'][0]['id'] for p in (1, 2)]
        assert paginas == [r['id'] for r in crescente[:2]]
