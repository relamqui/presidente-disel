const API_URL = window.location.origin;

let entregas = [];
let minhasEntregas = [];
let currentEntrega = null;
let currentTab = 'disponiveis';
let deferredInstallPrompt = null;

// Service Worker Registration
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(err => {
    console.error('Service Worker registration failed:', err);
  });
}

// Capture Android install prompt
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  showInstallButton();
});

// When app is installed, hide the button
window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  hideInstallButton();
});

function showInstallButton() {
  const btn = document.getElementById('installBanner');
  if (btn) btn.style.display = 'flex';
}

function hideInstallButton() {
  const btn = document.getElementById('installBanner');
  if (btn) btn.style.display = 'none';
}

async function installApp() {
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  const { outcome } = await deferredInstallPrompt.userChoice;
  if (outcome === 'accepted') {
    deferredInstallPrompt = null;
    hideInstallButton();
  }
}

const VAPID_PUBLIC_KEY = 'BCyC2gcfNYoQcNvsmTEGbfsPLSvCrzhAy5w-Ik2fkHxMfweEeLEL-nvwzFeUBHYpo0dRHTt2NKSpywdiMf7cKnk';

async function subscribePushNotifications() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      
      // Forçar renovação da inscrição para pegar a nova chave VAPID
      if (subscription) {
        try {
          await subscription.unsubscribe();
        } catch (e) {
          console.error('Erro ao desinscrever', e);
        }
      }
      
      const convertedVapidKey = urlBase64ToUint8Array(VAPID_PUBLIC_KEY);
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: convertedVapidKey
      });
    
    // Send subscription to server
    const token = localStorage.getItem('entregador_token');
    await fetch(`${API_URL}/api/entregador/push/subscribe`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`
      },
      body: JSON.stringify({ subscription })
    });
    
    // Hide banner on success
    const banner = document.getElementById('notificationBanner');
    if(banner) banner.style.display = 'none';
    
    alert("Notificações ativadas com sucesso!");
    
  } catch (err) {
    console.error('Push subscription failed:', err);
    alert("Falha ao ativar notificações. Verifique se seu navegador suporta Push.");
  }
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

function checkNotificationStatus() {
  const banner = document.getElementById('notificationBanner');
  if (!banner) return;
  
  if (!('Notification' in window)) {
    banner.style.display = 'none';
    return;
  }
  
  if (Notification.permission === 'default' || Notification.permission === 'prompt') {
    banner.style.display = 'block';
  } else {
    banner.style.display = 'none';
    // Se já tiver permissão (granted), tentar renovar/garantir a inscrição de forma silenciosa
    if (Notification.permission === 'granted') {
      subscribePushNotificationsSilently();
    }
  }
}

async function subscribePushNotificationsSilently() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      const convertedVapidKey = urlBase64ToUint8Array(VAPID_PUBLIC_KEY);
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: convertedVapidKey
      });
    }
    const token = localStorage.getItem('entregador_token');
    if (token) {
      await fetch(`${API_URL}/api/entregador/push/subscribe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
        body: JSON.stringify({ subscription })
      });
    }
  } catch (e) {
    console.error('Silent sub failed', e);
  }
}

// Initialization
document.addEventListener('DOMContentLoaded', () => {
  const token = localStorage.getItem('entregador_token');
  if (token) {
    showScreen('dashboardScreen');
    switchTab('disponiveis');
    checkNotificationStatus();
    startLocationTracking();
  } else {
    showScreen('loginScreen');
  }

  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const isInStandaloneMode = window.matchMedia('(display-mode: standalone)').matches;
  const iosTip = document.getElementById('iosTip');
  if (isIos && !isInStandaloneMode && iosTip) {
    iosTip.style.display = 'flex';
  }

  // --- Socket.IO Notifications ---
  if (typeof io !== 'undefined') {
    const socket = io();
    socket.on('nova_entrega', (data) => {
      // Play a short beep if possible (using browser AudioContext or simple alert if fallback)
      if (Notification.permission === 'granted') {
        new Notification('Nova Entrega!', {
          body: `Nova entrega para ${data.nome_cliente}. Acesse para coletar!`,
          icon: '/Logo.png'
        });
      } else {
        alert(`Nova Entrega!\nTemos uma nova entrega para ${data.nome_cliente}. Acesse para coletar!`);
      }
      
      // Se estiver na aba de disponíveis, recarrega a lista
      if (currentTab === 'disponiveis') {
        loadEntregas();
      }
    });
  }
});

function switchTab(tab) {
  const isRotaAtiva = localStorage.getItem('isRotaAtiva') === 'true';
  if (isRotaAtiva && tab === 'disponiveis') {
    alert("Você tem uma Rota Ativa! Conclua suas entregas da rota atual antes de buscar novas.");
    return;
  }
  
  currentTab = tab;
  document.getElementById('tabDisponiveis').classList.toggle('active', tab === 'disponiveis');
  document.getElementById('tabMinhaRota').classList.toggle('active', tab === 'minha_rota');
  
  if (tab === 'disponiveis') {
    document.getElementById('bannerIniciarRota').style.display = 'none';
    loadEntregas();
  } else {
    loadMinhasEntregas();
  }
}

function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.style.display = 'none');
  document.getElementById(id).style.display = 'flex';
}

async function login() {
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value.trim();
  const errorDiv = document.getElementById('loginError');
  errorDiv.innerText = '';

  if (!email || !password) {
    errorDiv.innerText = 'Preencha e-mail e senha.';
    return;
  }

  try {
    const res = await fetch(`${API_URL}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    
    const data = await res.json();
    
    if (res.ok && data.token) {
      if (data.user.role !== 'entregador' && data.user.role !== 'admin') {
        errorDiv.innerText = 'Acesso negado. Apenas entregadores.';
        return;
      }
      localStorage.setItem('entregador_token', data.token);
      showScreen('dashboardScreen');
      switchTab('disponiveis');
      checkNotificationStatus();
      startLocationTracking();
    } else {
      errorDiv.innerText = data.error || 'Falha no login.';
    }
  } catch (err) {
    console.error(err);
    errorDiv.innerText = 'Erro ao conectar com o servidor.';
  }
}

function logout() {
  localStorage.removeItem('entregador_token');
  showScreen('loginScreen');
}

async function loadEntregas() {
  if (currentTab !== 'disponiveis') return;
  const token = localStorage.getItem('entregador_token');
  const container = document.getElementById('listaEntregas');
  
  try {
    const res = await fetch(`${API_URL}/api/entregador/entregas/disponiveis`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    
    if (res.ok) {
      entregas = await res.json();
      renderEntregas();
    } else if (res.status === 401 || res.status === 403) {
      logout();
    }
  } catch (err) {
    console.error(err);
    container.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--danger);">Erro ao carregar entregas.</div>';
  }
}

function renderEntregas() {
  const container = document.getElementById('listaEntregas');
  container.innerHTML = '';

  if (entregas.length === 0) {
    container.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Nenhuma entrega pronta para coleta.</div>';
    return;
  }

  entregas.forEach(e => {
    const dataObj = e.criado_em ? new Date(e.criado_em) : null;
    const hora = dataObj ? `${dataObj.getHours().toString().padStart(2,'0')}:${dataObj.getMinutes().toString().padStart(2,'0')}` : '';
    
    const tag = e.pago 
      ? '<span class="tag tag-green">Pago</span>' 
      : `<span class="tag tag-orange">Pagar: R$ ${e.valor || '0.00'}</span>`;

    const card = document.createElement('div');
    card.className = 'card';
    card.onclick = () => openDetails(e.id, false);
    card.innerHTML = `
      <div class="card-header">
        <span class="card-id">#${e.id}</span>
        <span class="card-time">${hora}</span>
      </div>
      <div class="card-title">${escapeHtml(e.nome_cliente)}</div>
      <div class="card-info">
        <span>📍 ${escapeHtml(e.localizacao).split(',')[0]}</span>
        <span>📦 ${escapeHtml(e.nome_peca)}</span>
        <div style="margin-top: 4px;">${tag}</div>
      </div>
    `;
    container.appendChild(card);
  });
}

async function loadMinhasEntregas() {
  if (currentTab !== 'minha_rota') return;
  const token = localStorage.getItem('entregador_token');
  const container = document.getElementById('listaEntregas');
  
  try {
    const res = await fetch(`${API_URL}/api/entregador/entregas/minhas`, {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    
    if (res.ok) {
      minhasEntregas = await res.json();
      
      const isRotaAtiva = localStorage.getItem('isRotaAtiva') === 'true';
      if (isRotaAtiva) {
        // Ordena conforme a rota salva
        const savedOrderStr = localStorage.getItem('rotaOrder');
        if (savedOrderStr) {
          const savedOrder = JSON.parse(savedOrderStr); // array de IDs
          minhasEntregas.sort((a, b) => {
            const idxA = savedOrder.indexOf(a.id);
            const idxB = savedOrder.indexOf(b.id);
            return (idxA === -1 ? 999 : idxA) - (idxB === -1 ? 999 : idxB);
          });
        }
      }
      
      renderMinhasEntregas();
    } else if (res.status === 401 || res.status === 403) {
      logout();
    }
  } catch (err) {
    console.error(err);
    container.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--danger);">Erro ao carregar sua rota.</div>';
  }
}

function renderMinhasEntregas() {
  const container = document.getElementById('listaEntregas');
  container.innerHTML = '';
  
  const isRotaAtiva = localStorage.getItem('isRotaAtiva') === 'true';

  if (minhasEntregas.length === 0) {
    document.getElementById('bannerIniciarRota').style.display = 'none';
    if (isRotaAtiva) localStorage.removeItem('isRotaAtiva'); // Limpa status se não tem mais entregas
    container.innerHTML = '<div style="text-align:center; padding: 20px; color: var(--text-muted);">Você não tem entregas na sua rota.</div>';
    return;
  }
  
  if (!isRotaAtiva) {
    document.getElementById('bannerIniciarRota').style.display = 'block';
  } else {
    document.getElementById('bannerIniciarRota').style.display = 'none';
  }

  minhasEntregas.forEach((e, index) => {
    const dataObj = e.criado_em ? new Date(e.criado_em) : null;
    const hora = dataObj ? `${dataObj.getHours().toString().padStart(2,'0')}:${dataObj.getMinutes().toString().padStart(2,'0')}` : '';
    
    const tag = e.pago 
      ? '<span class="tag tag-green">Pago</span>' 
      : `<span class="tag tag-orange">Pagar: R$ ${e.valor || '0.00'}</span>`;

    const card = document.createElement('div');
    card.className = 'card';
    
    const isFirst = (index === 0);
    
    if (isRotaAtiva) {
      if (isFirst) {
        card.classList.add('highlight'); // A próxima entrega!
        card.onclick = () => openDetails(e.id, true);
      } else {
        card.classList.add('locked'); // Bloqueadas
        card.onclick = () => alert("Complete a entrega anterior primeiro!");
      }
    } else {
      card.onclick = () => openDetails(e.id, true);
    }
    
    let orderBadge = isRotaAtiva ? `<div style="font-weight:bold; color:var(--primary); margin-bottom:5px;">Parada #${index + 1}</div>` : '';

    card.innerHTML = `
      ${orderBadge}
      <div class="card-header">
        <span class="card-id">#${e.id}</span>
        <span class="card-time">${hora}</span>
      </div>
      <div class="card-title">${escapeHtml(e.nome_cliente)}</div>
      <div class="card-info">
        <span>📍 ${escapeHtml(e.localizacao).split(',')[0]}</span>
        <span>📦 ${escapeHtml(e.nome_peca)}</span>
        <div style="margin-top: 4px;">${tag}</div>
      </div>
    `;
    container.appendChild(card);
  });
}

function openDetails(id, isMinhaRota) {
  // Define a entrega atual
  currentEntrega = (isMinhaRota ? minhasEntregas : entregas).find(e => e.id === id);
  if (!currentEntrega) return;
  
  const e = currentEntrega;
  const container = document.getElementById('detailsContent');
  
  let googleMapsUrl = '';
  if (e.latitude && e.longitude) {
    const lat = String(e.latitude).replace(',', '.').trim();
    const lng = String(e.longitude).replace(',', '.').trim();
    googleMapsUrl = `https://www.google.com/maps/search/?api=1&query=${lat},${lng}`;
  }

  let mapsBtn = googleMapsUrl ? `<a href="${googleMapsUrl}" target="_blank" class="btn-maps">📍 Abrir Navegação (Google Maps)</a>` : '';

  const isLocked = isMinhaRota && localStorage.getItem('isRotaAtiva') === 'true';
  const isFirst = (minhasEntregas.length > 0 && minhasEntregas[0].id === e.id);

  document.getElementById('btnAceitar').style.display = isMinhaRota ? 'none' : 'block';
  
  if (isMinhaRota && isLocked && isFirst) {
    document.getElementById('conclusionButtons').style.display = 'flex';
  } else {
    document.getElementById('conclusionButtons').style.display = 'none';
  }

  container.innerHTML = `
    <div class="detail-row">
      <span class="detail-label">Cliente</span>
      <span class="detail-value" style="font-size:18px; font-weight:600;">${escapeHtml(e.nome_cliente)}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Telefone</span>
      <span class="detail-value">${escapeHtml(e.telefone_cliente || '-')}</span>
    </div>
    <div class="detail-row">
      <span class="detail-label">Endereço de Entrega</span>
      <span class="detail-value">${escapeHtml(e.localizacao)}</span>
      ${mapsBtn}
    </div>
    <div style="height:1px; background:var(--border); margin: 8px 0;"></div>
    <div class="detail-row">
      <span class="detail-label">Produto</span>
      <span class="detail-value"><strong>${escapeHtml(e.nome_peca)}</strong> (Tam: ${escapeHtml(e.tamanho_peca || '-')})</span>
    </div>
    <div class="detail-row" style="margin-top: 8px;">
      <span class="detail-label">Pagamento</span>
      <span class="detail-value">
        ${e.pago ? '<span style="color:#00a884;font-weight:600;">Já Pago</span>' : `<span style="color:#f59e0b;font-weight:600;">Cobrar: R$ ${e.valor || '0.00'}</span> (${e.forma_pagamento || '-'})`}
      </span>
    </div>
  `;
  
  showScreen('detailsScreen');
}

function voltarParaDashboard() {
  currentEntrega = null;
  showScreen('dashboardScreen');
  if (currentTab === 'disponiveis') loadEntregas();
  else loadMinhasEntregas();
}

async function iniciarRotaOtimizada() {
  if (!navigator.geolocation) {
    alert("GPS não suportado. Não é possível otimizar.");
    return;
  }

  const validEntregas = minhasEntregas.filter(e => e.latitude && e.longitude);
  if (validEntregas.length === 0) {
    alert("Nenhuma das suas entregas possui coordenadas válidas de mapa.");
    return;
  }

  document.getElementById('bannerIniciarRota').innerHTML = '<i>Calculando melhor rota...</i>';

  navigator.geolocation.getCurrentPosition(async (pos) => {
    const lat0 = pos.coords.latitude;
    const lng0 = pos.coords.longitude;

    try {
      // Monta as coordenadas. OSRM usa Lng,Lat
      let coords = `${lng0},${lat0}`;
      for (const e of validEntregas) {
        // Garantir que a coordenada use ponto ao invés de vírgula para as casas decimais
        let elng = String(e.longitude).replace(',', '.').trim();
        let elat = String(e.latitude).replace(',', '.').trim();
        coords += `;${elng},${elat}`;
      }
      
      const token = localStorage.getItem('entregador_token');
      const res = await fetch(`${API_URL}/api/entregador/otimizar_rota`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({ coords })
      });
      
      const data = await res.json();
      
      if (!res.ok) {
        console.error("OSRM Proxy Error:", res.status, data);
        alert(`A inteligência de rotas recusou o cálculo (Erro ${res.status}). ${data.error || ''}`);
        loadMinhasEntregas();
        return;
      }
      
      if (data.code === 'Ok' && data.waypoints) {
        // Mapeia o índice original (0 é o driver, 1 é validEntregas[0], etc)
        data.waypoints.forEach((wp, idx) => { wp.original_array_index = idx; });
        
        // waypoints tem a nova ordem ditada por waypoint_index
        const order = data.waypoints.slice(1).sort((a,b) => a.waypoint_index - b.waypoint_index);
        
        let newOrderIds = [];
        let googleMapsDirUrl = `https://www.google.com/maps/dir/${lat0},${lng0}`;
        
        for (const wp of order) {
          const originalIdx = wp.original_array_index - 1; 
          const e = validEntregas[originalIdx];
          
          if (e) {
            newOrderIds.push(e.id);
            let mapLat = String(e.latitude).replace(',', '.').trim();
            let mapLng = String(e.longitude).replace(',', '.').trim();
            googleMapsDirUrl += `/${mapLat},${mapLng}`;
          }
        }
        
        // Adiciona pro final da fila as entregas que vieram sem coordenadas (se houver)
        for (const e of minhasEntregas) {
          if (!newOrderIds.includes(e.id)) {
            newOrderIds.push(e.id);
          }
        }
        
        localStorage.setItem('rotaOrder', JSON.stringify(newOrderIds));
        localStorage.setItem('isRotaAtiva', 'true');
        
        window.open(googleMapsDirUrl, '_blank');
        loadMinhasEntregas(); // recarrega a UI travando na ordem
      } else {
        alert("Erro ao calcular otimização: " + (data.message || 'Desconhecido'));
        loadMinhasEntregas();
      }
    } catch(err) {
      console.error("Catch Error OSRM:", err);
      alert("Erro no App: " + err.message + "\n(Dica: O servidor pode estar desatualizado, reinicie-o no Easypanel)");
      loadMinhasEntregas();
    }
  }, (err) => {
    alert("Você precisa permitir o GPS para calcular a rota!");
    loadMinhasEntregas();
  });
}

async function aceitarEntrega() {
  if (!currentEntrega) return;
  
  const codigo = prompt("Digite o código de verificação para travar essa entrega em seu nome:");
  if (!codigo) return; // Usuário cancelou ou deixou vazio
  
  const token = localStorage.getItem('entregador_token');
  try {
    const res = await fetch(`${API_URL}/api/entregador/aceitar_entrega`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        entrega_id: currentEntrega.id,
        codigo_verificacao: codigo
      })
    });
    const data = await res.json();
    if (data.success) {
      alert('Adicionada à sua rota com sucesso!');
      voltarParaDashboard();
    } else {
      alert(`Erro: ${data.error || 'Código incorreto ou entrega indisponível.'}`);
    }
  } catch (err) {
    console.error(err);
    alert('Erro ao se conectar ao servidor.');
  }
}

// ==========================================
// FUNÇÕES DE DISTÂNCIA E VALIDAÇÃO DE ENTREGA
// ==========================================
function getDistanceFromLatLonInMeters(lat1, lon1, lat2, lon2) {
  var R = 6371000; // Radius of the earth in m
  var dLat = deg2rad(lat2-lat1);  
  var dLon = deg2rad(lon2-lon1); 
  var a = 
    Math.sin(dLat/2) * Math.sin(dLat/2) +
    Math.cos(deg2rad(lat1)) * Math.cos(deg2rad(lat2)) * 
    Math.sin(dLon/2) * Math.sin(dLon/2)
    ; 
  var c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a)); 
  var d = R * c; 
  return d;
}

function deg2rad(deg) {
  return deg * (Math.PI/180)
}

function tentarConcluirEntrega() {
  if (!currentEntrega) return;
  if (!currentEntrega.latitude || !currentEntrega.longitude) {
    // Se a entrega não tem coordenadas originais, passa direto
    concluirEntregaAtual();
    return;
  }

  navigator.geolocation.getCurrentPosition((pos) => {
    const latAtual = pos.coords.latitude;
    const lngAtual = pos.coords.longitude;
    const latDest = parseFloat(String(currentEntrega.latitude).replace(',', '.'));
    const lngDest = parseFloat(String(currentEntrega.longitude).replace(',', '.'));
    
    const dist = getDistanceFromLatLonInMeters(latAtual, lngAtual, latDest, lngDest);
    
    if (dist <= 150) {
      // Perto o suficiente
      concluirEntregaAtual();
    } else {
      // Longe demais, pedir senha
      document.getElementById('modalSenhaAdmin').style.display = 'flex';
      document.getElementById('inputSenhaAdmin').value = '';
    }
  }, (err) => {
    alert("Não foi possível acessar seu GPS. A senha de liberação será exigida.");
    document.getElementById('modalSenhaAdmin').style.display = 'flex';
    document.getElementById('inputSenhaAdmin').value = '';
  }, { enableHighAccuracy: true });
}

function fecharModalSenha() {
  document.getElementById('modalSenhaAdmin').style.display = 'none';
}

async function confirmarSenhaAdmin() {
  const codigo = document.getElementById('inputSenhaAdmin').value;
  if (!codigo) {
    alert("Digite o código.");
    return;
  }
  
  const token = localStorage.getItem('entregador_token');
  try {
    const res = await fetch(`${API_URL}/api/entregador/verificar_codigo`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ codigo })
    });
    
    if (res.ok) {
      fecharModalSenha();
      document.getElementById('inputJustificativaDistancia').value = '';
      document.getElementById('modalJustificativaDistancia').style.display = 'flex';
    } else {
      const data = await res.json();
      alert(data.error || "Código incorreto!");
    }
  } catch (err) {
    console.error(err);
    alert('Erro ao validar código.');
  }
}

function fecharModalJustificativaDistancia() {
  document.getElementById('modalJustificativaDistancia').style.display = 'none';
}

function confirmarJustificativaDistancia() {
  const justificativa = document.getElementById('inputJustificativaDistancia').value.trim();
  if (!justificativa) {
    alert('Por favor, informe o motivo para prosseguir.');
    return;
  }
  fecharModalJustificativaDistancia();
  concluirEntregaAtual(justificativa);
}

// ==========================================
// SUCESSO E FALHA
// ==========================================

async function concluirEntregaAtual(justificativaDistancia = null) {
  if (!currentEntrega) return;
  if (!confirm("Tem certeza que finalizou a entrega no cliente?")) return;
  
  const token = localStorage.getItem('entregador_token');
  try {
    const res = await fetch(`${API_URL}/api/entregador/concluir_entrega`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        entrega_id: currentEntrega.id,
        justificativa_distancia: justificativaDistancia
      })
    });
    const data = await res.json();
    if (data.success) {
      alert('Baixa realizada com sucesso!');
      
      // Remove da ordem de rotas ativa
      const savedOrderStr = localStorage.getItem('rotaOrder');
      if (savedOrderStr) {
        let savedOrder = JSON.parse(savedOrderStr);
        savedOrder = savedOrder.filter(id => id !== currentEntrega.id);
        localStorage.setItem('rotaOrder', JSON.stringify(savedOrder));
      }
      
      voltarParaDashboard();
    } else {
      alert(`Erro: ${data.error || 'Não foi possível dar baixa.'}`);
    }
  } catch (err) {
    console.error(err);
    alert('Erro ao se conectar ao servidor.');
  }
}

function abrirModalFalha() {
  document.getElementById('modalFalha').style.display = 'flex';
  document.getElementById('inputJustificativa').value = '';
}

function fecharModalFalha() {
  document.getElementById('modalFalha').style.display = 'none';
}

async function confirmarFalha() {
  if (!currentEntrega) return;
  const justificativa = document.getElementById('inputJustificativa').value.trim();
  if (!justificativa) {
    alert("Por favor, informe a justificativa.");
    return;
  }
  
  if (!confirm("Confirmar que esta entrega FALHOU?")) return;
  
  const token = localStorage.getItem('entregador_token');
  try {
    const res = await fetch(`${API_URL}/api/entregador/falha_entrega`, {
      method: 'POST',
      headers: { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        entrega_id: currentEntrega.id,
        justificativa: justificativa
      })
    });
    const data = await res.json();
    if (data.success) {
      alert('Falha registrada!');
      fecharModalFalha();
      
      // Remove da ordem de rotas ativa
      const savedOrderStr = localStorage.getItem('rotaOrder');
      if (savedOrderStr) {
        let savedOrder = JSON.parse(savedOrderStr);
        savedOrder = savedOrder.filter(id => id !== currentEntrega.id);
        localStorage.setItem('rotaOrder', JSON.stringify(savedOrder));
      }
      
      voltarParaDashboard();
    } else {
      alert(`Erro: ${data.error || 'Não foi possível registrar a falha.'}`);
    }
  } catch (err) {
    console.error(err);
    alert('Erro ao se conectar ao servidor.');
  }
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.innerText = text;
  return div.innerHTML;
}

// === LOCATION TRACKING ===
let locationInterval = null;

function startLocationTracking() {
  // Clear any existing interval
  if (locationInterval) clearInterval(locationInterval);
  
  // Track immediately
  trackLocation();
  
  // Then track every 5 seconds
  locationInterval = setInterval(trackLocation, 5000);
}

function trackLocation() {
  if (!navigator.geolocation) return;
  
  navigator.geolocation.getCurrentPosition(
    (position) => {
      sendLocationToBackend(position.coords.latitude, position.coords.longitude);
    },
    (err) => {
      console.warn('Erro ao obter GPS (pode estar negado ou desativado):', err.message);
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
  );
}

async function sendLocationToBackend(lat, lng) {
  const token = localStorage.getItem('entregador_token');
  if (!token) return;
  
  try {
    await fetch(`${API_URL}/api/entregador/location`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`
      },
      body: JSON.stringify({ lat, lng })
    });
  } catch (err) {
    console.error('Falha ao enviar localização', err);
  }
}

