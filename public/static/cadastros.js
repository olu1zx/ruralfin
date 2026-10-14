// Tabelas iniciam vazias; busca, ordenação e paginação são feitas no PostgreSQL.
const recursoCadastro = window.recursoCadastro;
const el = id=>document.getElementById(id);
const configs = {
  pessoas: [['id','ID'],['tipo','Tipo'],['razao_social','Nome / razão social'],['fantasia','Fantasia'],['documento','CPF/CNPJ'],['status','Situação']],
  classificacoes: [['id','ID'],['tipo','Tipo'],['descricao','Descrição'],['status','Situação']],
  contas: [['id','ID'],['tipo','Tipo'],['numero_nota_fiscal','Nota'],['data_emissao','Emissão'],['pessoa','Fornecedor / cliente'],['faturado','Faturado'],['valor_total','Valor (R$)'],['status','Situação']],
};
let ordemTabela = recursoCadastro==='pessoas'?'razao_social':recursoCadastro==='contas'?'data_emissao':'descricao';
let direcaoTabela = 'asc', paginaTabela=1, buscaRealizada=false, registroEditando=null;
let buscaAtual='', statusAtual='ATIVO';
const colunasTabela=configs[recursoCadastro];
async function apiCadastro(url, options={}) {
  const headers={...options.headers,'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content};
  if(options.body) headers['Content-Type']='application/json';
  const res=await fetch(url,{...options,headers}); const dados=await res.json();
  if(!res.ok) throw new Error(dados.error||'Falha na operação.'); return dados;
}
function mensagemCadastro(texto,tipo=''){el('cadastroStatus').textContent=texto;el('cadastroStatus').className='status '+tipo;}
function vazio(texto){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=colunasTabela.length+1;td.textContent=texto;tr.append(td);el('registros').replaceChildren(tr);}
function cabecalhoTabela(){
  const tr=document.createElement('tr');
  colunasTabela.forEach(([campo,nome])=>{
    const th=document.createElement('th');th.scope='col';
    {const b=document.createElement('button');b.type='button';b.textContent=nome+(ordemTabela===campo?(direcaoTabela==='asc'?' ↑':' ↓'):'');
      th.setAttribute('aria-sort',ordemTabela===campo?(direcaoTabela==='asc'?'ascending':'descending'):'none');
      b.addEventListener('click',()=>{direcaoTabela=ordemTabela===campo&&direcaoTabela==='asc'?'desc':'asc';ordemTabela=campo;paginaTabela=1;cabecalhoTabela();if(buscaRealizada)carregarTabela();});th.append(b);}
    tr.append(th);
  });const a=document.createElement('th');a.scope='col';a.textContent='Ações';tr.append(a);el('cabecalho').replaceChildren(tr);
}
function formatarTabela(campo,valor){
  if(campo==='valor_total') return Number(valor).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(campo==='data_emissao') return String(valor).split('-').reverse().join('/');
  if(campo==='tipo') return {'APAGAR':'A pagar','ARECEBER':'A receber','CLIENTE-FORNECEDOR':'Cliente / fornecedor','FATURADO':'Faturado','DESPESA':'Despesa','RECEITA':'Receita'}[valor]||valor;
  return valor??'';
}
async function carregarTabela(){
  mensagemCadastro('Carregando...');
  const q=new URLSearchParams({todos:'1',busca:buscaAtual,status:statusAtual,ordem:ordemTabela,direcao:direcaoTabela,pagina:paginaTabela});
  try {
    const dados=await apiCadastro(`/api/cadastros/${recursoCadastro}?${q}`);
    if(!dados.registros.length && paginaTabela>1){paginaTabela--;return carregarTabela();}
    el('registros').replaceChildren();
    if(!dados.registros.length) vazio('Nenhum registro encontrado.');
    dados.registros.forEach(r=>{
      const tr=document.createElement('tr');colunasTabela.forEach(([campo])=>{const td=document.createElement('td');td.textContent=formatarTabela(campo,r[campo]);tr.append(td);});
      const td=document.createElement('td');
      const editar=document.createElement('button');editar.textContent='Editar';editar.addEventListener('click',()=>abrirCadastro(r.id));td.append(editar);
      const status=document.createElement('button');status.textContent=r.status==='ATIVO'?'Inativar':'Reativar';status.className=r.status==='ATIVO'?'danger':'';
      status.addEventListener('click',()=>mudarStatus(r));td.append(status);
      if(recursoCadastro==='contas'){const doc=document.createElement('button');doc.textContent='PDF';doc.addEventListener('click',()=>abrirDocumento(r.id));td.append(doc);}
      tr.append(td);el('registros').append(tr);
    });
    el('contador').textContent=`${dados.total} registro(s) · Página ${paginaTabela}`;
    el('anteriorBtn').disabled=paginaTabela===1;el('proximaBtn').disabled=paginaTabela*dados.limite>=dados.total;
    mensagemCadastro('');
  }catch(erro){mensagemCadastro(erro.message,'err');}
}
async function abrirDocumento(id){
  try {const conta=await apiCadastro(`/api/cadastros/contas/${id}`);if(!conta.documentoId){mensagemCadastro('Esta conta foi criada manualmente e não possui PDF.');return;}
    window.location.assign(`/api/contas/${id}/documento`);
  }catch(erro){mensagemCadastro(erro.message,'err');}
}
async function abrirCadastro(id=null){
  try {
    const r=id?await apiCadastro(`/api/cadastros/${recursoCadastro}/${id}`):{};
    if(recursoCadastro==='contas'){if(r.status==='INATIVO'){mensagemCadastro('Reative a conta antes de editar.','err');return;}abrirNota(r,null,id);return;}
    registroEditando=id;el('cadastroTitulo').textContent=id?`Editar registro #${id}`:'Novo registro';
    el('tipo').value=r.tipo||(recursoCadastro==='pessoas'?'CLIENTE-FORNECEDOR':'DESPESA');
    if(recursoCadastro==='pessoas'){el('razao').value=r.razao_social||'';el('fantasia').value=r.fantasia||'';el('documento').value=r.documento||'';}
    else el('descricao').value=r.descricao||'';
    el('cadastroEditor').classList.remove('hidden');el('cadastroEditor').scrollIntoView({behavior:'smooth'});
  }catch(erro){mensagemCadastro(erro.message,'err');}
}
async function mudarStatus(registro){
  const inativar=registro.status==='ATIVO';
  if(!confirm(inativar?`Inativar o registro #${registro.id}? O histórico será preservado.`:`Reativar o registro #${registro.id}?`))return;
  try{await apiCadastro(`/api/cadastros/${recursoCadastro}/${registro.id}${inativar?'':'/reativar'}`,{method:inativar?'DELETE':'POST'});await carregarTabela();}
  catch(erro){mensagemCadastro(erro.message,'err');}
}
el('buscarForm').addEventListener('submit',e=>{e.preventDefault();buscaRealizada=true;buscaAtual=el('busca').value;statusAtual=el('situacao').value;paginaTabela=1;carregarTabela();});
el('todosBtn').addEventListener('click',()=>{el('busca').value='';el('situacao').value='ATIVO';buscaAtual='';statusAtual='ATIVO';paginaTabela=1;buscaRealizada=true;carregarTabela();});
el('anteriorBtn').addEventListener('click',()=>{paginaTabela--;carregarTabela();});el('proximaBtn').addEventListener('click',()=>{paginaTabela++;carregarTabela();});
el('novoBtn').addEventListener('click',()=>abrirCadastro());
if(recursoCadastro!=='contas'){
  el('cancelarCadastro').addEventListener('click',()=>el('cadastroEditor').classList.add('hidden'));
  el('cadastroForm').addEventListener('submit',async e=>{
    e.preventDefault();el('salvarCadastro').disabled=true;
    const dados={tipo:el('tipo').value};
    if(recursoCadastro==='pessoas')Object.assign(dados,{razao_social:el('razao').value,fantasia:el('fantasia').value,documento:el('documento').value});
    else dados.descricao=el('descricao').value;
    try{await apiCadastro(`/api/cadastros/${recursoCadastro}${registroEditando?'/'+registroEditando:''}`,{method:registroEditando?'PUT':'POST',body:JSON.stringify(dados)});
      el('cadastroEditor').classList.add('hidden');if(buscaRealizada)await carregarTabela();mensagemCadastro('Registro salvo com sucesso.','ok');}
    catch(erro){mensagemCadastro(erro.message,'err');el('cadastroStatus').scrollIntoView({behavior:'smooth'});}
    finally{el('salvarCadastro').disabled=false;}
  });
}
window.addEventListener('contaSalva',()=>{if(buscaRealizada)carregarTabela();});
cabecalhoTabela();vazio('Use Buscar ou Todos os ativos para carregar os registros.');
