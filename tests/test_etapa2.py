from copy import deepcopy
from io import BytesIO
import json
import re
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from db import conectar


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
    token = re.search(r'name="csrf_token" value="([^"]+)"',html).group(1)
    assert cliente.post('/login',data={'usuario':'admin','senha':'senha-teste','csrf_token':token}).status_code == 302
    assert cliente.get('/api/saude').status_code == 200


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
    assert resposta.json==nota
    assert resposta.headers['Cache-Control']=='no-store'


def test_limite_pdf_antes_da_chamada_gemini(cliente, ambiente, monkeypatch):
    monkeypatch.setitem(ambiente.config,'PDF_MAX_MB',1)
    resposta=cliente.post('/api/extrair',data={'apiKey':'teste','pdf':(BytesIO(b'%PDF-'+b'0'*(1024*1024)),'grande.pdf')})
    assert resposta.status_code==413
    assert '1 MB' in resposta.json['error']
