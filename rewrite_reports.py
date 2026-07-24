import re

# Read original file directly to preserve UTF-8
html = open('d:\\PROJETOS SNYKIA\\dashboad whasatpp presidente disel\\reports.html', encoding='utf-8').read()

# Replace main tab
html = html.replace('📊 Motivos de Finalização</button>', '📦 Relatório de Entregas</button>')
html = html.replace('<span>Motivos de Finalização e Eficiência de tempo de atendimento</span>', '<span>Relatório das Entregas e Eficiência de tempo de atendimento</span>')

# Replace <!-- MOTIVOS --> block
old_motivos_start = html.find('<!-- MOTIVOS -->')
old_motivos_end = html.find('<!-- TEMPO -->')

new_motivos = """  <!-- MOTIVOS -->
  <div class="main-tab-content active" id="main-content-nps">
    <div class="sub-tabs">
      <button class="sub-tab-btn active-nps" id="nps-sub-rotas" onclick="switchSub('nps','rotas')">🚚 Histórico de Rotas</button>
      <button class="sub-tab-btn" id="nps-sub-individuais" onclick="switchSub('nps','individuais')">📋 Entregas Individuais</button>
      <button class="sub-tab-btn" id="nps-sub-metricas" onclick="switchSub('nps','metricas')">⏱️ Métricas dos Entregadores</button>
    </div>

    <div class="sub-content active" id="nps-content-rotas">
      <div class="chart-card fade-in" id="rotas-box" style="min-height:300px">
        <div class="spinner nps-spin"></div>
      </div>
    </div>
    
    <div class="sub-content" id="nps-content-individuais">
      <div class="chart-card fade-in" id="individuais-box" style="min-height:300px">
        <div class="spinner nps-spin"></div>
      </div>
    </div>
    
    <div class="sub-content" id="nps-content-metricas">
      <div class="chart-card fade-in" id="metricas-box" style="min-height:300px">
        <div class="spinner nps-spin"></div>
      </div>
    </div>
  </div>

"""

if old_motivos_start != -1 and old_motivos_end != -1:
    html = html[:old_motivos_start] + new_motivos + html[old_motivos_end:]


# Javascript functions
js_code = """
  let curSubNps = 'rotas';

  async function loadRotas() {
    curSubNps = 'rotas';
    const el = document.getElementById('rotas-box');
    el.innerHTML = '<div class="spinner nps-spin"></div>';
    try {
      const r = await fetch(API+'/api/reports/entregas/rotas'+qp(), {headers:{Authorization:'Bearer '+tok()}});
      const j = await r.json();
      if(!r.ok||!j.success){el.innerHTML=errH(j.error||'Erro');return;}
      const d = j.data||[];
      if(!d.length){el.innerHTML=empH('Nenhuma rota encontrada no período.');return;}
      
      let html = d.map(r => {
        let sub = r.entregas.map(e => `
          <div style="padding:10px; border-bottom:1px solid #eee; display:flex; justify-content:space-between;">
            <div><strong>${e.nome_cliente}</strong> - ${e.localizacao}</div>
            <div>
              <span class="status-badge" style="background:#f0f0f0; padding:2px 8px; border-radius:12px; font-size:12px; font-weight:600;">${e.status}</span>
              ${e.justificativa_falha ? `<div style="color:var(--danger); font-size:11px; margin-top:4px;">Motivo: ${e.justificativa_falha}</div>` : ''}
            </div>
          </div>
        `).join('');
        return `
          <div class="filial-block" style="margin-bottom:15px; background:var(--bg-panel); border:1px solid var(--border-color); border-radius:12px;">
            <div class="filial-header" style="padding:15px;">
              <div class="filial-header-left">
                <div class="filial-icon ti" style="background:var(--primary); color:white;">🚚</div>
                <div>
                  <div class="filial-name">Rota #${r.numero_rota}</div>
                  <div class="filial-meta">${r.entregador_nome} • ${r.entregas.length} entregas</div>
                </div>
              </div>
              <div class="filial-header-right" style="text-align:right;">
                <div style="font-size:12px; color:var(--text-secondary);">Saiu: ${r.saiu_para_entrega_em || '-'}</div>
                <div style="font-size:12px; color:var(--text-secondary);">Fim: ${r.finalizado_em || 'Em andamento'}</div>
              </div>
            </div>
            <div style="background:#fafafa; border-top:1px solid var(--border-color); padding:0 15px;">
              ${sub}
            </div>
          </div>
        `;
      }).join('');
      el.innerHTML = html;
    } catch(e){el.innerHTML=errH('Erro de conexão.');}
  }

  async function loadIndividuais() {
    curSubNps = 'individuais';
    const el = document.getElementById('individuais-box');
    el.innerHTML = '<div class="spinner nps-spin"></div>';
    try {
      const r = await fetch(API+'/api/reports/entregas/individuais'+qp(), {headers:{Authorization:'Bearer '+tok()}});
      const j = await r.json();
      if(!r.ok||!j.success){el.innerHTML=errH(j.error||'Erro');return;}
      const d = j.data||[];
      if(!d.length){el.innerHTML=empH('Nenhuma entrega no período.');return;}

      const rows = d.map(item => {
        const falha = item.justificativa_falha ? `<div style="color:var(--danger);font-size:11px;">Motivo: ${item.justificativa_falha}</div>` : '';
        return `<tr>
          <td><div class="att-info"><span class="att-name">${item.nome_cliente}</span><span class="att-sub">${item.localizacao}</span></div></td>
          <td>${item.entregador_nome}</td>
          <td>Rota #${item.numero_rota || '-'}</td>
          <td><span style="font-weight:600; font-size:12px; padding:2px 8px; border-radius:12px; background:#eee; color:#333;">${item.status}</span>${falha}</td>
          <td>${item.criado_em}</td>
        </tr>`;
      }).join('');

      el.innerHTML = '<div class="ranking-table-wrap">'
        +'<table class="rtable"><thead><tr>'
        +'<th>Cliente / Endereço</th><th>Entregador</th><th>Rota</th><th>Status</th><th>Criada em</th>'
        +'</tr></thead><tbody>'+rows+'</tbody></table></div>';
    } catch(e){el.innerHTML=errH('Erro de conexão.');}
  }

  async function loadMetricas() {
    curSubNps = 'metricas';
    const el = document.getElementById('metricas-box');
    el.innerHTML = '<div class="spinner nps-spin"></div>';
    try {
      const r = await fetch(API+'/api/reports/entregas/metricas_entregador'+qp(), {headers:{Authorization:'Bearer '+tok()}});
      const j = await r.json();
      if(!r.ok||!j.success){el.innerHTML=errH(j.error||'Erro');return;}
      const d = j.data||[];
      if(!d.length){el.innerHTML=empH('Nenhum dado de entregador no período.');return;}

      const fmtMin = (sec) => {
        if(!sec) return '-';
        const m = Math.floor(sec/60);
        const s = Math.floor(sec%60);
        return `${m}m ${s}s`;
      };

      const rows = d.map((item, i) => {
        const medals = ['🥇','🥈','🥉'];
        return `<tr>
          <td><div class="rank-pos"><span class="medal">${medals[i]||''}</span><span>#${i+1}</span></div></td>
          <td><span style="font-weight:600;">${item.entregador_nome}</span></td>
          <td>${item.qtd_rotas}</td>
          <td>${item.qtd_entregas}</td>
          <td style="color:var(--text-secondary); font-weight:500;">${fmtMin(item.avg_tempo_assumir)}</td>
          <td style="color:var(--primary); font-weight:500;">${fmtMin(item.avg_tempo_entrega)}</td>
        </tr>`;
      }).join('');

      el.innerHTML = '<div class="ranking-table-wrap">'
        +'<table class="rtable"><thead><tr>'
        +'<th>#</th><th>Entregador</th><th>Rotas</th><th>Entregas</th><th>Média p/ Iniciar (Assumir)</th><th>Média p/ Entregar</th>'
        +'</tr></thead><tbody>'+rows+'</tbody></table></div>';
    } catch(e){el.innerHTML=errH('Erro de conexão.');}
  }
"""

idx_nps_start = html.find('/* 📊 NPS GERAL */')
idx_tempo_start = html.find('/* ⏱️ TEMPO FILIAIS */')

if idx_nps_start != -1 and idx_tempo_start != -1:
    html = html[:idx_nps_start] + "/* --- ENTREGAS --- */\n" + js_code + "\n" + html[idx_tempo_start:]

html = html.replace("if(sub==='geral') loadMotivosGeral();", "if(sub==='rotas') loadRotas();")
html = html.replace("else if(sub==='atendentes') loadMotivosAtendentes();", "else if(sub==='individuais') loadIndividuais();")
html = html.replace("else if(sub==='individuais') loadMotivosIndividuais();", "else if(sub==='metricas') loadMetricas();")
html = html.replace("if(t==='nps') loadMotivosGeral();", "if(t==='nps') { switchSub('nps', curSubNps); }")
html = html.replace("switchSubIndividuais()", "switchSub('nps','individuais')")
html = html.replace("if(curMain==='nps') loadMotivosGeral();", "if(curMain==='nps') { switchSub('nps', curSubNps); }")

open('d:\\PROJETOS SNYKIA\\dashboad whasatpp presidente disel\\reports.html', 'w', encoding='utf-8').write(html)
