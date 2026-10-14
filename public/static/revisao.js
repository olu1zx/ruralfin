// Revisão, consulta e lançamento; preserva o formato JSON da primeira etapa.
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
async function api(url, options = {}) {
  const headers = {...options.headers, 'X-CSRF-Token': csrfToken};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const resposta = await fetch(url, {...options, headers});
  const dados = await resposta.json();
  if (!resposta.ok) throw new Error(dados.error || 'Não foi possível concluir a operação.');
  return dados;
}
const $ = id => document.getElementById(id);
const notaEditor = $('notaEditor');
let arquivoNota = null;
let contaEditando = null;
let origemNota = 'manual';
let notaConsultada = null;
function notaStatus(mensagem, tipo = '') {
  $('notaStatus').textContent = mensagem;
  $('notaStatus').className = 'status ' + tipo;
}
function invalidarConsulta() {
  notaConsultada = null;
  $('consultaResultado').classList.add('hidden');
  $('lancarActions').classList.add('hidden');
}
function isoData(valor) {
  if (!valor) return '';
  const partes = String(valor).split('/');
  return partes.length === 3 ? `${partes[2]}-${partes[1]}-${partes[0]}` : String(valor).slice(0,10);
}
function adicionarParcela(parcela = {}) {
  const linha = document.createElement('div');
  linha.className = 'parcelas';
  const numero = document.createElement('span'); numero.className = 'numero';
  const vencimento = document.createElement('input'); vencimento.type = 'date'; vencimento.required = true;
  vencimento.value = isoData(parcela.dataVencimento);
  const valor = document.createElement('input'); valor.type = 'number'; valor.min = '0.01'; valor.step = '0.01'; valor.required = true;
  valor.value = parcela.valor ?? '';
  const remover = document.createElement('button'); remover.type = 'button'; remover.textContent = 'Remover';
  remover.addEventListener('click', () => {linha.remove(); renumerarParcelas(); invalidarConsulta();});
  linha.append(numero, vencimento, valor, remover);
  $('notaParcelas').append(linha);
  renumerarParcelas();
}
function renumerarParcelas() {
  [...$('notaParcelas').children].forEach((linha, i) => {
    linha.querySelector('.numero').textContent = i+1;
    linha.querySelector('input[type=date]').setAttribute('aria-label', `Vencimento da parcela ${i+1}`);
    linha.querySelector('input[type=number]').setAttribute('aria-label', `Valor da parcela ${i+1} em reais`);
  });
}
function abrirNota(dados = {}, arquivo = null, id = null) {
  arquivoNota = arquivo; contaEditando = id; origemNota = arquivo ? 'extracao' : 'manual';
  invalidarConsulta(); notaStatus('');
  $('editorTitulo').textContent = id ? `Editar conta #${id}` : 'Revisar dados da nota';
  $('lancarBtn').textContent = id ? 'Salvar alterações da conta' : 'Confirmar e lançar conta';
  $('notaTipo').value = dados.tipo || 'APAGAR';
  $('notaNumero').value = dados.numeroNotaFiscal || dados.numero_nota_fiscal || '';
  $('notaEmissao').value = isoData(dados.dataEmissao || dados.data_emissao);
  $('notaTotal').value = dados.valorTotal ?? dados.valor_total ?? '';
  $('notaDescricao').value = dados.descricaoProdutos || dados.descricao_produtos || '';
  $('notaRazao').value = dados.fornecedor?.razaoSocial || '';
  $('notaFantasia').value = dados.fornecedor?.fantasia || '';
  $('notaCnpj').value = dados.fornecedor?.cnpj || '';
  $('notaNome').value = dados.faturado?.nomeCompleto || '';
  $('notaCpf').value = dados.faturado?.cpf || '';
  $('notaCategorias').value = (dados.classificacoes || [dados.classificacaoDespesa || '']).join('\n');
  $('notaParcelas').replaceChildren();
  const parcelas = dados.parcelas?.length ? dados.parcelas : [{valor: dados.valorTotal ?? ''}];
  parcelas.forEach(adicionarParcela);
  atualizarTipo();
  notaEditor.classList.remove('hidden');
  notaEditor.scrollIntoView({behavior:'smooth', block:'start'});
}
function obterNota() {
  return {
    tipo: $('notaTipo').value, numeroNotaFiscal: $('notaNumero').value, dataEmissao: $('notaEmissao').value,
    valorTotal: $('notaTotal').value, descricaoProdutos: $('notaDescricao').value,
    fornecedor: {razaoSocial: $('notaRazao').value, fantasia: $('notaFantasia').value, cnpj: $('notaCnpj').value},
    faturado: {nomeCompleto: $('notaNome').value, cpf: $('notaCpf').value},
    classificacoes: $('notaCategorias').value.split('\n').map(s=>s.trim()).filter(Boolean),
    parcelas: [...$('notaParcelas').children].map((linha, i)=>({numero:i+1,
      dataVencimento:linha.querySelector('input[type=date]').value, valor:linha.querySelector('input[type=number]').value})),
  };
}
function atualizarTipo() { $('pessoaTitulo').textContent = $('notaTipo').value === 'APAGAR' ? 'Fornecedor' : 'Cliente'; }
$('notaTipo').addEventListener('change', atualizarTipo);
$('notaForm').addEventListener('input', invalidarConsulta);
$('notaForm').addEventListener('change', invalidarConsulta);
$('adicionarParcela').addEventListener('click', ()=>{adicionarParcela();invalidarConsulta();});
$('cancelarNota').addEventListener('click', ()=>{notaEditor.classList.add('hidden');invalidarConsulta();});
function mostrarConsulta(titulo, registro) {
  const bloco = document.createElement('div'); bloco.className = 'consulta' + (registro.existe && registro.status === 'ATIVO' ? '' : ' pendente');
  const cabecalho = document.createElement('strong'); cabecalho.textContent = titulo;
  const nome = document.createElement('span'); nome.textContent = registro.nome;
  const doc = document.createElement('span'); doc.textContent = registro.documento || '';
  const situacao = document.createElement('strong'); situacao.textContent = registro.mensagem;
  bloco.append(cabecalho,nome,doc,situacao);
  if(registro.nomeCadastrado && registro.nomeCadastrado !== registro.nome) {
    const aviso = document.createElement('span'); aviso.textContent = `Nome no cadastro: ${registro.nomeCadastrado}. O cadastro existente será utilizado.`; bloco.append(aviso);
  }
  $('consultaResultado').append(bloco);
}
$('notaForm').addEventListener('submit', async evento=>{
  evento.preventDefault(); invalidarConsulta(); const nota = obterNota();
  $('analisarBtn').disabled = true; notaStatus('Consultando o PostgreSQL...');
  try {
    const resultado = await api('/api/analisar', {method:'POST',body:JSON.stringify(nota)});
    const duplicadaPropria = contaEditando && resultado.duplicada?.id === contaEditando;
    const inativo = [resultado.fornecedor,resultado.faturado,...resultado.classificacoes].some(r=>r.status==='INATIVO');
    $('consultaResultado').replaceChildren();
    mostrarConsulta(nota.tipo==='APAGAR'?'FORNECEDOR':'CLIENTE', resultado.fornecedor);
    mostrarConsulta('FATURADO', resultado.faturado);
    resultado.classificacoes.forEach(r=>mostrarConsulta(nota.tipo==='APAGAR'?'DESPESA':'RECEITA',r));
    $('consultaResultado').classList.remove('hidden');
    // Uma resposta de uma consulta antiga não autoriza salvar dados já alterados.
    if (JSON.stringify(nota) !== JSON.stringify(obterNota())) {invalidarConsulta();notaStatus('Dados alterados. Consulte novamente.');return;}
    if (resultado.podeLancar || (duplicadaPropria && !inativo)) {
      notaConsultada = JSON.stringify(nota); $('lancarActions').classList.remove('hidden');
      notaStatus(duplicadaPropria?'Dados conferidos. Confirme as alterações.':resultado.orientacao);
    } else notaStatus(resultado.orientacao,'err');
  } catch(erro) {notaStatus(erro.message,'err');}
  finally {$('analisarBtn').disabled=false;}
});
$('lancarBtn').addEventListener('click', async()=>{
  const nota = obterNota();
  if(notaConsultada !== JSON.stringify(nota)) {invalidarConsulta();notaStatus('Consulte novamente antes de confirmar.','err');return;}
  $('lancarBtn').disabled=true; $('analisarBtn').disabled=true; notaStatus('Gravando conta e parcelas...');
  try {
    let resultado;
    if(contaEditando) resultado=await api(`/api/cadastros/contas/${contaEditando}`,{method:'PUT',body:JSON.stringify(nota)});
    else {
      const form = new FormData(); form.append('dados', JSON.stringify(nota)); form.append('origem', origemNota);
      if(arquivoNota) form.append('pdf', arquivoNota);
      resultado=await api('/api/lancar',{method:'POST',body:form});
    }
    invalidarConsulta(); notaStatus(`${resultado.mensagem} Conta #${resultado.id}.`,'ok');
    window.dispatchEvent(new CustomEvent('contaSalva',{detail:resultado}));
  } catch(erro){notaStatus(erro.message,'err');}
  finally{$('lancarBtn').disabled=false;$('analisarBtn').disabled=false;}
});
