"""Validação dos dados da nota antes de consultar ou gravar o banco."""
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation


class ErroValidacao(ValueError):
    pass


def texto(valor, campo, limite=200, obrigatorio=True):
    if not isinstance(valor, str):
        raise ErroValidacao(f'{campo}: informe um texto.')
    valor = ' '.join(valor.strip().split())
    if obrigatorio and not valor:
        raise ErroValidacao(f'{campo}: campo obrigatório. Corrija os dados antes de lançar.')
    if len(valor) > limite:
        raise ErroValidacao(f'{campo}: limite de {limite} caracteres.')
    return valor


def chave_descricao(valor):
    normalizado = unicodedata.normalize('NFKD', valor)
    return ''.join(c for c in normalizado if not unicodedata.combining(c)).upper()


def documento(valor, campo, cpf=False):
    if not isinstance(valor, str) or re.search(r'[^0-9./\-\s]', valor):
        raise ErroValidacao(f'{campo}: informe somente dígitos ou a máscara de CPF/CNPJ.')
    digitos = re.sub(r'\D', '', valor)
    if len(digitos) not in ([11] if cpf else [11, 14]):
        raise ErroValidacao(f'{campo}: informe {"11 dígitos" if cpf else "11 ou 14 dígitos"}.')
    # Aceita os documentos fictícios dos exemplos acadêmicos (sem validar dígito verificador).
    return digitos


def dinheiro(valor, campo):
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, ValueError):
        raise ErroValidacao(f'{campo}: informe um valor numérico, com ponto decimal.')
    if not numero.is_finite() or numero <= 0 or numero >= Decimal('1000000000000'):
        raise ErroValidacao(f'{campo}: informe um valor positivo dentro do limite permitido.')
    if numero != numero.quantize(Decimal('0.01')):
        raise ErroValidacao(f'{campo}: use no máximo duas casas decimais.')
    return numero.quantize(Decimal('0.01'))


def data(valor, campo):
    if not isinstance(valor, str):
        raise ErroValidacao(f'{campo}: informe uma data.')
    for formato in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(valor, formato).date()
        except ValueError:
            pass
    raise ErroValidacao(f'{campo}: informe uma data válida (DD/MM/AAAA).')


def objeto(valor, campo):
    if not isinstance(valor, dict):
        raise ErroValidacao(f'{campo}: estrutura de dados inválida.')
    return valor


def validar_nota(dados):
    dados = objeto(dados, 'Nota fiscal')
    fornecedor = objeto(dados.get('fornecedor'), 'Fornecedor')
    faturado = objeto(dados.get('faturado'), 'Faturado')
    tipo = dados.get('tipo', 'APAGAR')
    if tipo not in ('APAGAR', 'ARECEBER'):
        raise ErroValidacao('Tipo da conta inválido.')
    categorias = dados.get('classificacoes')
    if categorias is None:
        categorias = [dados.get('classificacaoDespesa', '')]
    if not isinstance(categorias, list) or not 1 <= len(categorias) <= 30:
        raise ErroValidacao('Informe de 1 a 30 classificações para a conta.')
    classificacoes = []
    chaves = set()
    for categoria in categorias:
        descricao = texto(categoria, 'Classificação', limite=120)
        chave = chave_descricao(descricao)
        if chave in chaves:
            raise ErroValidacao('Não repita a mesma classificação na conta.')
        classificacoes.append({'descricao': descricao, 'chave': chave,
                               'tipo': 'DESPESA' if tipo == 'APAGAR' else 'RECEITA'})
        chaves.add(chave)
    total = dinheiro(dados.get('valorTotal'), 'Valor total')
    parcelas = dados.get('parcelas')
    if not isinstance(parcelas, list) or not 1 <= len(parcelas) <= 120:
        raise ErroValidacao('Informe de 1 a 120 parcelas e seus vencimentos.')
    resultado_parcelas = []
    vencimentos = set()
    for numero, parcela in enumerate(parcelas, 1):
        parcela = objeto(parcela, f'Parcela {numero}')
        vencimento = data(parcela.get('dataVencimento'), f'Vencimento da parcela {numero}')
        if vencimento in vencimentos:
            raise ErroValidacao('As parcelas devem ter datas de vencimento distintas.')
        vencimentos.add(vencimento)
        resultado_parcelas.append({'numero': numero, 'dataVencimento': vencimento,
                                   'valor': dinheiro(parcela.get('valor'), f'Valor da parcela {numero}')})
    if sum(p['valor'] for p in resultado_parcelas) != total:
        raise ErroValidacao('A soma das parcelas deve ser igual ao valor total da conta.')
    return {
        'tipo': tipo,
        'numeroNotaFiscal': texto(dados.get('numeroNotaFiscal', ''), 'Número da nota', 60),
        'dataEmissao': data(dados.get('dataEmissao'), 'Data de emissão'),
        'descricaoProdutos': texto(dados.get('descricaoProdutos', ''), 'Descrição dos produtos', 10000, False),
        'valorTotal': total,
        'fornecedor': {'razaoSocial': texto(fornecedor.get('razaoSocial', ''), 'Razão social'),
                       'fantasia': texto(fornecedor.get('fantasia', ''), 'Nome fantasia', obrigatorio=False),
                       'cnpj': documento(fornecedor.get('cnpj', ''), 'CPF/CNPJ do fornecedor ou cliente')},
        'faturado': {'nomeCompleto': texto(faturado.get('nomeCompleto', ''), 'Nome do faturado'),
                     'cpf': documento(faturado.get('cpf', ''), 'CPF do faturado', cpf=True)},
        'classificacoes': classificacoes, 'parcelas': resultado_parcelas,
    }
