// js/dashboard.js 

// ─── State ───────────────────────────────────────────────────────────────────
const API_URL = window.location.origin;
let socket = null;
let currentView = 'chats';
let currentChat = null;
// CONTACTS está definido em data.js como let
let currentTab = 'all';
let currentInstance = 'all';
let currentTagFilter = 'all';
let sidebarOpen = true;
let emojiVisible = false;

// INSTANCES e EMOJIS estão definidos em data.js como let

// ─── Helpers ──────────────────────────────────────────────────────────────────
function getDefaultInstance() {
    return 'corpal';
}

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // checkAuth(); // Replaced by window.onload
  // initSocket(); // Replaced by window.onload
  // renderChatList(CONTACTS); // Replaced by window.onload
  renderInstances();
  renderEmojis();
});

window.onload = async () => {
  const userStr = localStorage.getItem('wp_crm_user');
  const token = localStorage.getItem('wp_crm_token');

  // ── Garante que o overlay de loading esta visivel ──
  const overlay = document.getElementById('dbLoadingOverlay');
  const loadingMsg = document.getElementById('dbLoadingMsg');
  if (overlay) overlay.style.display = 'flex';

  if (!userStr || !token) {
    window.location.href = 'index.html';
    return;
  }

  const user = JSON.parse(userStr);

  // ── Etapa 1: conectar ao DB e carregar dados reais ──
  if (loadingMsg) loadingMsg.textContent = 'Autenticando e carregando conversas...';
  let dbOk = false;
  try {
    await loadContacts();
    dbOk = true;
  } catch (e) {
    console.error('[INIT] Falha ao conectar ao banco de dados:', e);
    if (loadingMsg) loadingMsg.textContent = 'Erro ao conectar. Tentando novamente...';
    try {
      await new Promise(r => setTimeout(r, 2000));
      await loadContacts();
      dbOk = true;
    } catch (e2) {
      console.error('[INIT] Segunda tentativa falhou:', e2);
      if (loadingMsg) loadingMsg.textContent = 'Falha na conexao. Redirecionando...';
      setTimeout(() => { window.location.href = 'index.html'; }, 3000);
      return;
    }
  }

  // ── Etapa 2: inicializar interface apenas com dados reais ──
  if (loadingMsg) loadingMsg.textContent = 'Carregando interface...';
  renderUserProfile(user);
  initSocket(token);
  renderInstanceSelector();
  renderTagFilter();
  renderChatList(getFilteredContacts());

  // ── Etapa 3: esconder o overlay ──
  if (overlay) {
    overlay.style.transition = 'opacity 0.35s ease';
    overlay.style.opacity = '0';
    setTimeout(() => { overlay.style.display = 'none'; }, 360);
  }

  // Vista padrão: todos iniciam nas Conversas
  setView('contacts');

  // Para usuarios nao-admin, recarregar contatos periodicamente
  if (user.role !== 'admin') {
    setInterval(async () => {
      await loadContacts();
      renderTagFilter();
      renderChatList(getFilteredContacts());
    }, 30000);
  }
};

function renderUserProfile(user) {
  document.getElementById('userAvatar').textContent = user.name.charAt(0).toUpperCase();
  document.getElementById('userAvatar').title = user.name + ' (' + user.email + ')';

  if (user.role === 'admin') {
    document.getElementById('navAdmin').style.display = 'flex';
    document.getElementById('navReports').style.display = 'flex';
    const btnNavAdminEntregadores = document.getElementById('navAdminEntregadores');
    if (btnNavAdminEntregadores) btnNavAdminEntregadores.style.display = 'flex';
  }
  
  // O botão de transferência agora é controlado dentro do updateAttendanceBar
  // Mas vamos garantir que ele está disponível no DOM.
  const btnTransfer = document.getElementById('btnTransferChat');
  if (btnTransfer) btnTransfer.style.display = 'none'; // Será ativado no openChat/updateAttendanceBar
  
  if (user.role === 'user') {
    const navInst = document.getElementById('navInstances');
    if (navInst) navInst.style.display = 'none';
  }
}

async function loadContacts() {
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/contacts`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (res.ok) {
      let contacts = await res.json();
      
      // ── Filtro de segurança no cliente (segunda camada) ──
      const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
      if (userData.role !== 'admin') {
        const myFilial = userData.filial || '';
        const mySetor = userData.setor || '';
        
        contacts = contacts.filter(c => {
          const tags = c.tags || [];
          
          // Detectar tags no formato "Filial:Setor"
          // Regra: manter o contato se PELO MENOS UMA tag Filial:Setor é da minha filial
          // (transferências criam tags de múltiplas filiais intencionalmente)
          const filialTags = tags.filter(t => typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:'));
          if (filialTags.length > 0 && myFilial) {
            const hasMyFilial = filialTags.some(t => t.split(':')[0] === myFilial);
            if (!hasMyFilial) {
              return false; // Nenhuma tag Filial:Setor é da minha filial
            }
          }
          
          // Para gestor: exibir contatos com tag da sua filial ou sem tag de filial
          if (userData.role === 'gestor') {
            return true; // Já passou pelo filtro acima (não tem tag de outra filial)
          }
          
          // Para user comum: só exibir se tem a tag exata do seu email ou atribuído a mim
          if (userData.email) {
            const requiredTag = userData.email.toLowerCase();
            const hasMyTag = tags.some(t => typeof t === 'string' && t.toLowerCase() === requiredTag);
            const assignedToMe = c.assigned_to === userData.id;
            return hasMyTag || assignedToMe;
          }
          
          return true;
        });
      }
      
      // ── Mesclar com contatos existentes, preservando mensagens carregadas ──
      const existingMap = new Map(CONTACTS.map(c => [c.id, c]));
      contacts.forEach(c => {
        const old = existingMap.get(c.id);
        if (old) {
          // Preservar array de mensagens já carregado (não vem do servidor)
          if (old.messages && old.messages.length > 0) {
            c.messages = old.messages;
          }
        }
      });
      CONTACTS = contacts;
      
      // ── Manter referência do currentChat sincronizada ──
      if (currentChat) {
        const updated = CONTACTS.find(c => c.id === currentChat.id);
        if (updated) {
          // Preservar mensagens do chat aberto no novo objeto
          if (currentChat.messages && currentChat.messages.length > 0 && (!updated.messages || updated.messages.length === 0)) {
            updated.messages = currentChat.messages;
          }
          currentChat = updated;
        }
      }
    }
  } catch (e) {
    console.error('Erro ao carregar contatos:', e);
  }
}

let currentBootId = null;

function initSocket(token) {
  if (typeof io !== 'undefined') {
    socket = io(API_URL, {
      extraHeaders: {
        Authorization: `Bearer ${token}`
      }
    });
    
    socket.on('connect', () => {
      console.log('Conectado ao Backend WPCRM via Socket');
      socket.emit('join_company', 'comp_1');
      
      // Join instance rooms for isolation
      const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
      socket.emit('join_instances', {
        instances: userData.instances || [],
        role: userData.role || 'user'
      });
    });

    socket.on('server_boot', (data) => {
      if (!currentBootId) {
        currentBootId = data.boot_id;
      } else if (currentBootId !== data.boot_id) {
        console.log('Servidor atualizado/reiniciado! Recarregando a página para aplicar nova versão...');
        window.location.reload();
      } else {
        console.log('Reconexão de rede detectada (sem reinício do servidor). Ressincronizando chats silenciosamente...');
        loadContacts();
      }
    });

    socket.on('whatsapp_event', (data) => {
      handleIncomingWebhook(data);
    });

    socket.on('whatsapp_ack', (data) => {
      console.log('Ack recebido:', data);
      // Atualizar msg object em CONTACTS
      let found = false;
      for (const contact of CONTACTS) {
        if (!contact.messages) continue;
        const msg = contact.messages.find(m => m.id === data.messageId);
        if (msg) {
          msg.ack = data.ack;
          found = true;
          break;
        }
      }
      // Se a mensagem estiver no currentChat renderizado, atualiza
      if (found && currentChat && currentChat.messages) {
        const chatMsg = currentChat.messages.find(m => m.id === data.messageId);
        if (chatMsg) {
            renderMessages(currentChat.messages);
        }
      }
    });

    socket.on('chat_assignment', (data) => {
      handleChatAssignment(data);
    });

    socket.on('chat_tags_updated', (data) => {
      console.log('[Socket] chat_tags_updated recebido:', data);
      let contact = CONTACTS.find(c => c.id === data.id);
      
      // Fallback: busca por phone extraído do ID (c_PHONE_INSTANCE)
      if (!contact && data.id) {
        const parts = data.id.split('_');
        if (parts.length >= 2) {
          const phone = parts[1];
          contact = CONTACTS.find(c => c.phone === phone || c.id.includes(phone));
        }
      }
      
      if (contact) {
        contact.tags = data.tags;
        
        // ── Filtro de segurança: remover contato se NENHUMA tag Filial:Setor é da minha filial ──
        const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
        if (userData.role !== 'admin' && userData.filial) {
          const newTags = data.tags || [];
          const filialTags = newTags.filter(t => typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:'));
          if (filialTags.length > 0) {
            const hasMyFilial = filialTags.some(t => t.split(':')[0] === userData.filial);
            if (!hasMyFilial) {
              // Nenhuma tag Filial:Setor é da minha filial — remover da lista
              console.log('[Socket] Contato não pertence à minha filial, removendo:', contact.id, filialTags);
              CONTACTS = CONTACTS.filter(c => c.id !== contact.id);
              if (currentChat && currentChat.id === contact.id) {
                currentChat = null;
                document.getElementById('chatEmpty').style.display = 'flex';
                document.getElementById('chatInterface').style.display = 'none';
              }
              renderChatList(getFilteredContacts());
              renderTagFilter();
              return;
            }
          }
        }
        
        if (currentChat && currentChat.id === contact.id) {
            currentChat.tags = data.tags;
            updateContactDetails(currentChat);
        }
        renderChatList(getFilteredContacts());
        renderTagFilter();
        console.log('[Socket] Tags atualizadas para:', contact.id, data.tags);
      } else {
        // Contato não está na lista local — pode ser um chat transferido para mim
        // Verifica se alguma das novas tags é do meu setor para forçar reload
        const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
        if (userData.role !== 'admin' && userData.filial && userData.setor) {
          const myTag = `${userData.filial}:${userData.setor}`;
          if (data.tags && data.tags.includes(myTag)) {
            console.log('[Socket] Chat transferido para meu setor detectado, recarregando contatos:', data.id);
            loadContacts().then(() => {
              renderTagFilter();
              renderChatList(getFilteredContacts());
            });
            return;
          }
        } else if (userData.role === 'gestor' && userData.filial) {
          const newTags = data.tags || [];
          const hasMyFilial = newTags.some(t => typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:') && t.split(':')[0] === userData.filial);
          if (hasMyFilial) {
            console.log('[Socket] Chat transferido para minha filial (gestor), recarregando:', data.id);
            loadContacts().then(() => {
              renderTagFilter();
              renderChatList(getFilteredContacts());
            });
            return;
          }
        }
        console.warn('[Socket] Contato não encontrado para atualizar tags:', data.id);
      }
    });

    socket.on('chat_avatar_updated', (data) => {
      console.log('[Socket] chat_avatar_updated recebido:', data);
      const contact = CONTACTS.find(c => c.id === data.id);
      if (contact) {
        contact.avatar = data.avatar;
        renderChatList(getFilteredContacts());
        
        // Atualiza a foto no header do chat se estiver aberto
        if (currentChat && currentChat.id === data.id) {
          currentChat.avatar = data.avatar;
          const avatarEl = document.getElementById('currentChatAvatar');
          if (avatarEl) {
            if (data.avatar.startsWith('http')) {
              avatarEl.innerHTML = `<img src="${data.avatar}" alt="Avatar">`;
            } else {
              avatarEl.innerHTML = data.avatar;
            }
          }
        }
      }
    });

  } else {
    console.warn('Socket.io no encontrado. Rodando em modo offline/mock.');
  }
}

// IDs de áudios enviados pelo atendente (para evitar duplicação via socket)
let _pendingAudioIds = new Set();
let _pendingImageIds = new Set();
let _pendingVideoIds = new Set();
let _pendingDocIds = new Set();

function handleIncomingWebhook(data) {
  console.log('Evento WhatsApp recebido:', data.event);
  
  if (data.event === 'messages.upsert' || data.event === 'send.message') {
    // Skip outgoing media events we already rendered locally
    const msgId = data.data?.key?.id;
    if (msgId && (_pendingAudioIds.has(msgId) || _pendingImageIds.has(msgId) || _pendingVideoIds.has(msgId) || _pendingDocIds.has(msgId))) {
      console.log('Skipping duplicate socket event for sent media:', msgId);
      _pendingAudioIds.delete(msgId);
      _pendingImageIds.delete(msgId);
      _pendingVideoIds.delete(msgId);
      _pendingDocIds.delete(msgId);
      return;
    }
    const msg = data.data;
    const key = msg.key;
    const remoteJid = key.remoteJid;
    if (!remoteJid || remoteJid === 'status@broadcast') return;

    const phone = remoteJid.split('@')[0].split(':')[0];
    let fromMe = key.fromMe;
    if (data.event === 'send.message') fromMe = true;

    // Preferir texto processado pelo backend (já inclui [AUDIO_REF] etc)
    let text = data._processed_text;
    if (!text) {
        text = msg.message?.conversation || 
               msg.message?.extendedTextMessage?.text || 
               msg.message?.buttonsResponseMessage?.selectedDisplayText || 
               msg.message?.listResponseMessage?.title || 
               msg.message?.imageMessage?.caption || 
               msg.message?.videoMessage?.caption || 
               msg.message?.documentMessage?.caption || 
               "[Mensagem N8N/Mídia]";
               
        if (msg.message?.audioMessage) {
            const inst = data._instance || data.instance || 'unknown';
            const msgId = key.id || '';
            text = `[AUDIO_REF] ${inst}|${msgId}`;
        }
        
        if (msg.message?.imageMessage) {
            const inst = data._instance || data.instance || 'unknown';
            const msgId = key.id || '';
            const caption = msg.message.imageMessage.caption || '';
            text = `[IMAGE_REF] ${inst}|${msgId}`;
            if (caption) text += `\n${caption}`;
        }
        
        if (msg.message?.videoMessage) {
            const inst = data._instance || data.instance || 'unknown';
            const msgId = key.id || '';
            const caption = msg.message.videoMessage.caption || '';
            text = `[VIDEO_REF] ${inst}|${msgId}`;
            if (caption) text += `\n${caption}`;
        }
        
        if (msg.message?.documentMessage) {
            const inst = data._instance || data.instance || 'unknown';
            const msgId = key.id || '';
            const docName = msg.message.documentMessage.fileName || 'Arquivo';
            text = `[DOC_REF] ${inst}|${msgId}|${docName}`;
        }

        if (msg.message?.contactMessage) {
            const cd = msg.message.contactMessage;
            const displayName = cd.displayName || 'Contato';
            const vcard = cd.vcard || '';
            let contactPhone = '';
            for (const line of vcard.split('\n')) {
                if (line.includes('waid=')) {
                    contactPhone = line.split('waid=')[1].split(':')[0];
                    break;
                } else if (line.trim().toUpperCase().startsWith('TEL')) {
                    contactPhone = line.split(':').pop().trim().replace(/\r/g, '');
                }
            }
            text = `[CONTACT_REF] ${displayName}|${contactPhone}|${vcard}`;
        }

        if (msg.message?.contactsArrayMessage) {
            const contacts = msg.message.contactsArrayMessage.contacts || [];
            const names = contacts.map(c => c.displayName || '?').join(', ');
            text = `[CONTACT_REF] ${names}||`;
        }
    }
    
    const now = new Date();
    const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;

    const instName = data.instance || data._instance || 'unknown';
    let contact = CONTACTS.find(c => c.phone === phone && c.instance === instName);

    let type = fromMe ? 'out' : 'in';

    if (!contact) {
      // instName já declarado acima (linha 308), reutilizar
      contact = {
        id: 'c_' + phone + '_' + instName,
        name: phone,
        phone: phone,
        avatar: phone.charAt(0),
        lastMsg: text,
        time: time,
        unread: fromMe ? 0 : 1,
        messages: [],
        tags: ['Novo Lead'],
        instance: instName
      };
      CONTACTS.unshift(contact);
    } else {
      contact.lastMsg = text;
      contact.time = time;
      if (!fromMe && currentChat?.id !== contact.id) contact.unread++;
    }

    const newMsg = { id: key.id, text, type, time };
    
    // Ensure messages array exists
    if (!contact.messages) contact.messages = [];
    
    // Check for duplicates before pushing
    let isDuplicate = false;
    if (contact.messages.find(m => m.id === newMsg.id)) {
      isDuplicate = true;
    } else {
      contact.messages.push(newMsg);
    }

    // Se for o chat aberto, renderiza
    if (!isDuplicate && currentChat && currentChat.id === contact.id) {
      renderMessages(currentChat.messages);
      
      // Scroll to bottom when a new message arrives
      setTimeout(() => {
        const area = document.getElementById('messagesArea');
        if (area) area.scrollTop = area.scrollHeight;
      }, 50);
    }

    renderChatList(getFilteredContacts());
  }
}

// ─── View Navigation ──────────────────────────────────────────────────────────
function setView(view) {
  currentView = view;

  // Update active nav button
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('nav' + capitalize(view));
  if (btn) btn.classList.add('active');

  if (view === 'instances') {
    openInstancesModal();
    return;
  }
  
  if (view === 'entregas') {
    document.getElementById('panelList').style.display = 'none';
    document.getElementById('chatArea').style.display = 'none';
    document.getElementById('sidebarDetails').style.display = 'none';
    document.getElementById('adminEntregadoresView').style.display = 'none';
    
    const panelEntregas = document.getElementById('panelEntregas');
    if (panelEntregas) {
      panelEntregas.style.display = 'flex';
      loadEntregas();
    }
  } else if (view === 'admin_entregadores') {
    document.getElementById('panelList').style.display = 'none';
    document.getElementById('chatArea').style.display = 'none';
    document.getElementById('sidebarDetails').style.display = 'none';
    const panelEntregas = document.getElementById('panelEntregas');
    if (panelEntregas) panelEntregas.style.display = 'none';
    
    document.getElementById('adminEntregadoresView').style.display = 'flex';
    loadAdminEntregadores();
  } else {
    document.getElementById('panelList').style.display = 'flex';
    document.getElementById('chatArea').style.display = 'flex';
    if (currentChat) {
      document.getElementById('sidebarDetails').style.display = 'flex';
    }
    
    const panelEntregas = document.getElementById('panelEntregas');
    if (panelEntregas) {
      panelEntregas.style.display = 'none';
    }
    document.getElementById('adminEntregadoresView').style.display = 'none';
    
    document.getElementById('panelTitle').textContent = {
      chats: 'Conversas', contacts: 'Contatos', settings: 'Configurações'
    }[view] || 'Conversas';
  }
}

// ─── Lógica do Gestor de Entregas (Mapa & Histórico) ──────────────────────────

function toggleMapVisibility() {
  const mapContainer = document.getElementById('mapContainer');
  const icon = document.getElementById('mapToggleIcon');
  if (mapContainer.style.display === 'none') {
    mapContainer.style.display = 'block';
    icon.style.transform = 'rotate(180deg)';
    if (globalDriverMap) {
      setTimeout(() => globalDriverMap.invalidateSize(), 300);
    } else {
      setTimeout(initGlobalDriverMap, 300);
    }
  } else {
    mapContainer.style.display = 'none';
    icon.style.transform = 'rotate(0deg)';
  }
}

async function loadRouteHistory() {
  const container = document.getElementById('routeHistoryContainer');
  if (!container) return;
  
  try {
    const res = await fetch(`${API_URL}/api/admin/entregadores/historico_rotas`, {
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    });
    if (!res.ok) throw new Error('Falha ao buscar histórico de rotas');
    
    const historico = await res.json();
    
    if (historico.length === 0) {
      container.innerHTML = '<div style="color:var(--text-secondary); text-align:center; padding: 20px;">Nenhum histórico de rota encontrado.</div>';
      return;
    }
    
    let html = '';
    historico.forEach(rota => {
      // Formata horários
      const saiuDate = new Date(rota.saiu_para_entrega_em.replace('Z', '+00:00'));
      const saiuTime = saiuDate.toLocaleString('pt-BR');
      
      let finalizouTime = 'Em andamento';
      if (rota.finalizado_em) {
        const finDate = new Date(rota.finalizado_em.replace('Z', '+00:00'));
        finalizouTime = finDate.toLocaleString('pt-BR');
      }
      
      let entregasHtml = '';
      rota.entregas.forEach(ent => {
        const entFinDate = ent.finalizado_em ? new Date(ent.finalizado_em.replace('Z', '+00:00')).toLocaleTimeString('pt-BR') : '--:--:--';
        let statusBadge = '';
        if (ent.status === 'Entregue') {
          statusBadge = `<span style="background:var(--tag-green); color:white; padding:2px 6px; border-radius:4px; font-size:11px;">Entregue</span>`;
        } else if (ent.status === 'Falha na Entrega') {
          statusBadge = `<span style="background:var(--tag-red); color:white; padding:2px 6px; border-radius:4px; font-size:11px;" title="${ent.justificativa || ''}">Falhou</span>`;
        } else {
          statusBadge = `<span style="background:var(--tag-blue); color:white; padding:2px 6px; border-radius:4px; font-size:11px;">Em rota</span>`;
        }
        entregasHtml += `
          <div style="display:flex; justify-content:space-between; align-items:center; padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 13px;">
            <span>ID #${ent.id} - ${ent.cliente}</span>
            <div style="display:flex; gap: 8px; align-items:center;">
              ${statusBadge}
              <span style="color:var(--text-muted); font-size:12px;">${entFinDate}</span>
            </div>
          </div>
        `;
      });
      
      html += `
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; padding: 16px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <h4 style="margin: 0; color: var(--green); display: flex; align-items: center; gap: 8px;">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
              Rota #${rota.numero_rota}: ${rota.entregador_nome}
            </h4>
            <div style="font-size: 12px; color: var(--text-secondary); text-align: right;">
              <div>Saiu: <strong style="color:var(--text-primary);">${saiuTime}</strong></div>
              <div>Fim: <strong style="color:var(--text-primary);">${finalizouTime}</strong></div>
            </div>
          </div>
          <div style="background: rgba(0,0,0,0.15); padding: 8px 12px; border-radius: 6px;">
            ${entregasHtml}
          </div>
        </div>
      `;
    });
    
    container.innerHTML = html;
  } catch(err) {
    console.error(err);
    container.innerHTML = '<div style="color:#f87171; text-align:center; padding: 20px;">Erro ao carregar histórico.</div>';
  }
}


function openNovaEntregaModal() {
  document.getElementById('novaEntregaModal').style.display = 'flex';
  document.getElementById('entregaNomeCliente').value = '';
  document.getElementById('entregaTelefone').value = '';
  document.getElementById('entregaNomePeca').value = '';
  document.getElementById('entregaTamanhoPeca').value = '';
  document.getElementById('entregaLocalizacao').value = '';
  document.getElementById('entregaPago').checked = false;
  document.getElementById('entregaFormaPagamento').value = '';
  document.getElementById('entregaValor').value = '';
  document.getElementById('entregaStatus').value = 'Pronto para coleta';
  document.getElementById('entregaLat').value = '';
  document.getElementById('entregaLng').value = '';
  togglePagamentoFields();
  
  // Initialize map after modal is visible
  setTimeout(() => { initLeafletMap(); }, 100);
}

let entregaMap = null;
let entregaMarker = null;

function initLeafletMap() {
  if (entregaMap) {
    entregaMap.invalidateSize();
    return;
  }
  
  // Default coordinate (Brazil center as fallback)
  const initialLat = -14.235004;
  const initialLng = -51.92528;
  
  entregaMap = L.map('mapEntrega').setView([initialLat, initialLng], 4);
  
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(entregaMap);
  
  entregaMarker = L.marker([initialLat, initialLng], {draggable: true}).addTo(entregaMap);
  
  entregaMarker.on('dragend', function(e) {
    const position = entregaMarker.getLatLng();
    updateMapLocationFields(position.lat, position.lng);
  });
  
  entregaMap.on('click', function(e) {
    entregaMarker.setLatLng(e.latlng);
    updateMapLocationFields(e.latlng.lat, e.latlng.lng);
  });
  
  // Try to get user location
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(function(pos) {
      const lat = pos.coords.latitude;
      const lng = pos.coords.longitude;
      entregaMap.setView([lat, lng], 14);
      entregaMarker.setLatLng([lat, lng]);
      updateMapLocationFields(lat, lng);
    });
  }
}

async function updateMapLocationFields(lat, lng) {
  document.getElementById('entregaLat').value = lat;
  document.getElementById('entregaLng').value = lng;
  
  try {
    const res = await fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lng}`);
    const data = await res.json();
    if (data && data.display_name) {
      document.getElementById('entregaLocalizacao').value = data.display_name;
    }
  } catch (err) {
    console.error('Erro ao buscar endereço do pino:', err);
  }
}

async function buscarLocalizacaoMapa() {
  const query = document.getElementById('entregaLocalizacao').value.trim();
  if (!query) return;
  
  try {
    const res = await fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(query)}`);
    const data = await res.json();
    if (data && data.length > 0) {
      const lat = parseFloat(data[0].lat);
      const lng = parseFloat(data[0].lon);
      if (entregaMap && entregaMarker) {
        entregaMap.setView([lat, lng], 16);
        entregaMarker.setLatLng([lat, lng]);
        document.getElementById('entregaLat').value = lat;
        document.getElementById('entregaLng').value = lng;
      }
    } else {
      alert('Endereço não encontrado no mapa.');
    }
  } catch (err) {
    console.error(err);
  }
}

function closeNovaEntregaModal() {
  document.getElementById('novaEntregaModal').style.display = 'none';
}

function togglePagamentoFields() {
  const isPago = document.getElementById('entregaPago').checked;
  const divPagamento = document.getElementById('dadosPagamento');
  if (isPago) {
    divPagamento.style.display = 'none';
  } else {
    divPagamento.style.display = 'block';
  }
}

async function submitNovaEntrega() {
  const token = localStorage.getItem('wp_crm_token');
  const nomeCliente = document.getElementById('entregaNomeCliente').value.trim();
  const telefone = document.getElementById('entregaTelefone').value.trim();
  const nomePeca = document.getElementById('entregaNomePeca').value.trim();
  const tamanhoPeca = document.getElementById('entregaTamanhoPeca').value.trim();
  const localizacao = document.getElementById('entregaLocalizacao').value.trim();
  const isPago = document.getElementById('entregaPago').checked;
  const formaPagamento = document.getElementById('entregaFormaPagamento').value;
  const valor = document.getElementById('entregaValor').value.trim();
  const status = document.getElementById('entregaStatus').value;

  if (!nomeCliente || !nomePeca || !localizacao) {
    alert('Preencha os campos obrigatórios (Nome do Cliente, Nome da Peça, Localização).');
    return;
  }

  try {
    const res = await fetch(`${API_URL}/api/entregas`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        nome_cliente: nomeCliente,
        telefone_cliente: telefone,
        nome_peca: nomePeca,
        tamanho_peca: tamanhoPeca,
        localizacao: localizacao,
        pago: isPago,
        forma_pagamento: isPago ? null : formaPagamento,
        valor: isPago ? null : valor,
        status: status,
        latitude: document.getElementById('entregaLat').value || null,
        longitude: document.getElementById('entregaLng').value || null
      })
    });

    if (res.ok) {
      closeNovaEntregaModal();
      loadEntregas();
    } else {
      const err = await res.json();
      alert('Erro ao salvar entrega: ' + (err.error || 'Desconhecido'));
    }
  } catch (e) {
    console.error('Erro ao salvar entrega:', e);
  }
}

async function loadEntregas() {
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/entregas`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (res.ok) {
      const entregas = await res.json();
      renderEntregas(entregas);
    }
  } catch (e) {
    console.error('Erro ao carregar entregas:', e);
  }
}

function renderEntregas(entregas) {
  const tbody = document.getElementById('listaEntregasBody');
  if (!tbody) return;
  tbody.innerHTML = '';

  if (entregas.length === 0) {
    tbody.innerHTML = `<tr><td colspan="1" style="padding:40px; text-align:center; color:#8696a0; font-size:16px;">📭 Nenhuma entrega encontrada.</td></tr>`;
    return;
  }

  const statusConfig = {
    'Pronto para coleta': { bg: '#0d2e1a', color: '#00c896', border: '#00c896', icon: '📦' },
    'Saiu para entrega':  { bg: '#1a1a0d', color: '#f59e0b', border: '#f59e0b', icon: '🚚' },
    'Entregue':           { bg: '#0d1a2e', color: '#3b82f6', border: '#3b82f6', icon: '✅' },
    'Falha na Entrega':   { bg: '#2e0d0d', color: '#ef4444', border: '#ef4444', icon: '❌' },
    'Cancelado':          { bg: '#2e0d0d', color: '#ef4444', border: '#ef4444', icon: '🚫' },
  };

  // Use a container div instead of table rows
  tbody.closest('table').style.display = 'none';
  let container = document.getElementById('entregasCardsContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'entregasCardsContainer';
    container.style.cssText = 'display:flex;flex-direction:column;gap:14px;padding:16px;';
    tbody.closest('table').parentNode.insertBefore(container, tbody.closest('table'));
  }
  container.innerHTML = '';

  entregas.forEach(e => {
    const cfg = statusConfig[e.status] || { bg: '#1e1e2e', color: '#aaa', border: '#aaa', icon: '⏳' };

    let mapsLink = '';
    if (e.latitude && e.longitude) {
      mapsLink = `<a href="https://maps.google.com/?q=${e.latitude},${e.longitude}" target="_blank"
        style="display:inline-flex;align-items:center;gap:5px;color:#00c896;font-size:14px;font-weight:600;text-decoration:none;margin-top:6px;padding:6px 12px;background:rgba(0,200,150,0.1);border-radius:8px;border:1px solid rgba(0,200,150,0.3);">
        📍 Ver no Mapa
      </a>`;
    }

    const falhaHtml = e.justificativa_falha
      ? `<div style="margin-top:8px;padding:10px 14px;background:#2e0d0d;border-radius:8px;border-left:4px solid #ef4444;font-size:14px;color:#ef4444;">
           ⚠️ <strong>Motivo da falha:</strong> ${escapeHtml(e.justificativa_falha)}
         </div>`
      : '';

    const pagHtml = e.pago
      ? `<span style="background:rgba(0,200,150,0.15);color:#00c896;border:1px solid rgba(0,200,150,0.3);padding:6px 14px;border-radius:20px;font-size:14px;font-weight:700;">✅ Pago</span>`
      : `<span style="background:rgba(245,158,11,0.15);color:#f59e0b;border:1px solid rgba(245,158,11,0.3);padding:6px 14px;border-radius:20px;font-size:14px;font-weight:700;">💰 A Pagar ${e.forma_pagamento ? '— ' + e.forma_pagamento : ''} ${e.valor ? '— R$ ' + e.valor : ''}</span>`;

    const card = document.createElement('div');
    card.style.cssText = `
      background: var(--bg-panel);
      border: 1px solid rgba(255,255,255,0.08);
      border-left: 5px solid ${cfg.border};
      border-radius: 14px;
      padding: 20px 24px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.25);
      transition: box-shadow 0.2s;
    `;
    card.onmouseenter = () => card.style.boxShadow = '0 6px 28px rgba(0,0,0,0.4)';
    card.onmouseleave = () => card.style.boxShadow = '0 4px 20px rgba(0,0,0,0.25)';

    card.innerHTML = `
      <!-- Cabeçalho do card -->
      <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:10px; margin-bottom:14px;">
        <div style="display:flex; align-items:center; gap:14px;">
          <div style="width:50px;height:50px;border-radius:12px;background:${cfg.bg};border:2px solid ${cfg.border};display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0;">${cfg.icon}</div>
          <div>
            <div style="font-size:20px; font-weight:800; color:var(--text-primary);">${escapeHtml(e.nome_cliente)}</div>
            <div style="font-size:14px; color:var(--text-secondary); margin-top:2px;">📱 ${escapeHtml(e.telefone_cliente || '-')} &nbsp;|&nbsp; Pedido #${e.id}</div>
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <span style="padding:8px 18px; border-radius:24px; font-size:15px; font-weight:800; background:${cfg.bg}; color:${cfg.color}; border:2px solid ${cfg.border}; letter-spacing:0.5px;">
            ${cfg.icon} ${escapeHtml(e.status)}
          </span>
          <button onclick="openNovaEntregaModal(${e.id})" title="Editar" style="background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.12);color:var(--text-secondary);border-radius:8px;padding:8px 12px;cursor:pointer;font-size:18px;">✏️</button>
        </div>
      </div>

      <!-- Informações em grid -->
      <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:12px;">
        <div style="background:var(--bg-base); border-radius:10px; padding:12px 16px;">
          <div style="font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--text-secondary); margin-bottom:4px;">📦 Peça</div>
          <div style="font-size:16px; font-weight:700;">${escapeHtml(e.nome_peca)}</div>
          <div style="font-size:13px; color:var(--text-secondary);">Tamanho: ${escapeHtml(e.tamanho_peca || '-')}</div>
        </div>
        <div style="background:var(--bg-base); border-radius:10px; padding:12px 16px;">
          <div style="font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--text-secondary); margin-bottom:4px;">📍 Endereço</div>
          <div style="font-size:15px; font-weight:600; line-height:1.4;">${escapeHtml(e.localizacao)}</div>
          ${mapsLink}
        </div>
        <div style="background:var(--bg-base); border-radius:10px; padding:12px 16px;">
          <div style="font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--text-secondary); margin-bottom:8px;">💳 Pagamento</div>
          ${pagHtml}
        </div>
        <div style="background:var(--bg-base); border-radius:10px; padding:12px 16px;">
          <div style="font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--text-secondary); margin-bottom:4px;">👤 Atendente</div>
          <div style="font-size:15px; font-weight:600;">${escapeHtml(e.nome_atendente || '-')}</div>
          <div style="margin-top:6px;font-size:11px;color:var(--text-secondary);">Cód: <span style="font-family:monospace; font-size:16px; font-weight:800; color:var(--text-primary); letter-spacing:2px;">${escapeHtml(e.codigo_verificacao || '-')}</span></div>
        </div>
      </div>
      ${falhaHtml}
    `;
    container.appendChild(card);
  });
}

async function updateEntregaStatus(id, novoStatus) {
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/entregas/${id}/status`, {
      method: 'PUT',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ status: novoStatus })
    });
    if (!res.ok) {
      alert('Erro ao atualizar o status da entrega.');
      loadEntregas(); // recarrega p/ voltar o state
    }
  } catch (e) {
    console.error('Erro ao atualizar status:', e);
  }
}


// ─── Chat Badge ───────────────────────────────────────────────────────────────
function updateChatBadge() {
  const badge = document.getElementById('chatBadge');
  if (!badge) return;
  const visible = getFilteredContacts();
  const totalUnread = visible.reduce((sum, c) => {
    if (currentChat && c.id === currentChat.id) return sum;
    return sum + (c.unread > 0 ? 1 : 0);
  }, 0);
  if (totalUnread > 0) {
    badge.textContent = totalUnread;
    badge.style.display = 'flex';
  } else {
    badge.style.display = 'none';
  }
}

// ─── Chat List ────────────────────────────────────────────────────────────────
function renderChatList(contacts) {
  const list = document.getElementById('chatList');
  list.innerHTML = '';

  updateChatBadge();


  if (!contacts.length) {
    list.innerHTML = `<div style="padding:32px;text-align:center;color:var(--text-muted);font-size:13px;">Nenhuma conversa encontrada</div>`;
    return;
  }

  contacts.forEach(c => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (currentChat?.id === c.id ? ' active' : '');
    item.id = 'chatItem_' + c.id;
    item.onclick = () => openChat(c.id);

    const unreadBadge = c.unread > 0
      ? `<div class="unread-badge">${c.unread}</div>` : '';
    const timeClass = c.unread > 0 ? 'unread' : '';

    // Formata preview — esconde tags internas de áudio
    let preview = c.lastMsg || '';
    if (preview.startsWith('[AUDIO_REF]') || preview.startsWith('[AUDIO]') || preview.startsWith('[AUDIO_LOCAL]')) {
      preview = '🎤 Áudio';
    } else if (preview.startsWith('[IMAGE_REF]')) {
      preview = '🖼️ Imagem';
    } else if (preview.startsWith('[VIDEO_REF]')) {
      preview = '🎥 Vídeo';
    } else if (preview.startsWith('[DOC_REF]') || preview.startsWith('[DOCUMENT_REF]')) {
      preview = '📎 Arquivo';
    } else if (preview.startsWith('[LOCATION_REF]')) {
      preview = '📍 Localização';
    } else if (preview.startsWith('[CONTACT_REF]')) {
      const parts = preview.replace('[CONTACT_REF] ', '').split('|');
      preview = `👤 ${parts[0] || 'Contato'}`;
    }

    // Tags para mostrar na listagem
    let visibleTags = [];
    if (c.tags && c.tags.length > 0) {
        const atendenteTag = c.tags.find(t => t.startsWith('Atendente:'));
        const botTag = c.tags.find(t => t === 'BOT');
        const filialTag = c.tags.find(t => typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:'));
        
        if (atendenteTag) {
            visibleTags.push({ label: atendenteTag.replace('Atendente:', '').trim(), cls: 'tag-orange' });
        } else if (botTag) {
            visibleTags.push({ label: 'BOT', cls: 'tag-purple' });
        }
        
        if (filialTag) {
            visibleTags.push({ label: filialTag, cls: typeof tagColor === 'function' ? tagColor(filialTag) : 'tag-blue' });
        }
        
        const otherTags = c.tags.filter(t => 
             !t.toLowerCase().startsWith('atendente:') && 
             t !== 'BOT' && 
             !(typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:')) &&
             t !== 'Novo Lead' && t !== 'Leads'
        );
        
        otherTags.forEach(other => {
             visibleTags.push({ label: other, cls: typeof tagColor === 'function' ? tagColor(other) : 'tag-gray' });
        });
    }
    
    let tagsHtml = visibleTags.map(t => `<span class="chat-list-tag ${t.cls}">${escapeHtml(t.label)}</span>`).join('');

    item.innerHTML = `
      <div class="chat-item-avatar" style="background:${avatarColor(c.name)}">${c.avatar}</div>
      <div class="chat-item-body">
        <div class="chat-item-top">
          <span class="chat-item-name">${c.name}</span>
          <div style="display:flex; align-items:center; gap:6px; flex-shrink:0;">
             ${tagsHtml}
             <span class="chat-item-time ${timeClass}">${c.time}</span>
          </div>
        </div>
        <span class="chat-item-preview">${preview}</span>
      </div>
      <div class="chat-item-meta">${unreadBadge}</div>
    `;
    list.appendChild(item);
  });
}

// ─── Open Chat ────────────────────────────────────────────────────────────────
async function openChat(id) {
  const contact = CONTACTS.find(c => c.id === id);
  if (!contact) return;
  
  currentChat = contact;
  contact.unread = 0;
  updateChatBadge();
  
  // Limpa o alerta de mensagens não lidas no backend
  const token = localStorage.getItem('wp_crm_token');
  fetch(`${API_URL}/api/contacts/${id}/read`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}` }
  }).catch(e => console.error('Erro ao marcar chat como lido:', e));
  
  // Update chat list active state
  document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
  document.getElementById('chatItem_' + id)?.classList.add('active');

  // UI toggle
  document.getElementById('chatEmpty').style.display = 'none';
  document.getElementById('chatInterface').style.display = 'flex';
  document.getElementById('app').classList.add('chat-active');
  
  // Show mobile back btn selectively using css media query or just let css handle
  const mobileBackBtn = document.querySelector('.mobile-back-btn');
  if (mobileBackBtn) {
    mobileBackBtn.style.display = window.innerWidth <= 768 ? 'flex' : 'none';
  }
  
  // Header
  document.getElementById('chatName').textContent = contact.name;
  document.getElementById('chatAvatar').textContent = contact.avatar || contact.name[0];
  document.getElementById('chatAvatar').style.background = avatarColor(contact.name);
  document.getElementById('chatStatus').textContent = (contact.instanceName || contact.instance) + ' · Online';
  
  // Sidebar info
  document.getElementById('detailsName').textContent = contact.name;
  document.getElementById('detailsAvatar').textContent = contact.avatar || contact.name[0];
  document.getElementById('detailsAvatar').style.background = avatarColor(contact.name);
  document.getElementById('detailsPhone').textContent = contact.phone;
  document.getElementById('detailsInstance').textContent = contact.instanceName || contact.instance;

  // Carrega mensagens da API
  try {
    const res = await fetch(`${API_URL}/api/contacts/${contact.id}/messages`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (res.ok) {
      const apiMessages = await res.json();
      // Merge: keep any socket-received messages not yet in API response
      const existingMessages = contact.messages || [];
      const apiIdSet = new Set(apiMessages.map(m => m.id));
      const socketOnly = existingMessages.filter(m => m.id && !apiIdSet.has(m.id));
      contact.messages = apiMessages.concat(socketOnly);
    }
  } catch (e) {
    console.error('Erro ao carregar mensagens:', e);
  }

  // Garante que mensagens seja um array
  contact.messages = contact.messages || [];
  renderMessages(contact.messages);
  renderChatList(getFilteredContacts());

  // Scroll to bottom
  const area = document.getElementById('messagesArea');
  area.scrollTop = area.scrollHeight;

  // Update sidebar details
  updateContactDetails(contact);

  // Update attendance bar
  updateAttendanceBar(contact);
}

function closeChatMobile() {
  document.getElementById('app').classList.remove('chat-active');
  currentChat = null;
  document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
}

// ─── Messages ─────────────────────────────────────────────────────────────────
function renderMessages(messages) {
  const area = document.getElementById('messagesArea');
  area.innerHTML = `<div class="date-divider"><span>Hoje</span></div>`;

  messages.forEach(msg => {
    const isBot = msg.id && String(msg.id).startsWith('bot_');
    const el = document.createElement('div');
    el.className = `message ${msg.type === 'in' ? 'incoming' : 'outgoing'} ${isBot ? 'bot-message' : ''}`;

    let ticks = '';
    if (msg.type === 'out') {
      const ack = msg.ack !== undefined ? msg.ack : 2; // Padrão antigo 2
      const iconClock = `<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor"><path d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8z"></path><path d="M7.5 4.5v3.65l2.6 1.5.75-1.3-1.85-1.1V4.5h-1.5z"></path></svg>`;
      const iconSent = `<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M13.65 3.35a.75.75 0 010 1.06l-7.5 7.5a.75.75 0 01-1.06 0l-3.5-3.5a.75.75 0 111.06-1.06l2.97 2.97 6.97-6.97a.75.75 0 011.06 0z"></path></svg>`;
      const iconDelivered = `<svg viewBox="0 0 20 16" width="16" height="13" fill="currentColor"><path d="M19.65 3.35a.75.75 0 010 1.06l-7.5 7.5a.75.75 0 01-1.06 0l-3.5-3.5a.75.75 0 111.06-1.06l2.97 2.97 6.97-6.97a.75.75 0 011.06 0z"></path><path d="M14.65 3.35a.75.75 0 010 1.06l-2.5 2.5a.75.75 0 11-1.06-1.06l2.5-2.5a.75.75 0 011.06 0zM5.35 11.85a.75.75 0 101.06-1.06L3.91 8.29a.75.75 0 10-1.06 1.06l2.5 2.5z"></path></svg>`;
      const iconError = `<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/><path d="M7.002 11a1 1 0 1 1 2 0 1 1 0 0 1-2 0zM7.1 4.995a.905.905 0 1 1 1.8 0l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 4.995z"/></svg>`;
      
      if (ack === 0) {
        ticks = `<span class="msg-ticks" style="color: #999; margin-left:4px; display:inline-flex; align-items:center;" title="Pendente">${iconClock}</span>`;
      } else if (ack === 1) {
        ticks = `<span class="msg-ticks" style="color: #999; margin-left:4px; display:inline-flex; align-items:center;" title="Enviado">${iconSent}</span>`;
      } else if (ack === 2) {
        ticks = `<span class="msg-ticks" style="color: #999; margin-left:4px; display:inline-flex; align-items:center;" title="Entregue">${iconDelivered}</span>`;
      } else if (ack === 3 || ack === 4) {
        ticks = `<span class="msg-ticks" style="color: #34B7F1; margin-left:4px; display:inline-flex; align-items:center;" title="Lido">${iconDelivered}</span>`;
      } else if (ack === -1) {
        ticks = `<span class="msg-ticks" style="color: #f44336; margin-left:4px; display:inline-flex; align-items:center;" title="Erro">${iconError}</span>`;
      } else {
        ticks = `<span class="msg-ticks" style="color: #999; margin-left:4px; display:inline-flex; align-items:center;">${iconDelivered}</span>`;
      }
    }

    const botLabel = isBot ? `<div class="bot-label">🤖 Respondido pelo Bot</div>` : '';

    let messageContent = msg.text ? String(msg.text) : "";
    let isDeletedMsg = false;
    const authToken = localStorage.getItem('wp_crm_token');
    
    // Parse [REPLY:id|text]
    let replyBlock = '';
    const replyMatch = messageContent.match(/^\[REPLY:([^|]+)\|([^\]]+)\]\n/);
    if (replyMatch) {
        const replyText = replyMatch[2];
        replyBlock = `<div class="msg-reply-block">
            <div class="msg-reply-text">${escapeHtml(replyText)}</div>
        </div>`;
        messageContent = messageContent.replace(replyMatch[0], '');
    } else if (messageContent === '[MENSAGEM_APAGADA]') {
        messageContent = '<div class="msg-deleted"><svg viewBox="0 0 16 16" width="14" height="14" style="margin-right:4px;"><path fill="currentColor" d="M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM0 8a8 8 0 1116 0A8 8 0 010 8z"></path><path fill="currentColor" d="M10.854 5.146a.5.5 0 00-.708 0L8 7.293 5.854 5.146a.5.5 0 10-.708.708L7.293 8l-2.147 2.146a.5.5 0 00.708.708L8 8.707l2.146 2.147a.5.5 0 00.708-.708L8.707 8l2.147-2.146a.5.5 0 000-.708z"></path></svg> <em>Esta mensagem foi apagada</em></div>';
        replyBlock = '';
        ticks = '';
        isDeletedMsg = true;
    }
    
    if (messageContent.startsWith('[LOCATION_REF] ')) {
        const ref = messageContent.replace('[LOCATION_REF] ', '');
        const parts = ref.split('|');
        const lat = parts[0];
        const lng = parts[1];
        const name = parts[2] || 'Localização';
        const address = parts[3] || '';
        const mapsUrl = `https://maps.google.com/?q=${lat},${lng}`;
        messageContent = `<a href="${mapsUrl}" target="_blank" class="msg-doc" style="display: flex; align-items: center; gap: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; text-decoration: none; color: inherit;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/></svg>
            <div style="display:flex; flex-direction:column; text-align:left;">
                <strong style="font-size:14px; max-width:200px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(name)}</strong>
                <span style="font-size:11px; opacity:0.8; max-width:200px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(address)}</span>
            </div>
        </a>`;
    } else if (messageContent.startsWith('[IMAGE_REF] ')) {
        const ref = messageContent.replace('[IMAGE_REF] ', '');
        const [imgInstance, imgMsgId] = ref.split('\n')[0].split('|');
        const caption = ref.split('\n')[1] || '';
        const imgSrc = `${API_URL}/api/media/image?instance=${encodeURIComponent(imgInstance)}&msg_id=${encodeURIComponent(imgMsgId)}&token=${encodeURIComponent(authToken)}`;
        messageContent = `<img src="${imgSrc}" class="msg-image" alt="Imagem" onclick="openLightbox(this.src)" />`;
        if (caption) {
            messageContent += `<div class="msg-caption">${escapeHtml(caption)}</div>`;
        }
    } else if (messageContent.startsWith('[VIDEO_REF] ')) {
        const ref = messageContent.replace('[VIDEO_REF] ', '');
        const [vidInstance, vidMsgId] = ref.split('\n')[0].split('|');
        const caption = ref.split('\n')[1] || '';
        const vidSrc = `${API_URL}/api/media/video?instance=${encodeURIComponent(vidInstance)}&msg_id=${encodeURIComponent(vidMsgId)}&token=${encodeURIComponent(authToken)}`;
        messageContent = `<video controls class="msg-video"><source src="${vidSrc}" type="video/mp4">Seu navegador não suporta vídeo.</video>`;
        if (caption) {
            messageContent += `<div class="msg-caption">${escapeHtml(caption)}</div>`;
        }
    } else if (messageContent.startsWith('[DOC_UPLOADING] ')) {
        // Estado temporário enquanto o arquivo está sendo enviado ao servidor
        const docName = messageContent.replace('[DOC_UPLOADING] ', '');
        messageContent = `<div class="msg-doc" style="display: flex; align-items: center; gap: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; color: inherit; opacity: 0.6;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>
            <span>${escapeHtml(docName)} <em style="font-size:11px;">(Enviando...)</em></span>
        </div>`;
    } else if (messageContent.startsWith('[DOC_REF] ')) {
        const ref = messageContent.replace('[DOC_REF] ', '');
        const parts = ref.split('|');
        const docInstance = parts[0];
        const docMsgId = parts[1];
        const docName = parts[2] || 'Arquivo';
        const docSrc = `${API_URL}/api/media/document?instance=${encodeURIComponent(docInstance)}&msg_id=${encodeURIComponent(docMsgId)}&filename=${encodeURIComponent(docName)}&token=${encodeURIComponent(authToken)}`;
        messageContent = `<a href="${docSrc}" target="_blank" class="msg-doc" style="display: flex; align-items: center; gap: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; text-decoration: none; color: inherit;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>
            <span>${escapeHtml(docName)}</span>
        </a>`;
    } else if (messageContent.startsWith('[DOCUMENT_REF] ')) {
        // Fallback para documentos enviados com formato antigo (sem instance|msg_id)
        const docName = messageContent.replace('[DOCUMENT_REF] ', '');
        messageContent = `<div class="msg-doc" style="display: flex; align-items: center; gap: 8px; padding: 10px; background: rgba(0,0,0,0.2); border-radius: 8px; color: inherit;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6zm4 18H6V4h7v5h5v11z"/></svg>
            <span>${escapeHtml(docName)}</span>
        </div>`;
    } else if (messageContent.startsWith('[IMAGE_LOCAL] ')) {
        const localSrc = messageContent.replace('[IMAGE_LOCAL] ', '');
        messageContent = `<img src="${localSrc}" class="msg-image" alt="Imagem" onclick="openLightbox(this.src)" />`;
    } else if (messageContent.startsWith('[VIDEO_LOCAL] ')) {
        const localSrc = messageContent.replace('[VIDEO_LOCAL] ', '');
        messageContent = `<video controls class="msg-video"><source src="${localSrc}" type="video/mp4">Seu navegador não suporta vídeo.</video>`;
    } else if (messageContent.startsWith('[IMAGE_SENT] ')) {
        const ref = messageContent.replace('[IMAGE_SENT] ', '');
        const [imgInstance, imgMsgId] = ref.split('|');
        const imgSrc = `${API_URL}/api/media/image?instance=${encodeURIComponent(imgInstance)}&msg_id=${encodeURIComponent(imgMsgId)}&token=${encodeURIComponent(authToken)}`;
        messageContent = `<img src="${imgSrc}" class="msg-image" alt="Imagem" onclick="openLightbox(this.src)" />`;
    } else if (messageContent.startsWith('[VIDEO_SENT] ')) {
        const ref = messageContent.replace('[VIDEO_SENT] ', '');
        const [vidInstance, vidMsgId] = ref.split('|');
        const vidSrc = `${API_URL}/api/media/video?instance=${encodeURIComponent(vidInstance)}&msg_id=${encodeURIComponent(vidMsgId)}&token=${encodeURIComponent(authToken)}`;
        messageContent = `<video controls class="msg-video"><source src="${vidSrc}" type="video/mp4">Seu navegador não suporta vídeo.</video>`;
    } else if (messageContent.startsWith('[AUDIO_REF] ')) {
        const ref = messageContent.replace('[AUDIO_REF] ', '');
        const [audioInstance, audioMsgId] = ref.split('|');
        const audioSrc = `${API_URL}/api/media/audio?instance=${encodeURIComponent(audioInstance)}&msg_id=${encodeURIComponent(audioMsgId)}&token=${encodeURIComponent(authToken)}`;
        const avatarLetter = (currentChat?.name || '?')[0].toUpperCase();
        const isOut = msg.type === 'out';
        messageContent = buildWaAudioHTML(audioSrc, avatarLetter, isOut);
    } else if (messageContent.startsWith('[AUDIO_LOCAL] ')) {
        // Áudio gravado localmente — usa data URL diretamente
        const localSrc = messageContent.replace('[AUDIO_LOCAL] ', '');
        const avatarLetter = (currentChat?.name || '?')[0].toUpperCase();
        messageContent = buildWaAudioHTML(localSrc, avatarLetter, true);
    } else if (messageContent.startsWith('[AUDIO] ')) {
        const rawSrc = messageContent.replace('[AUDIO] ', '');
        if (rawSrc.includes('mmg.whatsapp.net') || rawSrc.includes('.enc')) {
            const avatarLetter = (currentChat?.name || '?')[0].toUpperCase();
            const isOut = msg.type === 'out';
            messageContent = buildWaAudioHTML(null, avatarLetter, isOut);
        } else {
            const avatarLetter = (currentChat?.name || '?')[0].toUpperCase();
            const isOut = msg.type === 'out';
            messageContent = buildWaAudioHTML(rawSrc, avatarLetter, isOut);
        }
    } else if (messageContent.startsWith('[CONTACT_REF] ')) {
        const ref = messageContent.replace('[CONTACT_REF] ', '');
        const parts = ref.split('|');
        const contactName = parts[0] || 'Contato';
        const contactPhone = parts[1] || '';
        const cleanNumber = contactPhone ? contactPhone.replace(/[^\d]/g, '') : null;
        messageContent = `
            <div style="display:flex; align-items:center; gap:12px; padding:12px 14px; background:rgba(255,255,255,0.07); border-radius:12px; border:1px solid rgba(255,255,255,0.12); min-width:200px; max-width:280px;">
                <div style="width:44px; height:44px; border-radius:50%; background:linear-gradient(135deg,#25D366,#128C7E); display:flex; align-items:center; justify-content:center; flex-shrink:0;">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="white"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg>
                </div>
                <div style="flex:1; min-width:0;">
                    <div style="font-weight:600; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(contactName)}</div>
                    ${contactPhone ? `<div style="font-size:12px; opacity:0.7; margin-top:2px;">${escapeHtml(contactPhone)}</div>` : ''}
                </div>
                ${cleanNumber ? `
                <button onclick="showNewChatWithNumber('${cleanNumber}')" title="Iniciar Nova Conversa no Sistema"
                   style="display:flex; align-items:center; justify-content:center; width:32px; height:32px; border-radius:50%; border:none; background:rgba(37,211,102,0.2); color:#25D366; cursor:pointer; flex-shrink:0; transition:background 0.2s;"
                   onmouseover="this.style.background='rgba(37,211,102,0.4)'" onmouseout="this.style.background='rgba(37,211,102,0.2)'">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path><line x1="12" y1="9" x2="12" y2="15"></line><line x1="9" y1="12" x2="15" y2="12"></line></svg>
                </button>` : ''}
            </div>`;
    } else if (!isDeletedMsg) {
        messageContent = escapeHtml(messageContent).replace(/\n/g, '<br>').replace(/\*([^*\n]+)\*/g, '<strong>$1</strong>');
    }

    el.innerHTML = `
      <div class="msg-bubble">
        <div class="msg-actions" onclick="toggleMsgMenu(event, '${msg.id}')">
          <svg viewBox="0 0 19 20" width="19" height="20"><path fill="currentColor" d="M3.8 6.7l5.7 5.7 5.7-5.7 1.6 1.6-7.3 7.2-7.3-7.2 1.6-1.6z"></path></svg>
        </div>
        ${botLabel}
        ${replyBlock}
        ${messageContent}
        <div class="msg-meta">
          <span class="msg-time">${msg.time}</span>
          ${ticks}
        </div>
      </div>
    `;
    area.appendChild(el);
  });

  area.scrollTop = area.scrollHeight;

  // Ativar todos os players de áudio na área
  area.querySelectorAll('.wa-audio-player[data-src]').forEach(initWaPlayer);
  area.querySelectorAll('.wa-audio-player[data-expired]').forEach(p => {
    p.querySelector('.wa-play-btn').disabled = true;
    p.querySelector('.wa-duration').textContent = 'Expirado';
  });
}

// Gera HTML do player estilo WhatsApp
function buildWaAudioHTML(src, avatarLetter, isOut) {
  const bars = Array.from({length: 30}, (_, i) => {
    const h = 6 + Math.round(Math.abs(Math.sin(i * 0.8)) * 18);
    return `<div class="wa-bar" style="height:${h}px" data-idx="${i}"></div>`;
  }).join('');

  const dataAttr = src ? `data-src="${src}"` : `data-expired="1"`;

  return `
    <div class="wa-audio-player" ${dataAttr}>
      <div class="wa-audio-avatar">${avatarLetter}</div>
      <button class="wa-play-btn" aria-label="Play">
        <svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <div class="wa-audio-body">
        <div class="wa-waveform">${bars}</div>
        <div class="wa-audio-footer">
          <span class="wa-duration">0:00</span>
          <span class="wa-mic-icon">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M12 15c1.66 0 3-1.34 3-3V6c0-1.66-1.34-3-3-3S9 4.34 9 6v6c0 1.66 1.34 3 3 3zm-1-9c0-.55.45-1 1-1s1 .45 1 1v6c0 .55-.45 1-1 1s-1-.45-1-1V6zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-2.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>
          </span>
        </div>
      </div>
    </div>
  `;
}

// Inicializa um player de áudio customizado
const _waAudioInstances = [];
function initWaPlayer(playerEl) {
  if (playerEl._initialized) return;
  playerEl._initialized = true;

  const src = playerEl.dataset.src;
  const audio = new Audio(src);
  const playBtn = playerEl.querySelector('.wa-play-btn');
  const durationEl = playerEl.querySelector('.wa-duration');
  const bars = playerEl.querySelectorAll('.wa-bar');
  const waveform = playerEl.querySelector('.wa-waveform');
  const totalBars = bars.length;

  _waAudioInstances.push(audio);

  function formatTime(s) {
    if (!isFinite(s)) return '0:00';
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60).toString().padStart(2, '0');
    return `${m}:${sec}`;
  }

  function updateBars() {
    if (!audio.duration) return;
    const progress = audio.currentTime / audio.duration;
    const played = Math.floor(progress * totalBars);
    bars.forEach((b, i) => b.classList.toggle('played', i < played));
    durationEl.textContent = formatTime(audio.currentTime);
  }

  // Clique na waveform para seek
  waveform.addEventListener('click', e => {
    if (!audio.duration) return;
    const rect = waveform.getBoundingClientRect();
    const ratio = (e.clientX - rect.left) / rect.width;
    audio.currentTime = ratio * audio.duration;
    updateBars();
  });

  audio.addEventListener('loadedmetadata', () => {
    durationEl.textContent = formatTime(audio.duration);
  });

  audio.addEventListener('timeupdate', updateBars);

  audio.addEventListener('ended', () => {
    playerEl.classList.remove('playing');
    playBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
    bars.forEach(b => b.classList.remove('played'));
    durationEl.textContent = formatTime(audio.duration);
  });

  playBtn.addEventListener('click', () => {
    const isPlaying = !audio.paused;
    // Pausar todos os outros players
    _waAudioInstances.forEach(a => { if (a !== audio) a.pause(); });
    document.querySelectorAll('.wa-audio-player.playing').forEach(p => {
      if (p !== playerEl) {
        p.classList.remove('playing');
        p.querySelector('.wa-play-btn').innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
      }
    });

    if (isPlaying) {
      audio.pause();
      playerEl.classList.remove('playing');
      playBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
    } else {
      audio.play().catch(() => {});
      playerEl.classList.add('playing');
      playBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;
    }
  });
}

async function sendMessage() {
  const textarea = document.getElementById('messageInput');
  const text = textarea.value.trim();
  if (!text || !currentChat) return;

  const now = new Date();
  const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;

  // ── Monta o prefixo com o primeiro nome do atendente ──
  let textToSend = text;
  try {
    const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
    const fullName = userData.name || '';
    const firstName = fullName.split(' ')[0]; // Pega apenas o primeiro nome
    if (firstName) {
      textToSend = `*${firstName}:*\n${text}`;
    }
  } catch (e) {
    console.warn('[sendMessage] Nao foi possivel obter nome do atendente:', e);
  }

  // 1. Atualiza Localmente (Optimistic Update) — exibe texto original sem prefixo
  const tempId = 'temp_' + Date.now();
  let optimisticText = textToSend;
  if (window.replyingToMsgId) {
      optimisticText = `[REPLY:${window.replyingToMsgId}|${window.replyingToMsgText}]\n${textToSend}`;
  }
  const newMsg = { id: tempId, text: optimisticText, type: 'out', time };
  if (!currentChat.messages) currentChat.messages = [];
  currentChat.messages.push(newMsg);
  currentChat.lastMsg = text;
  currentChat.time = time;

  renderMessages(currentChat.messages);
  renderChatList(getFilteredContacts());
  textarea.value = '';
  autoResize(textarea);
  updateSendBtn();
  // Garante scroll para o final apos renderizacao no DOM
  requestAnimationFrame(() => {
    const area = document.getElementById('messagesArea');
    if (area) area.scrollTop = area.scrollHeight;
  });

  // 2. Envia para o Backend (com prefixo do atendente)
  try {
    // Sanatiza o numero (remove +, space, -, etc)
    const cleanNumber = currentChat.phone.replace(/\D/g, '');
    
    // Se a instância for mock (inst1, inst2...), tenta pegar uma real
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
       // Busca primeiro nome de instância real que o usuário tem
       targetInstance = getDefaultInstance();
       if (!targetInstance) {
         throw new Error('Nenhuma instância do WhatsApp vinculada a este usuário.');
       }
    }

    const response = await fetch(`${API_URL}/api/whatsapp/send`, {
      method: 'POST',
      headers: { 
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
      },
      body: JSON.stringify({
        instance: targetInstance,
        number: cleanNumber,
        text: textToSend,  // Envia com o prefixo *Nome:*
        reply_to: window.replyingToMsgId || null
      })
    });

    const data = await response.json();
    if (!response.ok) {
      // If chat is locked by another attendant, remove optimistic message
      if (response.status === 403) {
        const idx = currentChat.messages.indexOf(newMsg);
        if (idx > -1) currentChat.messages.splice(idx, 1);
        renderMessages(currentChat.messages);
      }
      throw new Error(data.error || 'Falha ao enviar');
    }
    
    // Update the temporary ID with the real ID from the backend to avoid duplicate from webhook
    let realId = data.key?.id || data.messageId || data.id;
    if (typeof realId === 'object' && realId !== null) {
      realId = realId.id || realId._serialized || realId;
    }
    if (realId && typeof realId === 'string') {
      newMsg.id = realId;
    }
    
    console.log('Mensagem enviada via WAHA:', targetInstance);
    
    // Limpa o estado de reply após envio
    cancelReply();
  } catch (err) {
    console.error('Erro ao enviar mensagem:', err);
    showToast(`Erro ao enviar: ${err.message}`);
  }
}

// ─── Ações de Mensagem (Menu de Contexto, Responder, Apagar) ───────────────────
function toggleMsgMenu(event, msgId) {
    event.stopPropagation();
    
    const oldMenu = document.getElementById('msg-context-menu');
    if (oldMenu) oldMenu.remove();
    
    const menu = document.createElement('div');
    menu.id = 'msg-context-menu';
    menu.className = 'msg-context-menu';
    menu.innerHTML = `
        <div class="msg-menu-item" onclick="replyToMsg('${msgId}')">Responder</div>
        <div class="msg-menu-item delete" onclick="deleteMessage('${msgId}')">Apagar</div>
    `;
    
    document.body.appendChild(menu);
    
    const rect = event.currentTarget.getBoundingClientRect();
    menu.style.top = `${rect.bottom + window.scrollY}px`;
    menu.style.left = `${rect.left + window.scrollX - 120}px`;
    
    setTimeout(() => {
        const closeMenu = (e) => {
            if (!menu.contains(e.target)) {
                menu.remove();
                document.removeEventListener('click', closeMenu);
            }
        };
        document.addEventListener('click', closeMenu);
    }, 0);
}

function replyToMsg(msgId) {
    const msg = currentChat.messages.find(m => m.id === msgId);
    if (!msg) return;
    
    window.replyingToMsgId = msgId;
    window.replyingToMsgText = (msg.text || '').replace(/\n/g, ' ').substring(0, 50);
    
    const replyBar = document.getElementById('reply-preview-bar');
    const replyText = document.getElementById('reply-preview-text');
    if (replyBar && replyText) {
        replyText.textContent = window.replyingToMsgText;
        replyBar.style.display = 'flex';
    }
    
    document.getElementById('messageInput').focus();
    
    const oldMenu = document.getElementById('msg-context-menu');
    if (oldMenu) oldMenu.remove();
}

function cancelReply() {
    window.replyingToMsgId = null;
    window.replyingToMsgText = null;
    const replyBar = document.getElementById('reply-preview-bar');
    if (replyBar) replyBar.style.display = 'none';
}

async function deleteMessage(msgId) {
    const oldMenu = document.getElementById('msg-context-menu');
    if (oldMenu) oldMenu.remove();

    if (!confirm('Tem certeza que deseja apagar esta mensagem para todos?')) return;
    
    const msg = currentChat.messages.find(m => m.id === msgId);
    const originalText = msg ? msg.text : '';
    if (msg) {
        msg.text = '[MENSAGEM_APAGADA]';
        renderMessages(currentChat.messages);
    }
    
    try {
        const cleanNumber = currentChat.phone.replace(/\D/g, '');
        const response = await fetch(`${API_URL}/api/whatsapp/delete-message`, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
            },
            body: JSON.stringify({
                instance: currentChat.instance,
                number: cleanNumber,
                message_id: msgId
            })
        });
        
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || 'Falha ao apagar');
        }
    } catch (err) {
        console.error('Erro ao deletar mensagem:', err);
        showToast(`Erro ao apagar: ${err.message}`);
        if (msg) {
            msg.text = originalText;
            renderMessages(currentChat.messages);
        }
    }
}

function handleEnter(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ─── Send / Mic Button Toggle ─────────────────────────────────────────────────
function updateSendBtn() {
  const text = document.getElementById('messageInput')?.value.trim();
  const micIcon = document.getElementById('micIcon');
  const sendIcon = document.getElementById('sendIcon');
  if (!micIcon || !sendIcon) return;
  if (text) {
    micIcon.style.display = 'none';
    sendIcon.style.display = 'block';
  } else {
    micIcon.style.display = 'block';
    sendIcon.style.display = 'none';
  }
}

function handleSendOrMic() {
  const text = document.getElementById('messageInput')?.value.trim();
  if (text) {
    sendMessage();
  } else {
    startRecording();
  }
}

// ─── Audio Recording ──────────────────────────────────────────────────────────
let _mediaRecorder = null;
let _audioChunks = [];
let _recTimerInterval = null;
let _recSeconds = 0;
let _recAnalyser = null;
let _recAnimFrame = null;
let _recStream = null;

async function startRecording() {
  if (!currentChat) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    _recStream = stream;

    // Setup analyser for live waveform
    const audioCtx = new AudioContext();
    const source = audioCtx.createMediaStreamSource(stream);
    _recAnalyser = audioCtx.createAnalyser();
    _recAnalyser.fftSize = 256;
    source.connect(_recAnalyser);

    // Start MediaRecorder
    const mimeType = MediaRecorder.isTypeSupported('audio/ogg; codecs=opus')
      ? 'audio/ogg; codecs=opus'
      : MediaRecorder.isTypeSupported('audio/webm; codecs=opus')
        ? 'audio/webm; codecs=opus'
        : 'audio/webm';

    _mediaRecorder = new MediaRecorder(stream, { mimeType });
    _audioChunks = [];

    _mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) _audioChunks.push(e.data);
    };

    _mediaRecorder.onstop = async () => {
      clearInterval(_recTimerInterval);
      cancelAnimationFrame(_recAnimFrame);
      stream.getTracks().forEach(t => t.stop());

      if (_audioChunks.length === 0) return; // cancelled

      const blob = new Blob(_audioChunks, { type: mimeType });
      const reader = new FileReader();
      reader.onloadend = () => {
        const base64 = reader.result; // data:audio/ogg;base64,...
        sendAudioMessage(base64);
      };
      reader.readAsDataURL(blob);
    };

    _mediaRecorder.start(250); // collect chunks every 250ms

    // Show recording bar, hide input bar
    document.getElementById('recordingBar').style.display = 'flex';
    document.getElementById('inputBar').style.display = 'none';

    // Timer
    _recSeconds = 0;
    document.getElementById('recTimer').textContent = '0:00';
    _recTimerInterval = setInterval(() => {
      _recSeconds++;
      const m = Math.floor(_recSeconds / 60);
      const s = (_recSeconds % 60).toString().padStart(2, '0');
      document.getElementById('recTimer').textContent = `${m}:${s}`;
    }, 1000);

    // Live waveform
    drawRecWaveform();

  } catch (err) {
    console.error('Erro ao acessar microfone:', err);
    showToast('Permissão de microfone negada');
  }
}

function drawRecWaveform() {
  if (!_recAnalyser) return;
  const container = document.getElementById('recWaveform');
  const dataArray = new Uint8Array(_recAnalyser.frequencyBinCount);

  function draw() {
    _recAnimFrame = requestAnimationFrame(draw);
    _recAnalyser.getByteFrequencyData(dataArray);

    // Build bars
    const numBars = 40;
    const step = Math.floor(dataArray.length / numBars);
    let html = '';
    for (let i = 0; i < numBars; i++) {
      const val = dataArray[i * step] || 0;
      const h = Math.max(4, Math.round((val / 255) * 28));
      html += `<div class="rec-bar" style="height:${h}px"></div>`;
    }
    container.innerHTML = html;
  }
  draw();
}

function cancelRecording() {
  if (_mediaRecorder && _mediaRecorder.state !== 'inactive') {
    _audioChunks = []; // mark as cancelled
    _mediaRecorder.stop();
  }
  clearInterval(_recTimerInterval);
  cancelAnimationFrame(_recAnimFrame);
  if (_recStream) _recStream.getTracks().forEach(t => t.stop());

  // Restore UI
  document.getElementById('recordingBar').style.display = 'none';
  document.getElementById('inputBar').style.display = 'flex';
}

function stopRecording(cancel) {
  if (cancel) {
    cancelRecording();
    return;
  }
  if (_mediaRecorder && _mediaRecorder.state !== 'inactive') {
    _mediaRecorder.stop();
  }
  // Restore UI
  document.getElementById('recordingBar').style.display = 'none';
  document.getElementById('inputBar').style.display = 'flex';
}

async function sendAudioMessage(base64Data) {
  if (!currentChat) return;

  const now = new Date();
  const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;

  // Optimistic update — show local player immediately using base64 data URL
  const tempId = 'audio_temp_' + Date.now();
  const tempText = `[AUDIO_LOCAL] ${base64Data}`;
  const newMsg = { id: tempId, text: tempText, type: 'out', time };
  if (!currentChat.messages) currentChat.messages = [];
  currentChat.messages.push(newMsg);
  currentChat.lastMsg = '🎤 Áudio';
  currentChat.time = time;
  renderMessages(currentChat.messages);
  renderChatList(getFilteredContacts());

  // Send to backend
  try {
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
      targetInstance = getDefaultInstance();
      if (!targetInstance) {
        throw new Error('Nenhuma instância vinculada.');
      }
    }

    const cleanNumber = currentChat.phone.replace(/\D/g, '');

    const response = await fetch(`${API_URL}/api/whatsapp/send-audio`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
      },
      body: JSON.stringify({
        instance: targetInstance,
        number: cleanNumber,
        audio: base64Data
      })
    });

    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Falha ao enviar áudio');

    // Update the temp ID with real ID (to prevent future duplication)
    const realId = data.msg_id || data.key?.id;
    if (realId) {
      newMsg.id = realId;
      // KEEP [AUDIO_LOCAL] — the base64 data plays correctly in the browser
      // Don't switch to [AUDIO_REF] because the server proxy may not have it yet
      _pendingAudioIds.add(realId); // Mark to skip socket duplicate
    }

    console.log('Áudio enviado com sucesso!');
  } catch (err) {
    console.error('Erro ao enviar áudio:', err);
    showToast(`Erro ao enviar áudio: ${err.message}`);
  }
}

// ─── Contact Details Sidebar ──────────────────────────────────────────────────
function updateContactDetails(contact) {
  document.getElementById('detailsAvatar').textContent = contact.avatar;
  document.getElementById('detailsAvatar').style.background = avatarColor(contact.name);
  document.getElementById('detailsName').textContent = contact.name;
  document.getElementById('detailsPhone').textContent = contact.phone;
  document.getElementById('detailsInstance').textContent = contact.instanceName;

  // Update attendant info
  const detailsAgent = document.getElementById('detailsAgent');
  if (detailsAgent) {
    const user = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
    if (contact.assigned_name) {
      detailsAgent.textContent = contact.assigned_to === user.id ? 'Você' : contact.assigned_name;
    } else {
      detailsAgent.textContent = 'Nenhum';
    }
  }

  // Update contact status
  const detailsStatus = document.getElementById('detailsContactStatus');
  if (detailsStatus) {
    detailsStatus.textContent = contact.assigned_to ? 'Em atendimento' : 'Aberto';
  }

  const tagsArea = document.getElementById('tagsArea');
  tagsArea.innerHTML = '';
  const currentUser = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
  const isAdmin = currentUser.role === 'admin';
  contact.tags.forEach(tag => {
    const el = document.createElement('div');
    el.className = 'tag ' + tagColor(tag);
    if (isAdmin) {
      el.innerHTML = `<span>${escapeHtml(tag)}</span><span style="cursor:pointer;margin-left:6px;opacity:0.7;font-weight:bold" onclick="removeTag('${tag.replace(/'/g, "\\'")}')" title="Remover etiqueta">✕</span>`;
    } else {
      el.innerHTML = `<span>${escapeHtml(tag)}</span>`;
    }
    tagsArea.appendChild(el);
  });
  if (isAdmin) {
    const addBtn = document.createElement('button');
    addBtn.className = 'tag tag-add';
    addBtn.textContent = '+ Adicionar';
    addBtn.onclick = addTag;
    tagsArea.appendChild(addBtn);
  }
}

function toggleSidebar() {
  sidebarOpen = !sidebarOpen;
  document.getElementById('sidebarDetails').style.display = sidebarOpen ? '' : 'none';
}

// ─── Instances Modal ──────────────────────────────────────────────────────────
async function openInstancesModal() {
  document.getElementById('instancesModal').style.display = 'flex';
  // Reset nav since instances uses modal, not panel view
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('navChats')?.classList.add('active');

  const list = document.getElementById('instancesList');
  list.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">Carregando instncias...</div>`;

  try {
    const response = await fetch(`${API_URL}/api/whatsapp/instances`, {
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    });
    const instances = await response.json();
    renderInstances(instances);
  } catch (err) {
    console.error('Erro ao buscar instncias:', err);
    list.innerHTML = `<div style="padding:20px;text-align:center;color:var(--error)">Falha ao conectar na WAHA API via Backend.</div>`;
  }
}

function renderInstances(instancesData = INSTANCES) {
  const list = document.getElementById('instancesList');
  list.innerHTML = '';

  instancesData.forEach(inst => {
    const row = document.createElement('div');
    row.className = 'instance-row';
    const isConn = inst.status === 'connected' || inst.connectionStatus === 'open';
    const name = inst.name || inst.instanceName;
    const phone = inst.phone || inst.owner || 'Sem nmero';

    row.innerHTML = `
      <div class="instance-row-info">
        <div class="chip-dot ${isConn ? 'connected' : 'disconnected'}"></div>
        <div>
          <h4>${name}</h4>
          <p>${phone} · ${isConn ? 'Conectado' : 'Desconectado'}</p>
        </div>
      </div>
      <div class="instance-row-actions">
        <button class="btn-connect ${isConn ? 'connected' : 'disconnected'}" onclick="toggleInstance('${inst.id || name}')">
          ${isConn ? 'Desconectar' : 'Conectar'}
        </button>
      </div>
    `;
    list.appendChild(row);
  });

  // Atualiza também os chips de filtro se estiver no Dashboard
  renderInstanceSelector(instancesData);
}

function renderInstanceSelector(instances = []) {
  const container = document.getElementById('instanceSelector');
  if (!container) return;

  // Se não vier dados (ex: no window.onload), buscamos os atuais
  if (instances.length === 0) {
    // Tenta carregar do backend ou usa cache se houver
    fetch(`${API_URL}/api/whatsapp/instances`, {
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    })
    .then(r => r.json())
    .then(data => renderInstanceSelector(data))
    .catch(err => console.error('Erro ao buscar instâncias para seletor:', err));
    return;
  }

  container.innerHTML = `<button class="instance-chip ${currentInstance === 'all' ? 'active' : ''}" onclick="selectInstance(this, 'all')">Todos</button>`;

  instances.forEach(inst => {
    const isConn = inst.status === 'connected' || inst.connectionStatus === 'open';
    const name = inst.name || inst.instanceName;
    const btn = document.createElement('button');
    btn.className = `instance-chip ${currentInstance === (inst.id || name) ? 'active' : ''}`;
    btn.onclick = () => selectInstance(btn, inst.id || name);
    btn.innerHTML = `
      <span class="chip-dot ${isConn ? 'connected' : 'disconnected'}"></span>
      ${name}
    `;
    container.appendChild(btn);
  });
}

function closeInstancesModal() {
  document.getElementById('instancesModal').style.display = 'none';
}

function toggleInstance(id) {
  // O ID pode ser o nome da instncia na WAHA
  showToast(`Tentando alternar status da instncia: ${id}`);
  // In production: call WAHA API to connect/disconnect instance
}

function addInstance() {
  alert('Integração com WAHA API: emitir POST /api/sessions/start na sua VPS.');
}

// ─── Filtering ────────────────────────────────────────────────────────────────
function filterChats(query) {
  const q = query.toLowerCase().trim();
  const qDigits = q.replace(/\D/g, ''); // somente dígitos para busca por número
  const filtered = CONTACTS.filter(c => {
    const nameMatch = c.name.toLowerCase().includes(q);
    const msgMatch  = (c.lastMsg || '').toLowerCase().includes(q);
    const phoneMatch = qDigits.length > 0 && (c.phone || '').replace(/\D/g, '').includes(qDigits);
    return nameMatch || msgMatch || phoneMatch;
  });
  renderChatList(filtered);
}

function selectInstance(btn, instance) {
  currentInstance = instance;
  document.querySelectorAll('.instance-chip').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderChatList(getFilteredContacts());
}

function setTab(btn, tab) {
  currentTab = tab;
  currentTagFilter = 'all';
  const tagFilterSelect = document.getElementById('tagFilter');
  if (tagFilterSelect) tagFilterSelect.value = 'all';
  document.querySelectorAll('.tab-btn').forEach(b => {
    if (b.tagName !== 'SELECT') b.classList.remove('active');
  });
  btn.classList.add('active');
  renderChatList(getFilteredContacts());
}

function filterByTag(tag) {
  currentTagFilter = tag;
  if (tag !== 'all') {
    document.querySelectorAll('.tab-btn').forEach(b => {
       if(b.tagName !== 'SELECT') b.classList.remove('active');
    });
  } else {
    document.querySelectorAll('.tab-btn').forEach(b => {
       if(b.tagName !== 'SELECT') b.classList.remove('active');
    });
    const tabs = document.querySelectorAll('.tab-btn');
    if (tabs.length > 0) tabs[0].classList.add('active');
    currentTab = 'all';
  }
  renderChatList(getFilteredContacts());
}

function renderTagFilter() {
  const select = document.getElementById('tagFilter');
  if (!select) return;
  const oldVal = select.value;
  
  const allTags = new Set();
  CONTACTS.forEach(c => {
    if (c.tags) {
      c.tags.forEach(t => allTags.add(t));
    }
  });
  
  select.innerHTML = '<option value="all">Filtro de Etiquetas</option>';
  Array.from(allTags).sort().forEach(tag => {
    const opt = document.createElement('option');
    opt.value = tag;
    opt.textContent = tag;
    select.appendChild(opt);
  });
  
  if (allTags.has(oldVal)) {
    select.value = oldVal;
  } else {
    currentTagFilter = 'all';
    select.value = 'all';
  }
}

function parseTimeStr(timeStr) {
  if (!timeStr) return 0;
  const parts = timeStr.split(' ');
  if (parts.length !== 2) return 0;
  const dateParts = parts[0].split('/');
  const timeParts = parts[1].split(':');
  if (dateParts.length !== 2 || timeParts.length !== 2) return 0;
  
  const day = parseInt(dateParts[0], 10);
  const month = parseInt(dateParts[1], 10) - 1;
  const hours = parseInt(timeParts[0], 10);
  const mins = parseInt(timeParts[1], 10);
  
  const now = new Date();
  let year = now.getFullYear();
  
  // if month is December and now is January, it was likely last year
  if (month === 11 && now.getMonth() === 0) year--;
  
  return new Date(year, month, day, hours, mins).getTime();
}

function getFilteredContacts() {
  const filtered = CONTACTS.filter(c => {
    if (currentInstance !== 'all' && c.instance !== currentInstance) return false;
    if (currentTab === 'unread' && c.unread === 0) return false;
    if (currentTagFilter !== 'all') {
      if (!c.tags || !c.tags.includes(currentTagFilter)) return false;
    }
    return true;
  });

  return filtered.sort((a, b) => {
    return parseTimeStr(b.time) - parseTimeStr(a.time);
  });
}

// ─── Emojis ───────────────────────────────────────────────────────────────────
function renderEmojis() {
  const grid = document.getElementById('emojiGrid');
  EMOJIS.forEach(emoji => {
    const btn = document.createElement('button');
    btn.className = 'emoji-btn-item';
    btn.textContent = emoji;
    btn.onclick = () => insertEmoji(emoji);
    grid.appendChild(btn);
  });
}

function toggleEmojiPanel() {
  emojiVisible = !emojiVisible;
  document.getElementById('emojiPanel').style.display = emojiVisible ? 'block' : 'none';
}

function insertEmoji(emoji) {
  const ta = document.getElementById('messageInput');
  const pos = ta.selectionStart;
  ta.value = ta.value.slice(0, pos) + emoji + ta.value.slice(pos);
  ta.focus();
  ta.selectionStart = ta.selectionEnd = pos + emoji.length;
  toggleEmojiPanel();
}

// ─── Localização ──────────────────────────────────────────────────────────────
async function sendLocationMessage() {
  if (!currentChat) return;
  
  if (!navigator.geolocation) {
    showToast('Geolocalização não é suportada pelo seu navegador.');
    return;
  }
  
  showToast('Obtendo sua localização (autorize no navegador)...');
  
  navigator.geolocation.getCurrentPosition(
    async (position) => {
      const lat = position.coords.latitude;
      const lng = position.coords.longitude;
      const user = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
      const nomeAtendente = user.name || 'Atendente';
      
      // Optimistic Update: Adiciona localmente na tela na mesma hora
      const now = new Date();
      const timeStr = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
      const tempText = `[LOCATION_REF] ${lat}|${lng}|Localização de ${nomeAtendente}|Enviado via CRM`;
      const tempId = 'loc_temp_' + Date.now();
      
      if (!currentChat.messages) currentChat.messages = [];
      currentChat.messages.push({ id: tempId, text: tempText, type: 'out', time: timeStr });
      currentChat.lastMsg = '📍 Localização';
      currentChat.time = timeStr;
      
      renderMessages(currentChat.messages);
      renderChatList(getFilteredContacts());
      
      try {
        const res = await fetch(`${API_URL}/api/whatsapp/send-location`, {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
          },
          body: JSON.stringify({
            instance: currentChat.instance,
            number: currentChat.phone,
            name: `Localização de ${nomeAtendente}`,
            address: 'Enviado via CRM',
            latitude: lat,
            longitude: lng
          })
        });
        
        if (res.ok) {
          showToast('Localização enviada!');
        } else {
          showToast('Erro ao enviar localização.');
        }
      } catch (e) {
        console.error(e);
        showToast('Erro de conexão ao enviar localização.');
      }
    },
    (error) => {
      console.error(error);
      let msg = 'Erro ao obter localização.';
      if (error.code === 1) msg = 'Permissão de localização negada.';
      else if (error.code === 2) msg = 'Posição não disponível no momento.';
      showToast(msg);
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
  );
}

// ─── File Upload ──────────────────────────────────────────────────────────────
function triggerFileUpload() {
  document.getElementById('fileUpload').click();
}

function handleFileUpload(event) {
  const file = event.target.files[0];
  if (!file || !currentChat) return;
  
  const fileType = file.type;
  
  if (fileType.startsWith('image/')) {
    sendImageMessage(file);
  } else if (fileType.startsWith('video/')) {
    sendVideoMessage(file);
  } else {
    sendDocumentMessage(file);
  }
  
  event.target.value = '';
}

async function sendImageMessage(file) {
  if (!currentChat) return;
  
  const reader = new FileReader();
  reader.onload = async function(e) {
    const base64 = e.target.result;
    const now = new Date();
    const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
    
    const tempId = 'img_temp_' + Date.now();
    const tempText = `[IMAGE_LOCAL] ${base64}`;
    const newMsg = { id: tempId, text: tempText, type: 'out', time };
    if (!currentChat.messages) currentChat.messages = [];
    currentChat.messages.push(newMsg);
    currentChat.lastMsg = '🖼️ Imagem';
    currentChat.time = time;
    renderMessages(currentChat.messages);
    renderChatList(getFilteredContacts());
    
    try {
      let targetInstance = currentChat.instance;
      if (targetInstance.startsWith('inst')) {
        targetInstance = getDefaultInstance();
        if (!targetInstance) {
          throw new Error('Nenhuma instância vinculada.');
        }
      }
      
      const cleanNumber = currentChat.phone.replace(/\D/g, '');
      
      const response = await fetch(`${API_URL}/api/whatsapp/send-image`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
        },
        body: JSON.stringify({
          instance: targetInstance,
          number: cleanNumber,
          image: base64,
          caption: ''
        })
      });
      
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Falha ao enviar imagem');
      
      const realId = data.msg_id || data.key?.id;
      if (realId) {
        newMsg.id = realId;
        newMsg.text = `[IMAGE_SENT] ${targetInstance}|${realId}`;
        _pendingImageIds.add(realId);
      }
      
      showToast('Imagem enviada!');
    } catch (err) {
      console.error('Erro ao enviar imagem:', err);
      showToast(`Erro ao enviar: ${err.message}`);
    }
  };
  reader.readAsDataURL(file);
}

async function sendVideoMessage(file) {
  if (!currentChat) return;
  
  const reader = new FileReader();
  reader.onload = async function(e) {
    const base64 = e.target.result;
    const now = new Date();
    const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
    
    const tempId = 'vid_temp_' + Date.now();
    const tempText = `[VIDEO_LOCAL] ${base64}`;
    const newMsg = { id: tempId, text: tempText, type: 'out', time };
    if (!currentChat.messages) currentChat.messages = [];
    currentChat.messages.push(newMsg);
    currentChat.lastMsg = '🎥 Vídeo';
    currentChat.time = time;
    renderMessages(currentChat.messages);
    renderChatList(getFilteredContacts());
    
    try {
      let targetInstance = currentChat.instance;
      if (targetInstance.startsWith('inst')) {
        targetInstance = getDefaultInstance();
        if (!targetInstance) {
          throw new Error('Nenhuma instância vinculada.');
        }
      }
      
      const cleanNumber = currentChat.phone.replace(/\D/g, '');
      
      const response = await fetch(`${API_URL}/api/whatsapp/send-video`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
        },
        body: JSON.stringify({
          instance: targetInstance,
          number: cleanNumber,
          video: base64,
          caption: ''
        })
      });
      
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Falha ao enviar vídeo');
      
      const realId = data.msg_id || data.key?.id;
      if (realId) {
        newMsg.id = realId;
        newMsg.text = `[VIDEO_SENT] ${targetInstance}|${realId}`;
        _pendingVideoIds.add(realId);
      }
      
      showToast('Vídeo enviado!');
    } catch (err) {
      console.error('Erro ao enviar vídeo:', err);
      showToast(`Erro ao enviar: ${err.message}`);
    }
  };
  reader.readAsDataURL(file);
}

async function sendDocumentMessage(file) {
  if (!currentChat) return;
  
  const reader = new FileReader();
  reader.onload = async function(e) {
    const base64 = e.target.result;
    const now = new Date();
    const time = `${now.getDate().toString().padStart(2,'0')}/${(now.getMonth()+1).toString().padStart(2,'0')} ${now.getHours().toString().padStart(2,'0')}:${now.getMinutes().toString().padStart(2,'0')}`;
    
    const tempId = 'doc_temp_' + Date.now();
    
    // Resolve instância antes para poder montar o DOC_REF temporário
    let targetInstance = currentChat.instance;
    if (targetInstance.startsWith('inst')) {
      targetInstance = getDefaultInstance();
      if (!targetInstance) {
        showToast('Nenhuma instância vinculada.');
        return;
      }
    }
    
    // Enquanto o upload ocorre, mostra estado 'Enviando...' (sem link clicável)
    const tempText = `[DOC_UPLOADING] ${file.name}`;
    const newMsg = { id: tempId, text: tempText, type: 'out', time };
    if (!currentChat.messages) currentChat.messages = [];
    currentChat.messages.push(newMsg);
    currentChat.lastMsg = '📎 Arquivo';
    currentChat.time = time;
    renderMessages(currentChat.messages);
    renderChatList(getFilteredContacts());
    
    try {
      const cleanNumber = currentChat.phone.replace(/\D/g, '');
      
      const response = await fetch(`${API_URL}/api/whatsapp/send-document`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
        },
        body: JSON.stringify({
          instance: targetInstance,
          number: cleanNumber,
          document: base64,
          fileName: file.name,
          caption: ''
        })
      });
      
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Falha ao enviar arquivo');
      
      const realId = data.msg_id || data.key?.id;
      if (realId) {
        newMsg.id = realId;
        // Agora atualiza para DOC_REF com o ID real — link de download funcionará
        newMsg.text = `[DOC_REF] ${targetInstance}|${realId}|${file.name}`;
        _pendingDocIds.add(realId);
        // Re-render para mostrar o link clicável correto
        renderMessages(currentChat.messages);
      }
      
      showToast('Arquivo enviado!');
    } catch (err) {
      console.error('Erro ao enviar arquivo:', err);
      showToast(`Erro ao enviar: ${err.message}`);
    }
  };
  reader.readAsDataURL(file);
}

// ─── Atendimento (Assign / Release) ──────────────────────────────────────────
function updateAttendanceBar(contact) {
  const bar = document.getElementById('attendanceBar');
  const info = document.getElementById('attendanceInfo');
  const actions = document.getElementById('attendanceActions');
  const inputBar = document.getElementById('inputBar');
  const inputLocked = document.getElementById('inputLockedBar');
  
  if (!bar || !contact) return;
  
  const user = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
  const isAssignedToMe = contact.assigned_to === user.id;
  const isAssigned = !!contact.assigned_to;
  const isAssignedToOther = isAssigned && !isAssignedToMe;
  
  bar.style.display = 'flex';
  
  if (!isAssigned) {
    // Chat livre — input travado, aguardando alguém apertar "Atender"
    info.innerHTML = `<span class="att-status-dot free"></span> <span>Chat sem atendente</span>`;
    actions.innerHTML = `<button class="btn-atender" onclick="assignChat()">✋ Atender</button>`;
    if (inputBar) inputBar.style.display = 'none';
    if (inputLocked) {
      inputLocked.style.display = 'flex';
      document.getElementById('inputLockedText').textContent = 'Clique em "Atender" para começar a responder';
    }
    const btnTransfer = document.getElementById('btnTransferChat');
    if (btnTransfer) btnTransfer.style.display = 'none';
  } else if (isAssignedToMe) {
    // Eu estou atendendo — input liberado
    info.innerHTML = `<span class="att-status-dot active"></span> <span>Você está atendendo este chat</span>`;
    actions.innerHTML = `<button class="btn-finalizar" onclick="releaseChat()">✖ Finalizar</button>`;
    if (inputBar) inputBar.style.display = 'flex';
    if (inputLocked) inputLocked.style.display = 'none';
    const btnTransfer = document.getElementById('btnTransferChat');
    if (btnTransfer) btnTransfer.style.display = 'flex';
  } else {
    // Outro atendente está atendendo — input travado
    info.innerHTML = `<span class="att-status-dot active"></span> <span>Atendido por <span class="att-label">${contact.assigned_name}</span></span>`;
    if (user.role === 'admin') {
      actions.innerHTML = `<button class="btn-destravar" onclick="releaseChat()">🔓 Destravar</button>`;
    } else {
      actions.innerHTML = '';
    }
    if (inputBar) inputBar.style.display = 'none';
    if (inputLocked) {
      inputLocked.style.display = 'flex';
      document.getElementById('inputLockedText').textContent = `Chat sendo atendido por ${contact.assigned_name}`;
    }
    const btnTransfer = document.getElementById('btnTransferChat');
    if (btnTransfer) btnTransfer.style.display = 'none';
  }
}

async function assignChat() {
  if (!currentChat) return;
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/contacts/${currentChat.id}/assign`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` }
    });
    const data = await res.json();
    if (res.ok) {
      currentChat.assigned_to = data.assigned_to;
      currentChat.assigned_name = data.assigned_name;
      if (data.tags) currentChat.tags = data.tags;
      updateAttendanceBar(currentChat);
      updateContactDetails(currentChat);
      renderChatList(getFilteredContacts());
      showToast('Você assumiu o atendimento!');
    } else {
      showToast(data.error || 'Erro ao atender');
    }
  } catch (err) {
    console.error('Erro ao atender:', err);
    showToast('Erro de conexão ao atender.');
  }
}

async function releaseChat() {
  if (!currentChat) return;
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/contacts/${currentChat.id}/release`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` }
    });
    const data = await res.json();
    if (res.ok) {
      currentChat.assigned_to = null;
      currentChat.assigned_name = null;
      if (data.tags) currentChat.tags = data.tags;
      updateAttendanceBar(currentChat);
      updateContactDetails(currentChat);
      renderChatList(getFilteredContacts());
      showToast('Atendimento finalizado!');
    } else {
      showToast(data.error || 'Erro ao finalizar');
    }
  } catch (err) {
    console.error('Erro ao finalizar:', err);
    showToast('Erro de conexão ao finalizar.');
  }
}

function handleChatAssignment(data) {
  // Update local contact data when another user assigns/releases
  const contact = CONTACTS.find(c => c.id === data.contact_id);
  if (contact) {
    contact.assigned_to = data.assigned_to;
    contact.assigned_name = data.assigned_name;
    if (data.tags) contact.tags = data.tags;
    
    // If this is the currently open chat, update the UI
    if (currentChat && currentChat.id === data.contact_id) {
      currentChat.assigned_to = data.assigned_to;
      currentChat.assigned_name = data.assigned_name;
      if (data.tags) currentChat.tags = data.tags;
      updateAttendanceBar(currentChat);
      updateContactDetails(currentChat);
    }
    renderChatList(getFilteredContacts());
  } else {
    // Contato não está na lista local — pode ser um chat transferido para mim
    // Verifica se as tags indicam que é do meu setor/filial
    const userData = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
    const tags = data.tags || [];
    let shouldReload = false;
    
    if (userData.role === 'user' && userData.filial && userData.setor) {
      const myTag = `${userData.filial}:${userData.setor}`;
      if (tags.includes(myTag)) shouldReload = true;
    } else if (userData.role === 'gestor' && userData.filial) {
      const hasMyFilial = tags.some(t => typeof t === 'string' && t.includes(':') && !t.toLowerCase().startsWith('atendente:') && t.split(':')[0] === userData.filial);
      if (hasMyFilial) shouldReload = true;
    }
    
    if (userData.role === 'admin' || data.assigned_to === userData.id) {
        shouldReload = true;
    }
    
    if (shouldReload) {
      console.log('[Socket] Chat transferido detectado via assignment, recarregando contatos:', data.contact_id);
      loadContacts().then(() => {
        renderTagFilter();
        renderChatList(getFilteredContacts());
      });
    }
  }
}

// ─── Misc ─────────────────────────────────────────────────────────────────────
async function saveTagsToBackend(contactId, tags) {
  try {
    const res = await fetch(`${API_URL}/api/contacts/${contactId}`, {
      method: 'PUT',
      headers: { 
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
      },
      body: JSON.stringify({ tags })
    });
    if (!res.ok) showToast('Erro ao salvar etiquetas');
  } catch(e) { console.error('Erro ao salvar etiquetas', e); }
}

function addTag() {
  const currentUser = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
  if (currentUser.role !== 'admin') {
    showToast('Apenas administradores podem adicionar etiquetas.');
    return;
  }
  const input = prompt('Nome da etiqueta:');
  if (input && input.trim() && currentChat) {
    const tag = input.trim();
    if (!currentChat.tags) currentChat.tags = [];
    if (!currentChat.tags.includes(tag)) {
      currentChat.tags.push(tag);
      updateContactDetails(currentChat);
      saveTagsToBackend(currentChat.id, currentChat.tags);
    }
  }
}

function removeTag(tagToRemove) {
  const currentUser = JSON.parse(localStorage.getItem('wp_crm_user') || '{}');
  if (currentUser.role !== 'admin') {
    showToast('Apenas administradores podem remover etiquetas.');
    return;
  }
  if (currentChat && currentChat.tags) {
    currentChat.tags = currentChat.tags.filter(t => t !== tagToRemove);
    updateContactDetails(currentChat);
    saveTagsToBackend(currentChat.id, currentChat.tags);
  }
}

function saveNotes() {
  const notes = document.getElementById('notesInput').value;
  // In production: save to DB
  showToast('Nota salva!');
}

async function openChatMenu() {
  if (!currentChat) return;
  
  const newName = prompt('Digite o novo nome para o contato:', currentChat.name);
  if (newName && newName !== currentChat.name) {
    try {
      // O backend agora espera o ID completo (c_numero_instancia)
      const response = await fetch(`${API_URL}/api/contacts/${currentChat.id}`, {
        method: 'PUT',
        headers: { 
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
        },
        body: JSON.stringify({ name: newName })
      });
      
      if (response.ok) {
        const updated = await response.json();
        currentChat.name = updated.name;
        currentChat.avatar = updated.avatar;
        
        // Atualiza UI
        document.getElementById('chatName').textContent = updated.name;
        document.getElementById('chatAvatar').textContent = updated.avatar;
        document.getElementById('detailsName').textContent = updated.name;
        document.getElementById('detailsAvatar').textContent = updated.avatar;
        
        renderChatList(getFilteredContacts());
        showToast('Nome atualizado com sucesso!');
      } else {
        const err = await response.json();
        showToast(`Erro ao atualizar: ${err.error}`);
      }
    } catch (err) {
      console.error('Erro ao salvar nome:', err);
      showToast('Erro de conexão ao salvar nome.');
    }
  }
}

async function showNewChat() {
  const modal = document.getElementById('newChatModal');
  modal.style.display = 'flex';
  document.getElementById('newChatNumber').value = '';
  document.getElementById('newChatReason').value = 'Olá!';
}

function closeNewChatModal() {
  document.getElementById('newChatModal').style.display = 'none';
};

// ==========================================
// ADMIN ENTREGADORES
// ==========================================

let adminEntregadoresCache = [];
let entregadorTransferenciaOrigem = null;

async function loadAdminEntregadores() {
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/admin/entregadores`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    
    if (res.ok) {
      adminEntregadoresCache = await res.json();
      renderAdminEntregadores();
    } else {
      console.error("Erro ao carregar entregadores");
    }
  } catch (err) {
    console.error(err);
  }
}

function renderAdminEntregadores() {
  const container = document.getElementById('adminEntregadoresList');
  if (!container) return;
  
  if (!adminEntregadoresCache || adminEntregadoresCache.length === 0) {
    container.innerHTML = '<p style="color:var(--text-muted);">Nenhum entregador encontrado no sistema.</p>';
    return;
  }
  
  let html = '';
  
  adminEntregadoresCache.forEach(ent => {
    const qtdAtivas = ent.entregas_ativas ? ent.entregas_ativas.length : 0;
    
    html += `
      <div style="background:var(--bg-dark); padding:16px; border-radius:12px; border:1px solid var(--border); display:flex; flex-direction:column; gap:12px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <h3 style="margin:0; font-size:16px; color:var(--primary);">${escapeHtml(ent.name)}</h3>
            <span style="font-size:13px; color:var(--text-muted);">${escapeHtml(ent.email)}</span>
          </div>
          <div style="background:var(--bg-panel); padding:4px 12px; border-radius:12px; border:1px solid var(--border);">
            <strong style="color:white;">${qtdAtivas}</strong> entregas ativas
          </div>
        </div>
        
        <div style="display:flex; gap:8px;">
          <button onclick="desativarRota(${ent.id})" style="flex:1; padding:10px; background:#dc3545; color:white; border:none; border-radius:8px; cursor:pointer; font-weight:600; ${qtdAtivas === 0 ? 'opacity:0.5; cursor:not-allowed;' : ''}" ${qtdAtivas === 0 ? 'disabled' : ''}>
            🔴 Desativar Rota
          </button>
          <button onclick="abrirModalTransferencia(${ent.id}, '${escapeHtml(ent.name)}')" style="flex:1; padding:10px; background:#f59e0b; color:white; border:none; border-radius:8px; cursor:pointer; font-weight:600; ${qtdAtivas === 0 ? 'opacity:0.5; cursor:not-allowed;' : ''}" ${qtdAtivas === 0 ? 'disabled' : ''}>
            🔁 Transferir Rota
          </button>
        </div>
      </div>
    `;
  });
  
  container.innerHTML = html;
}

async function desativarRota(id) {
  if (!confirm("Isso removerá todas as entregas do motoboy atual e as devolverá para 'Pronto para coleta'. Confirmar?")) return;
  
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/admin/entregadores/desativar_rota`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ entregador_id: id })
    });
    
    const data = await res.json();
    if (data.success) {
      alert(`Rota desativada! ${data.modificadas} entregas retornadas.`);
      loadAdminEntregadores(); // recarrega a tela
    } else {
      alert("Erro ao desativar rota: " + (data.error || 'Desconhecido'));
    }
  } catch (err) {
    console.error(err);
    alert('Erro de conexão ao desativar rota');
  }
}

function abrirModalTransferencia(origem_id, origem_nome) {
  entregadorTransferenciaOrigem = origem_id;
  document.getElementById('transferOrigemNome').textContent = origem_nome;
  
  const select = document.getElementById('transferDestinoSelect');
  select.innerHTML = '<option value="">Selecione...</option>';
  
  adminEntregadoresCache.forEach(ent => {
    if (ent.id !== origem_id) {
      select.innerHTML += `<option value="${ent.id}">${escapeHtml(ent.name)}</option>`;
    }
  });
  
  document.getElementById('modalTransferirRota').style.display = 'flex';
}

function fecharModalTransferencia() {
  document.getElementById('modalTransferirRota').style.display = 'none';
  entregadorTransferenciaOrigem = null;
}

async function confirmarTransferenciaRota() {
  const destino_id = document.getElementById('transferDestinoSelect').value;
  if (!destino_id) {
    alert("Selecione um entregador de destino.");
    return;
  }
  
  const token = localStorage.getItem('wp_crm_token');
  try {
    const res = await fetch(`${API_URL}/api/admin/entregadores/transferir`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ 
        origem_id: entregadorTransferenciaOrigem,
        destino_id: parseInt(destino_id)
      })
    });
    
    const data = await res.json();
    if (data.success) {
      alert(`Rota transferida com sucesso! ${data.modificadas} entregas foram movidas.`);
      fecharModalTransferencia();
      loadAdminEntregadores();
    } else {
      alert("Erro ao transferir rota: " + (data.error || 'Desconhecido'));
    }
  } catch (err) {
    console.error(err);
    alert('Erro de conexão ao transferir rota');
  }
}

function startNewChat() {
  const numberInput = document.getElementById('newChatNumber');
  const reasonInput = document.getElementById('newChatReason');
  
  const number = numberInput.value.trim().replace(/\D/g, '');
  const text = reasonInput.value.trim();

  if (!number) {
    showToast('Preencha o número');
    return;
  }

  // Regra de 12 dígitos solicitada
  if (number.length !== 12) {
    showToast('O número deve ter exatamente 12 dígitos numéricos.');
    return;
  }
  
  if (!text) {
    showToast('Digite uma mensagem inicial');
    return;
  }

  // Verifica se há instância selecionada ou pega a primeira do usuário
  let inst = currentInstance;
  if (!inst || inst === 'all') {
      inst = getDefaultInstance();
  }
  
  if (!inst) {
      showToast('Nenhuma instância selecionada ou vinculada ao seu usuário para enviar a mensagem.');
      return;
  }

  const token = localStorage.getItem('wp_crm_token');
  
  fetch(`${API_URL}/api/whatsapp/send`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`
    },
    body: JSON.stringify({ instance: inst, number: number, text: text })
  })
  .then(async res => {
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Erro ao enviar mensagem');
    return data;
  })
  .then(data => {
    showToast('Mensagem enviada com sucesso!');
    numberInput.value = '';
    reasonInput.value = 'Olá!';
    closeNewChatModal();
    // Recarrega contatos para exibir a nova conversa
    loadContacts();
  })
  .catch(err => {
    showToast(err.message || 'Erro de conexão ao enviar mensagem.');
  });
}

function logout() {
  localStorage.clear();
  sessionStorage.clear();
  window.location.href = 'index.html';
}

function showToast(msg) {
  const toast = document.createElement('div');
  toast.style.cssText = `
    position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
    background:#1f2c34;color:#e9edef;padding:10px 20px;border-radius:10px;
    border:1px solid #2a3942;font-size:13px;font-family:Inter,sans-serif;
    z-index:9999;animation:msgIn 0.2s ease;box-shadow:0 4px 20px rgba(0,0,0,0.4);
  `;
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function avatarColor(name) {
  const colors = ['#0d7377','#005c4b','#1a237e','#4a148c','#880e4f','#3e2723','#006064'];
  let hash = 0;
  for (let c of name) hash = c.charCodeAt(0) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

function tagColor(tag) {
  if (tag === 'BOT') return 'tag-purple';
  if (tag.startsWith('Atendente:')) return 'tag-orange';
  const map = { 'Novo Lead': 'tag-green', 'VIP': 'tag-blue', 'Cliente': 'tag-blue', 'Vendas': 'tag-green', 'Suporte': 'tag-blue', 'Leads': 'tag-green' };
  return map[tag] || 'tag-green';
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Close emoji panel on outside click
document.addEventListener('click', (e) => {
  if (emojiVisible && !e.target.closest('.emoji-btn') && !e.target.closest('.emoji-panel')) {
    emojiVisible = false;
  }
});

// ======== LIGHTBOX ========
function openLightbox(src) {
  const overlay = document.getElementById('imageLightbox');
  const img = document.getElementById('lightboxImg');
  if (overlay && img) {
    img.src = src;
    overlay.style.display = 'flex';
  }
}

function closeLightbox(e) {
  if (!e || e.target.id === 'imageLightbox' || e.target.classList.contains('lightbox-close')) {
    const overlay = document.getElementById('imageLightbox');
    if (overlay) {
      overlay.style.display = 'none';
      document.getElementById('lightboxImg').src = '';
    }
  }
}

// ─── Apagar Chat (Admin/Gestor) ─────────────────────────────────────────────
async function deleteCurrentChat() {
  if (!currentChat) return;
  if (!confirm('Tem certeza que deseja apagar essa conversa inteira? Essa ação não pode ser desfeita e todas as mensagens serão perdidas.')) return;
  
  try {
    const response = await fetch(`${API_URL}/api/chat/${encodeURIComponent(currentChat.id)}`, {
      method: 'DELETE',
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    });
    
    if (response.ok) {
      showToast('Conversa apagada com sucesso!');
      
      // Remove da lista atual
      chats = chats.filter(c => c.id !== currentChat.id);
      
      // Fecha a janela de chat
      document.getElementById('chatEmpty').style.display = 'flex';
      document.getElementById('chatInterface').style.display = 'none';
      if (window.innerWidth > 992) {
        document.getElementById('sidebarDetails').classList.remove('open');
      }
      currentChat = null;
      
      renderChats();
    } else {
      const data = await response.json();
      showToast('Erro ao apagar conversa: ' + (data.error || 'Erro desconhecido'));
    }
  } catch (e) {
    console.error('Erro ao deletar chat:', e);
    showToast('Erro ao apagar conversa. Verifique sua conexão.');
  }
}

// ─── Transferência de Chat (Admin) ──────────────────────────────────────────
async function openTransferModal() {
  if (!currentChat) return;
  document.getElementById('transferChatModal').style.display = 'flex';
  document.getElementById('transferSetorSelect').innerHTML = '<option value="">Selecione uma filial primeiro</option>';
  
  // Carregar filiais do backend
  const select = document.getElementById('transferFilialSelect');
  select.innerHTML = '<option value="">Carregando...</option>';
  try {
    const res = await fetch(`${API_URL}/api/admin/filiais?action=transfer`, {
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    });
    const filiais = await res.json();
    window._allTransferFiliais = filiais;
    select.innerHTML = '<option value="">Selecione uma filial</option>';
    filiais.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.id;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } catch(e) {
    console.error(e);
    select.innerHTML = '<option value="">Erro ao carregar</option>';
  }
}

function closeTransferModal() {
  document.getElementById('transferChatModal').style.display = 'none';
}

async function loadSetoresForTransfer() {
  const filialId = document.getElementById('transferFilialSelect').value;
  const select = document.getElementById('transferSetorSelect');
  if (!filialId) {
    select.innerHTML = '<option value="">Selecione uma filial primeiro</option>';
    return;
  }
  select.innerHTML = '<option value="">Carregando...</option>';
  try {
    const res = await fetch(`${API_URL}/api/admin/setores?action=transfer`, {
      headers: { 'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}` }
    });
    const setores = await res.json();
    const filtered = setores.filter(s => s.filial_id == filialId);
    select.innerHTML = '<option value="">Selecione um setor</option>';
    filtered.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name;
      select.appendChild(opt);
    });
  } catch(e) {
    console.error(e);
    select.innerHTML = '<option value="">Erro ao carregar</option>';
  }
}

async function confirmTransferChat() {
  const filialSelect = document.getElementById('transferFilialSelect');
  const setorSelect = document.getElementById('transferSetorSelect');
  const filialId = filialSelect.value;
  const setorId = setorSelect.value;
  
  if (!filialId || !setorId || !currentChat) return;
  
  const filialName = filialSelect.options[filialSelect.selectedIndex].text;
  const setorName = setorSelect.options[setorSelect.selectedIndex].text;
  
  try {
    const res = await fetch(`${API_URL}/api/chat/transfer`, {
      method: 'POST',
      headers: { 
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
      },
      body: JSON.stringify({
        contact_id: currentChat.id,
        filial: filialName,
        setor: setorName
      })
    });
    
    const data = await res.json();
    if (res.ok) {
      showToast('Conversa transferida com sucesso!');
      closeTransferModal();
      
      // Opcional: remover atribuição atual localmente
      currentChat.assigned_to = null;
      currentChat.assigned_name = null;
      
      const tagStr = `${filialName}:${setorName}`;
      if (!currentChat.tags) currentChat.tags = [];
      
      // Remover tag Atendente localmente para refletir logo a interface
      currentChat.tags = currentChat.tags.filter(t => typeof t === 'string' && !t.toLowerCase().startsWith('atendente:'));
      
      if (!currentChat.tags.includes(tagStr)) {
          currentChat.tags.push(tagStr);
      }
      
      updateAttendanceBar(currentChat);
      updateContactDetails(currentChat);
      renderChatList(getFilteredContacts());
    } else {
      showToast(data.error || 'Erro ao transferir');
    }
  } catch(e) {
    console.error(e);
    showToast('Erro de conexão ao transferir');
  }
}

// ─── Envio de Localização ─────────────────────────────────────────────────────
async function sendLocationMessage() {
  if (!currentChat) return;

  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(async (position) => {
      const lat = position.coords.latitude;
      const lng = position.coords.longitude;
      const targetInstance = currentChat.instanceName || currentChat.instance;
      const cleanNumber = currentChat.phone.replace(/\D/g, '');

      try {
        const response = await fetch(`${API_URL}/api/whatsapp/send-location`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
          },
          body: JSON.stringify({
            instance: targetInstance,
            number: cleanNumber,
            latitude: lat,
            longitude: lng,
            name: "Minha Localização",
            address: "Enviado pelo sistema"
          })
        });

        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Falha ao enviar localização');

        showToast('Localização enviada!');
      } catch (err) {
        console.error('Erro ao enviar localização:', err);
        showToast(`Erro ao enviar localização: ${err.message}`);
      }
    }, (error) => {
      console.error("Erro ao obter localização", error);
      showToast("Não foi possível obter a sua localização. Verifique as permissões do navegador.");
    });
  } else {
    showToast("Geolocalização não é suportada por este navegador.");
  }
}

// ─── Envio de Contato ─────────────────────────────────────────────────────────
function openSendContactModal() {
  if (!currentChat) {
    showToast("Selecione uma conversa primeiro.");
    return;
  }
  document.getElementById('sendContactModal').style.display = 'flex';
  document.getElementById('contactSendName').value = '';
  document.getElementById('contactSendPhone').value = '';
  document.getElementById('contactSendName').focus();
}

function closeSendContactModal() {
  document.getElementById('sendContactModal').style.display = 'none';
}

function maskPhoneInput(input) {
  let value = input.value.replace(/\D/g, '');
  if (value.length > 11) value = value.slice(0, 11);
  
  if (value.length > 2) {
    value = `(${value.slice(0,2)}) ${value.slice(2)}`;
  }
  if (value.length > 9) {
    value = `${value.slice(0,9)}-${value.slice(9)}`;
  }
  input.value = value;
}

async function confirmSendContact() {
  if (!currentChat) return;

  const contactName = document.getElementById('contactSendName').value.trim();
  const contactPhone = document.getElementById('contactSendPhone').value.trim();

  if (!contactName || !contactPhone) {
    showToast("Preencha o nome e o número de telefone.");
    return;
  }

  const targetInstance = currentChat.instanceName || currentChat.instance;
  const cleanNumber = currentChat.phone.replace(/\D/g, '');

  let formattedContactPhone = contactPhone.replace(/\D/g, '');
  if (formattedContactPhone.length === 10 || formattedContactPhone.length === 11) {
    formattedContactPhone = '55' + formattedContactPhone;
  }

  try {
    const btn = document.querySelector('#sendContactModal .btn-save-notes');
    const oldText = btn.innerHTML;
    btn.innerHTML = 'Enviando...';
    btn.disabled = true;

    const response = await fetch(`${API_URL}/api/whatsapp/send-contact`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('wp_crm_token')}`
      },
      body: JSON.stringify({
        instance: targetInstance,
        number: cleanNumber,
        contact_name: contactName,
        contact_phone: formattedContactPhone
      })
    });

    const data = await response.json();
    btn.innerHTML = oldText;
    btn.disabled = false;

    if (!response.ok) throw new Error(data.error || 'Falha ao enviar contato');

    showToast('Contato enviado!');
    closeSendContactModal();
  } catch (err) {
    console.error('Erro ao enviar contato:', err);
    showToast(`Erro ao enviar contato: ${err.message}`);
    const btn = document.querySelector('#sendContactModal .btn-save-notes');
    btn.innerHTML = '📤 Enviar Contato';
    btn.disabled = false;
  }
}

// ─── Mensagens Rápidas (Quick Replies) ───────────────────────────────────────
const QUICK_REPLIES_KEY = 'wp_crm_quick_replies';

function getQuickReplies() {
  try {
    return JSON.parse(localStorage.getItem(QUICK_REPLIES_KEY) || '[]');
  } catch (e) {
    return [];
  }
}

function saveQuickReplies(replies) {
  localStorage.setItem(QUICK_REPLIES_KEY, JSON.stringify(replies));
}

function renderQuickReplies() {
  const container = document.getElementById('quickRepliesBar');
  if (!container) return;

  const replies = getQuickReplies();
  container.innerHTML = '';
  
  if (replies.length > 0) {
    container.style.display = 'flex';
  } else {
    container.style.display = 'none';
  }

  // Renderiza cada mensagem rápida salva
  replies.forEach((reply, index) => {
    const chip = document.createElement('div');
    chip.className = 'quick-reply-chip';
    chip.title = reply;
    
    const textSpan = document.createElement('span');
    // Trunca para nÃ£o ficar gigante
    textSpan.textContent = reply.length > 30 ? reply.substring(0, 30) + '...' : reply;
    textSpan.onclick = () => insertQuickReply(reply);
    
    const delBtn = document.createElement('button');
    delBtn.className = 'del-btn';
    delBtn.innerHTML = '&times;';
    delBtn.title = 'Remover mensagem rápida';
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteQuickReply(index);
    };

    chip.appendChild(textSpan);
    chip.appendChild(delBtn);
    container.appendChild(chip);
  });

  // Botão de adicionar
  const addBtn = document.createElement('div');
  addBtn.className = 'quick-reply-chip add-btn';
  addBtn.innerHTML = '+ Nova Frase';
  addBtn.title = 'Adicionar frase rápida padrão';
  addBtn.onclick = addNewQuickReply;
  container.appendChild(addBtn);
  
  // Mostrar se estamos dentro do chatArea visível e há botão adicionar
  if (currentChat) {
     container.style.display = 'flex';
  }
}

function addNewQuickReply() {
  const phrase = prompt("Digite a frase padrão que deseja salvar:");
  if (phrase && phrase.trim()) {
    const replies = getQuickReplies();
    replies.push(phrase.trim());
    saveQuickReplies(replies);
    setTimeout(renderQuickReplies, 50); return res;
  }
}

function deleteQuickReply(index) {
  if (confirm("Remover esta frase rápida?")) {
    const replies = getQuickReplies();
    replies.splice(index, 1);
    saveQuickReplies(replies);
    setTimeout(renderQuickReplies, 50); return res;
  }
}

function insertQuickReply(text) {
  const input = document.getElementById('messageInput');
  if (input) {
    // Insere o texto onde o cursor está ou no final
    const start = input.selectionStart;
    const end = input.selectionEnd;
    const val = input.value;
    input.value = val.slice(0, start) + text + val.slice(end);
    
    // Atualiza o cursor e dispara eventos
    const newPos = start + text.length;
    input.selectionStart = newPos;
    input.selectionEnd = newPos;
    input.focus();
    
    autoResize(input);
    updateSendBtn();
  }
}

// Intercepta a seleção de chat para mostrar a barra
const originalOpenChatQR = window.openChat;
if (originalOpenChatQR) {
  window.openChat = async function(contactId) {
    const res = await originalOpenChatQR(contactId);
    setTimeout(renderQuickReplies, 50); return res;
  };
} else {
  // Caso nÃ£o intercepte por algum motivo, inicializa ao carregar
  document.addEventListener('DOMContentLoaded', renderQuickReplies);
}

// Chama inicialmente caso a página já tenha carregado
setTimeout(renderQuickReplies, 1000);

// Helper para abrir modal de nova conversa com número pré-preenchido
window.showNewChatWithNumber = async function(number) {
    let cleanNumber = String(number).replace(/\D/g, '');
    if (cleanNumber.length === 10 || cleanNumber.length === 11) {
        cleanNumber = '55' + cleanNumber;
    }
    
    await showNewChat();
    const numberInput = document.getElementById('newChatNumber');
    if (numberInput) {
        numberInput.value = cleanNumber;
    }

    // Pre-seleciona a instancia atual
    if (currentChat) {
        const currentInstance = currentChat.instanceName || currentChat.instance;
        const instanceSelect = document.getElementById('newChatInstance');
        if (instanceSelect && currentInstance) {
            for (const opt of instanceSelect.options) {
                if (opt.value === currentInstance) {
                    opt.selected = true;
                    break;
                }
            }
        }
    }
};

// ==========================================
// RASTREAMENTO GLOBAL DE ENTREGADORES (MAPA)
// ==========================================
let globalDriverMap = null;
let globalDriverMarkers = {};
let driverTrackingInterval = null;

function initGlobalDriverMap() {
  if (globalDriverMap) return;
  const container = document.getElementById('driverTrackingMap');
  if (!container) return;

  globalDriverMap = L.map('driverTrackingMap').setView([-15.7801, -47.9292], 4);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(globalDriverMap);

  fetchGlobalDriverLocations();
  if(driverTrackingInterval) clearInterval(driverTrackingInterval);
  driverTrackingInterval = setInterval(fetchGlobalDriverLocations, 5000); // 5 segundos
}

async function fetchGlobalDriverLocations() {
  const token = localStorage.getItem('wp_crm_token');
  if (!token) return;

  try {
    const res = await fetch(`${API_URL}/api/admin/drivers/locations`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    const data = await res.json();
    if (data.locations) {
      updateGlobalDriverMap(data.locations);
    }
  } catch (err) {
    console.error('Erro ao buscar loc de entregadores:', err);
  }
}

function updateGlobalDriverMap(locations) {
  if (!globalDriverMap) return;

  const currentIds = new Set();
  let hasPoints = false;

  // Custom Icon
  const truckIcon = L.icon({
    iconUrl: 'https://cdn-icons-png.flaticon.com/512/3089/3089851.png', // Um icone de caminhao basico
    iconSize: [32, 32],
    iconAnchor: [16, 32],
    popupAnchor: [0, -32]
  });

  locations.forEach(loc => {
    currentIds.add(loc.user_id);

    const latLng = [loc.lat, loc.lng];
    const lastUpdate = new Date(loc.updated_at).toLocaleTimeString();
    
    if (globalDriverMarkers[loc.user_id]) {
      // Atualiza
      globalDriverMarkers[loc.user_id].setLatLng(latLng);
      globalDriverMarkers[loc.user_id].setTooltipContent(`<b>${loc.name}</b><br>Atualizado: ${lastUpdate}`);
    } else {
      // Cria
      const marker = L.marker(latLng, {icon: truckIcon}).addTo(globalDriverMap);
      marker.bindTooltip(`<b>${loc.name}</b><br>Atualizado: ${lastUpdate}`, {
        permanent: false,
        direction: 'top'
      });
      globalDriverMarkers[loc.user_id] = marker;
    }
    hasPoints = true;
  });

  // Remove offline
  for (const id in globalDriverMarkers) {
    if (!currentIds.has(Number(id))) {
      globalDriverMap.removeLayer(globalDriverMarkers[id]);
      delete globalDriverMarkers[id];
    }
  }
}

// Iniciar o mapa quando entrar na aba de entregas
const originalSetView = setView;
setView = function(view) {
  originalSetView(view);
  if (view === 'entregas') {
    loadRouteHistory();
    // Mapa agora só inicia se estiver visível (ou se o usuário clicar para abrir)
    const mapContainer = document.getElementById('mapContainer');
    if (mapContainer && mapContainer.style.display !== 'none') {
      setTimeout(initGlobalDriverMap, 300);
    }
  } else {
    if (driverTrackingInterval) {
      clearInterval(driverTrackingInterval);
      driverTrackingInterval = null;
    }
  }
};

// ─── Colar imagem com Ctrl+V no campo de mensagem ───────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const messageInput = document.getElementById('messageInput');
  if (messageInput) {
    messageInput.addEventListener('paste', (event) => {
      const clipboardData = event.clipboardData || window.clipboardData;
      if (!clipboardData) return;
      
      const items = clipboardData.items;
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const file = item.getAsFile();
          if (file) {
            openImageCropModal(file);
            event.preventDefault();
            return;
          }
        }
      }
    });
  }
});

// ─── Modal de Recorte de Imagem ──────────────────────────────────────────────
let currentCropper = null;

function openImageCropModal(file) {
  const modal = document.getElementById('imageCropModal');
  const imageToCrop = document.getElementById('imageToCrop');
  
  if (currentCropper) {
    currentCropper.destroy();
    currentCropper = null;
  }
  
  const reader = new FileReader();
  reader.onload = (e) => {
    imageToCrop.src = e.target.result;
    modal.style.display = 'flex';
    
    currentCropper = new Cropper(imageToCrop, {
      viewMode: 1,
      dragMode: 'move',
      autoCropArea: 0.9,
      restore: false,
      guides: true,
      center: true,
      highlight: false,
      cropBoxMovable: true,
      cropBoxResizable: true,
      toggleDragModeOnDblclick: false,
    });
  };
  reader.readAsDataURL(file);
}

function closeImageCropModal() {
  document.getElementById('imageCropModal').style.display = 'none';
  if (currentCropper) {
    currentCropper.destroy();
    currentCropper = null;
  }
}

function confirmImageCrop() {
  if (!currentCropper) return;
  
  currentCropper.getCroppedCanvas({
    maxWidth: 1920,
    maxHeight: 1920,
  }).toBlob((blob) => {
    if (blob) {
      const croppedFile = new File([blob], "cropped_image.jpg", { type: "image/jpeg", lastModified: new Date().getTime() });
      sendImageMessage(croppedFile);
      closeImageCropModal();
    }
  }, 'image/jpeg', 0.85);
}
