# Modo async: quando executado via Gunicorn com --worker-class eventlet,
# o próprio Gunicorn faz o monkey_patch. Em dev local, usa threading.
import sys
_async_mode = 'eventlet' if 'eventlet' in sys.modules else 'threading'

import os
import jwt
import uuid
import datetime
import pytz

def get_now():
    return datetime.datetime.now(pytz.timezone('America/Sao_Paulo'))

from functools import wraps
import time
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.attributes import flag_modified
import requests
from dotenv import load_dotenv
import json
import io
from flask import Response

load_dotenv()

WAHA_API_URL = os.getenv('WAHA_API_URL', 'http://localhost:3000').rstrip('/')
WAHA_API_KEY = os.getenv('WAHA_API_KEY', '')


def get_valid_waha_number(number, session, waha_base_url, headers):
    """Verifica via check-exists qual formato do número tem WhatsApp ativo.
    Tenta primeiro o número como está no banco (sem o 9), depois tenta com o 9.
    Retorna o chatId exato retornado pela API (ex: '5511970910090@c.us') ou None.
    """
    import requests
    base = waha_base_url.rstrip('/')
    from urllib.parse import urlparse
    parsed = urlparse(base)
    check_exists_url = f"{parsed.scheme}://{parsed.netloc}/api/contacts/check-exists"

    def check_number(num):
        """Retorna o chatId da API se o número existir, ou None caso contrário."""
        try:
            chk_res = requests.get(
                check_exists_url,
                params={"phone": num, "session": session},
                headers=headers,
                timeout=10
            )
            if chk_res.status_code == 200:
                data = chk_res.json()
                exists = data.get("numberExists", False)
                api_chat_id = data.get("chatId", "")
                print(f"[CHECK-EXISTS] {num} → numberExists={exists}, chatId={api_chat_id}")
                if exists:
                    return api_chat_id if api_chat_id else f"{num}@c.us"
                return None
            print(f"[CHECK-EXISTS] {num} → HTTP {chk_res.status_code}, considerando False")
            return None
        except Exception as e:
            print(f"[CHECK-EXISTS] {num} → Exceção: {e}, considerando False")
            return None

    # Número BR com 12 dígitos (55 + DDD + 8 dígitos): sem o 9
    if len(number) == 12 and number.startswith("55"):
        result = check_number(number)
        if result:
            return result
        number_13 = number[:4] + "9" + number[4:]  # adiciona o 9 → 13 dígitos
        result = check_number(number_13)
        if result:
            return result
        return None

    # Número BR com 13 dígitos (55 + DDD + 9 + 8 dígitos): com o 9
    elif len(number) == 13 and number.startswith("55"):
        result = check_number(number)
        if result:
            return result
        number_12 = number[:4] + number[5:]  # remove o 9 → 12 dígitos
        result = check_number(number_12)
        if result:
            return result
        return None

    # Outros formatos: verificar como está
    return check_number(number)

def waha_post_with_fallback(url, payload, headers, timeout=30):
    """Envia mensagem via WAHA verificando antes com check-exists qual formato
    de número é válido (com ou sem o 9° dígito BR). Usa o chatId exato
    retornado pela API. Retorna erro 400 se o número não possuir WhatsApp ativo.
    """
    import copy, requests, json
    chat_id = payload.get("chatId", "")
    session = payload.get("session", "")

    if "@c.us" not in chat_id or not session:
        return requests.post(url, json=payload, headers=headers, timeout=timeout)

    number = chat_id.replace("@c.us", "")
    # Retorna o chatId exato da API (ex: '5511970910090@c.us') ou None
    final_chat_id = get_valid_waha_number(number, session, WAHA_API_URL, headers)

    class MockResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text
        def json(self):
            return json.loads(self.text)
        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(self.text)

    if not final_chat_id:
        print(f"[SEND] Número {number} não possui WhatsApp ativo em nenhum formato.")
        return MockResponse(400, '{"message": "Número não possui WhatsApp ativo. Verifique o número e tente novamente."}')

    payload_final = copy.deepcopy(payload)
    # Usar o chatId exato retornado pela API — não reconstruir manualmente
    payload_final["chatId"] = final_chat_id
    print(f"[SEND] Usando chatId validado pela API: {final_chat_id} (original: {chat_id})")

    reply_to = payload_final.get("reply_to")
    if reply_to and not reply_to.startswith("true_") and not reply_to.startswith("false_"):
        payload_final["reply_to"] = f"true_{final_chat_id}_{reply_to}"

    return requests.post(url, json=payload_final, headers=headers, timeout=timeout)

def get_waha_headers():
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if WAHA_API_KEY:
        headers['X-Api-Key'] = WAHA_API_KEY
    return headers

CORPAL_WEBHOOK_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/corpal-metrica'

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode=_async_mode)
print(f"[INIT] SocketIO async_mode={_async_mode}")

JWT_SECRET = os.getenv('JWT_SECRET', 'secret')
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# Identificador único de inicialização para forçar refresh nos clientes quando o backend for atualizado/reiniciado
SERVER_BOOT_ID = str(uuid.uuid4())

# Configuração do Local de Armazenamento
DATA_DIR = os.path.join(os.getcwd(), 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ─── Configuração MinIO (S3-compatible) ──────────────────────────────────────
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

MINIO_ENDPOINT    = os.getenv('MINIO_ENDPOINT',    'https://teste-minio.ioms5g.easypanel.host')
MINIO_ACCESS_KEY  = os.getenv('MINIO_ACCESS_KEY',  'WwBXwqD5gy1EMd4tAXfm')
MINIO_SECRET_KEY  = os.getenv('MINIO_SECRET_KEY',  'JZQYvE2qklBT0cpnSUiKAI7TdzBHMIqIN5wufjMD')
MINIO_BUCKET      = os.getenv('MINIO_BUCKET',      'presidente')

def _get_minio_client():
    return boto3.client(
        's3',
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        config=Config(signature_version='s3v4'),
        region_name='us-east-1'
    )

def minio_upload(file_bytes: bytes, object_key: str, content_type: str = 'application/octet-stream') -> bool:
    """Faz upload de bytes para o MinIO. Retorna True em caso de sucesso."""
    import sys
    try:
        s3 = _get_minio_client()
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=object_key,
            Body=file_bytes,
            ContentType=content_type,
        )
        print(f"[MinIO] Upload OK: {object_key} ({len(file_bytes)} bytes)")
        sys.stdout.flush()
        return True
    except Exception as e:
        print(f"[MinIO] Erro no upload de {object_key}: {e}")
        sys.stdout.flush()
        return False

def minio_exists(object_key: str) -> bool:
    """Verifica se um objeto existe no MinIO."""
    import sys
    try:
        s3 = _get_minio_client()
        s3.head_object(Bucket=MINIO_BUCKET, Key=object_key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        print(f"[MinIO] Erro ClientError ao verificar {object_key}: {e}")
        sys.stdout.flush()
        return False
    except Exception as e:
        print(f"[MinIO] Erro Exception ao verificar {object_key}: {e}")
        sys.stdout.flush()
        return False

def minio_presigned_url(object_key: str, expiry: int = 3600) -> str | None:
    """Gera uma URL temporária (presigned) para acessar o objeto. Expira em `expiry` segundos."""
    try:
        s3 = _get_minio_client()
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': MINIO_BUCKET, 'Key': object_key},
            ExpiresIn=expiry
        )
        return url
    except Exception as e:
        print(f"[MinIO] Erro ao gerar presigned URL para {object_key}: {e}")
        return None
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get('DB_PATH', os.path.join(DATA_DIR, 'db.json'))
DATABASE_URL = os.environ.get('DATABASE_URL', '')

if not DATABASE_URL or DATABASE_URL == 'sqlite_local':
    DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'wpcrm.db')}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    pass

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db_sql = SQLAlchemy(app)

# ─── Modelos do Banco de Dados ──────────────────────────────────────────────

class Filial(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    instance = db_sql.Column(db_sql.String(100), nullable=False)

class Setor(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    filial_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('filial.id'), nullable=False)
    filial_name = db_sql.Column(db_sql.String(100), nullable=True)

class User(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    name = db_sql.Column(db_sql.String(100), nullable=False)
    email = db_sql.Column(db_sql.String(120), unique=True, nullable=False)
    phone = db_sql.Column(db_sql.String(30), nullable=True)
    password = db_sql.Column(db_sql.String(200), nullable=False)
    role = db_sql.Column(db_sql.String(20), default='user')
    instances = db_sql.Column(db_sql.JSON, default=[]) # Nomes das instâncias vinculadas
    filial_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('filial.id'), nullable=True)
    setor_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('setor.id'), nullable=True)
    filial = db_sql.Column(db_sql.String(150), nullable=True)
    setor = db_sql.Column(db_sql.String(150), nullable=True)

class PushSubscription(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    user_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=False)
    endpoint = db_sql.Column(db_sql.Text, nullable=False, unique=True)
    p256dh = db_sql.Column(db_sql.String(255), nullable=False)
    auth = db_sql.Column(db_sql.String(255), nullable=False)
    created_at = db_sql.Column(db_sql.DateTime, default=get_now)

class DriverLocation(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    user_id = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=False, unique=True)
    lat = db_sql.Column(db_sql.Float, nullable=False)
    lng = db_sql.Column(db_sql.Float, nullable=False)
    updated_at = db_sql.Column(db_sql.DateTime, default=get_now)
    
    user = db_sql.relationship('User', backref=db_sql.backref('location', uselist=False))

class Contact(db_sql.Model):
    id = db_sql.Column(db_sql.String(150), primary_key=True) # c_phone_instance
    name = db_sql.Column(db_sql.String(100), nullable=False)
    phone = db_sql.Column(db_sql.String(30), nullable=False) # No longer unique
    avatar = db_sql.Column(db_sql.Text, nullable=True)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    tags = db_sql.Column(db_sql.JSON, default=['Novo Lead'])
    last_msg = db_sql.Column(db_sql.Text, nullable=True)
    last_msg_time = db_sql.Column(db_sql.String(20), nullable=True)
    unread = db_sql.Column(db_sql.Integer, default=0)
    assigned_to = db_sql.Column(db_sql.Integer, db_sql.ForeignKey('user.id'), nullable=True)
    assigned_name = db_sql.Column(db_sql.String(100), nullable=True)

class ContactRequest(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    phone = db_sql.Column(db_sql.String(30), nullable=False)
    attendant_name = db_sql.Column(db_sql.String(100), nullable=False)
    filial = db_sql.Column(db_sql.String(150), nullable=True)
    setor = db_sql.Column(db_sql.String(150), nullable=True)
    reason = db_sql.Column(db_sql.Text, nullable=False)
    status = db_sql.Column(db_sql.String(20), default='PENDING') # PENDING, ANSWERED
    created_at = db_sql.Column(db_sql.DateTime, default=get_now)
    is_first_time = db_sql.Column(db_sql.Boolean, default=True)

class Message(db_sql.Model):
    id = db_sql.Column(db_sql.String(100), primary_key=True)
    contact_id = db_sql.Column(db_sql.String(150), db_sql.ForeignKey('contact.id'), nullable=False)
    text = db_sql.Column(db_sql.Text, nullable=False)
    type = db_sql.Column(db_sql.String(20), nullable=False) # 'in' or 'out'
    time = db_sql.Column(db_sql.String(20), nullable=False)
    timestamp = db_sql.Column(db_sql.BigInteger, nullable=False)
    instance = db_sql.Column(db_sql.String(100), nullable=True)
    ack = db_sql.Column(db_sql.Integer, default=0, nullable=True)

class Setting(db_sql.Model):
    key = db_sql.Column(db_sql.String(50), primary_key=True)
    value = db_sql.Column(db_sql.Text, nullable=True)

class AtendimentoChat(db_sql.Model):
    __tablename__ = 'atendimentos_chat'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero = db_sql.Column(db_sql.Text, nullable=True, unique=True)
    status = db_sql.Column(db_sql.String(50), nullable=True)
    atendente = db_sql.Column(db_sql.String(100), nullable=True)
    registro_time_chat = db_sql.Column(db_sql.Text, nullable=True)
    ultimo_setor = db_sql.Column(db_sql.String(255), nullable=True)
    ultimo_atendente = db_sql.Column(db_sql.String(255), nullable=True)
    # Campos para controle de alertas de tempo de espera
    alerta_20min_enviado = db_sql.Column(db_sql.Boolean, default=False, nullable=False, server_default='false')
    alerta_40min_enviado = db_sql.Column(db_sql.Boolean, default=False, nullable=False, server_default='false')
    # Timestamp ISO de quando o status mudou para 'atendente' (início da espera)
    atendente_desde = db_sql.Column(db_sql.Text, nullable=True)

class SlaHistory(db_sql.Model):
    __tablename__ = 'sla_history'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero = db_sql.Column(db_sql.String(50), nullable=False)
    filial = db_sql.Column(db_sql.String(100), nullable=True)
    setor = db_sql.Column(db_sql.String(100), nullable=True)
    atendente = db_sql.Column(db_sql.String(100), nullable=True)
    tempo_na_fila_segundos = db_sql.Column(db_sql.Integer, nullable=True)
    tempo_primeira_resposta_segundos = db_sql.Column(db_sql.Integer, nullable=True)
    soma_tempo_resposta_segundos = db_sql.Column(db_sql.Integer, default=0)
    qtd_respostas_atendente = db_sql.Column(db_sql.Integer, default=0)
    ultimo_horario_mensagem_cliente = db_sql.Column(db_sql.Text, nullable=True)
    entrou_na_fila_em = db_sql.Column(db_sql.Text, nullable=True)
    assumido_em = db_sql.Column(db_sql.Text, nullable=True)
    finalizado_em = db_sql.Column(db_sql.Text, nullable=True)
    criado_em = db_sql.Column(db_sql.Text, nullable=False)

class TempoEspera(db_sql.Model):
    __tablename__ = 'tempo_espera'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero_cliente = db_sql.Column(db_sql.String(50), nullable=False)
    nome_atendente = db_sql.Column(db_sql.String(100), nullable=True)
    setor_filial = db_sql.Column(db_sql.String(150), nullable=True)
    inicio = db_sql.Column(db_sql.DateTime, nullable=False, default=get_now)
    atendido = db_sql.Column(db_sql.DateTime, nullable=True)
    finalizado = db_sql.Column(db_sql.DateTime, nullable=True)

class NPSVotos(db_sql.Model):
    __tablename__ = 'nps_votos'
    id = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    numero = db_sql.Column(db_sql.String(50))
    voto = db_sql.Column(db_sql.String(50), nullable=False)
    atendente = db_sql.Column(db_sql.String(150))
    filial = db_sql.Column(db_sql.String(150))
    setor = db_sql.Column(db_sql.String(150))
    timestamp = db_sql.Column(db_sql.DateTime, default=get_now)

class MotivoFinalizacao(db_sql.Model):
    __tablename__ = 'motivo_finalizacao'
    id             = db_sql.Column(db_sql.Integer, primary_key=True, autoincrement=True)
    contact_id     = db_sql.Column(db_sql.String(150), nullable=True)
    numero_cliente = db_sql.Column(db_sql.String(50), nullable=True)
    atendente      = db_sql.Column(db_sql.String(150), nullable=True)
    motivo         = db_sql.Column(db_sql.String(50), nullable=True)   # 'Venda', 'Orçamento', ou outro texto
    detalhes       = db_sql.Column(db_sql.Text, nullable=True)
    criado_em      = db_sql.Column(db_sql.DateTime, nullable=True, default=get_now)

class Entrega(db_sql.Model):
    id = db_sql.Column(db_sql.Integer, primary_key=True)
    nome_peca = db_sql.Column(db_sql.String(150), nullable=False)
    tamanho_peca = db_sql.Column(db_sql.String(50), nullable=True)
    nome_cliente = db_sql.Column(db_sql.String(150), nullable=False)
    localizacao = db_sql.Column(db_sql.String(255), nullable=True)
    latitude = db_sql.Column(db_sql.String(50), nullable=True)
    longitude = db_sql.Column(db_sql.String(50), nullable=True)
    telefone_cliente = db_sql.Column(db_sql.String(30), nullable=True)
    pago = db_sql.Column(db_sql.Boolean, default=False)
    forma_pagamento = db_sql.Column(db_sql.String(50), nullable=True)
    valor = db_sql.Column(db_sql.String(50), nullable=True)
    status = db_sql.Column(db_sql.String(50), default='Pronto para coleta')
    nome_atendente = db_sql.Column(db_sql.String(150), nullable=True)
    criado_em = db_sql.Column(db_sql.DateTime, default=get_now)
    codigo_verificacao = db_sql.Column(db_sql.String(20), nullable=True)
    entregador_id = db_sql.Column(db_sql.Integer, nullable=True)
    justificativa_falha = db_sql.Column(db_sql.Text, nullable=True)
    saiu_para_entrega_em = db_sql.Column(db_sql.Text, nullable=True)
    finalizado_em = db_sql.Column(db_sql.Text, nullable=True)
    numero_rota = db_sql.Column(db_sql.Integer, nullable=True)

# ─── Utils ──────────────────────────────────────────────────────────────────
def normalize_br_phone(phone_str):
    if not phone_str: return ""
    p = str(phone_str)
    p = p.split('@')[0]
    p = "".join(filter(str.isdigit, p))
    if len(p) == 13 and p.startswith('55') and p[4] == '9':
        p = p[:4] + p[5:]
    return p

def extract_waha_msg_id(res_data, fallback):
    m_id = res_data.get('id')
    if isinstance(m_id, dict):
        m_id = m_id.get('id')
    return m_id or res_data.get('key', {}).get('id') or res_data.get('messageId') or fallback

def get_media_base64(instance, msg_data):
    """Busca mídia da WAHA e salva localmente em data/media/. Retorna base64 se conseguir."""
    try:
        msg_id = msg_data.get('id')
        if not msg_id: return None
        
        # Verificar se já existe localmente
        import glob
        media_dir = os.path.join(DATA_DIR, 'media')
        short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id
        for check_id in [msg_id, short_id]:
            check_path = os.path.join(media_dir, check_id)
            matches = glob.glob(check_path + '.*')
            if os.path.exists(check_path):
                matches.insert(0, check_path)
            if matches:
                with open(matches[0], 'rb') as f:
                    import base64
                    return base64.b64encode(f.read()).decode('utf-8')
        
        url = f"{WAHA_API_URL}/api/files?session={instance}&messageId={msg_id}"
        res = requests.get(url, headers=get_waha_headers(), timeout=15)
        if res.status_code == 200:
            import base64
            file_bytes = res.content
            # Determinar extensão pela resposta do WAHA
            ctype = res.headers.get('Content-Type', '')
            import mimetypes as _mt_b64
            ext = _mt_b64.guess_extension(ctype.split(';')[0].strip()) or '.oga'
            # Salvar localmente para uso futuro
            try:
                os.makedirs(media_dir, exist_ok=True)
                for save_id in set([msg_id, short_id]):
                    save_path = os.path.join(media_dir, save_id + ext)
                    if not os.path.exists(save_path):
                        with open(save_path, 'wb') as f:
                            f.write(file_bytes)
                print(f"[Media] get_media_base64: salvo localmente para msg_id={msg_id}")
            except Exception as e_save:
                print(f"[Media] get_media_base64: erro ao salvar local: {e_save}")
            return base64.b64encode(file_bytes).decode('utf-8')
    except Exception as e:
        print(f"Erro ao baixar midia base64 (WAHA): {e}")
    return None

def track_sla_event(numero, filial=None, setor=None, atendente=None, event_type='QUEUE_ENTER'):
    """
    event_type: 'QUEUE_ENTER', 'ASSIGNED', 'CLIENT_MSG', 'ATTENDANT_MSG', 'RELEASED'
    """
    try:
        now_iso = get_now().isoformat()
        
        # Busca SLA atual em aberto (sem finalizado_em)
        sla = SlaHistory.query.filter_by(numero=numero).filter(SlaHistory.finalizado_em == None).order_by(SlaHistory.id.desc()).first()
        
        if event_type == 'QUEUE_ENTER':
            # Se já houver um, vamos fechar para abrir um novo ciclo na fila
            if sla:
                sla.finalizado_em = now_iso
            # Cria novo
            sla = SlaHistory(
                numero=numero, filial=filial, setor=setor,
                entrou_na_fila_em=now_iso, criado_em=now_iso
            )
            db_sql.session.add(sla)
            
        elif sla:
            if event_type == 'ASSIGNED':
                sla.atendente = atendente
                sla.assumido_em = now_iso
                if sla.entrou_na_fila_em:
                    dt_fila = datetime.datetime.fromisoformat(sla.entrou_na_fila_em)
                    sla.tempo_na_fila_segundos = int((get_now() - dt_fila).total_seconds())
                    
            elif event_type == 'CLIENT_MSG':
                # Só registra se já foi assumido por alguém
                if sla.assumido_em:
                    sla.ultimo_horario_mensagem_cliente = now_iso
                    
            elif event_type == 'ATTENDANT_MSG':
                # Primeira resposta
                if not sla.tempo_primeira_resposta_segundos and sla.assumido_em:
                    dt_ass = datetime.datetime.fromisoformat(sla.assumido_em)
                    sla.tempo_primeira_resposta_segundos = int((get_now() - dt_ass).total_seconds())
                    
                # Respostas subsequentes
                if sla.ultimo_horario_mensagem_cliente:
                    dt_cliente = datetime.datetime.fromisoformat(sla.ultimo_horario_mensagem_cliente)
                    diff = int((get_now() - dt_cliente).total_seconds())
                    if diff < 0: diff = 0
                    sla.soma_tempo_resposta_segundos = (sla.soma_tempo_resposta_segundos or 0) + diff
                    sla.qtd_respostas_atendente = (sla.qtd_respostas_atendente or 0) + 1
                    # Limpa o último horário para não contar de novo a mesma msg
                    sla.ultimo_horario_mensagem_cliente = None
                    
            elif event_type == 'RELEASED':
                sla.finalizado_em = now_iso
                
        db_sql.session.commit()
    except Exception as e:
        db_sql.session.rollback()
        print(f"Erro em track_sla_event: {e}")

# ─── Database JSON Fallback / Migration ──────────────────────────────────────
def load_db():
    target_path = DB_PATH
    if not os.path.exists(target_path):
        legacy_path = os.path.join(ROOT_DIR, 'db.json')
        if os.path.exists(legacy_path):
            target_path = legacy_path
        else:
            return {"users": [], "instances": {}, "contacts": [], "messages": {}}
    try:
        with open(target_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"users": [], "instances": {}, "contacts": [], "messages": {}}


def migrate_to_sql():
    with app.app_context():
        db_sql.create_all()
        
        # Add new columns if missing
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN filial_id INTEGER REFERENCES filial(id)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN setor_id INTEGER REFERENCES setor(id)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN filial VARCHAR(150)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN setor VARCHAR(150)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "user" ADD COLUMN phone VARCHAR(30)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()

        # Add new columns for Entrega if missing
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN nome_atendente VARCHAR(150)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN latitude VARCHAR(50)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN longitude VARCHAR(50)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN justificativa_falha TEXT'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()

        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN codigo_verificacao VARCHAR(20)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN entregador_id INTEGER'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN saiu_para_entrega_em TEXT'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN finalizado_em TEXT'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE "entrega" ADD COLUMN numero_rota INTEGER'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
        
        # Add assignment columns to contact
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ADD COLUMN assigned_to INTEGER REFERENCES "user"(id)'))
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ADD COLUMN assigned_name VARCHAR(100)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
        
        # Se Users estiver vazio, tenta migrar do JSON
        
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE contact ALTER COLUMN last_msg_time TYPE VARCHAR(20)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE message ALTER COLUMN time TYPE VARCHAR(20)'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()
            
        # Migração das rotas antigas
        try:
            db_sql.session.execute(db_sql.text("""
                CREATE TABLE IF NOT EXISTS tempo_espera (
                    id SERIAL PRIMARY KEY,
                    numero_cliente VARCHAR(50) NOT NULL,
                    nome_atendente VARCHAR(100),
                    setor_filial VARCHAR(150),
                    inicio TIMESTAMP NOT NULL DEFAULT NOW(),
                    atendido TIMESTAMP,
                    finalizado TIMESTAMP
                )
            """))
            db_sql.session.commit()
            print("[MIGRATE] Tabela tempo_espera verificada/criada.")
        except Exception as e_te:
            db_sql.session.rollback()
            print(f"[MIGRATE] tempo_espera: {e_te}")

        try:
            db_sql.session.execute(db_sql.text("""
                CREATE TABLE IF NOT EXISTS nps_votos (
                    id SERIAL PRIMARY KEY,
                    numero VARCHAR(50),
                    voto VARCHAR(50) NOT NULL,
                    atendente VARCHAR(150),
                    filial VARCHAR(150),
                    setor VARCHAR(150),
                    timestamp TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
            db_sql.session.commit()
            print("[MIGRATE] Tabela nps_votos verificada/criada.")
        except Exception as e_nps:
            db_sql.session.rollback()
            print(f"[MIGRATE] nps_votos: {e_nps}")

        try:
            db_sql.session.execute(db_sql.text("""
                CREATE TABLE IF NOT EXISTS motivo_finalizacao (
                    id SERIAL PRIMARY KEY,
                    contact_id VARCHAR(150),
                    numero_cliente VARCHAR(50),
                    atendente VARCHAR(150),
                    motivo VARCHAR(50),
                    detalhes TEXT,
                    criado_em TIMESTAMP DEFAULT NOW()
                )
            """))
            db_sql.session.commit()
            print("[MIGRATE] Tabela motivo_finalizacao verificada/criada.")
        except Exception as e_mf:
            db_sql.session.rollback()
            print(f"[MIGRATE] motivo_finalizacao: {e_mf}")

        try:
            velhas = Entrega.query.filter(
                Entrega.entregador_id.isnot(None),
                Entrega.saiu_para_entrega_em.isnot(None),
                Entrega.numero_rota.is_(None)
            ).order_by(Entrega.saiu_para_entrega_em).all()
            
            if velhas:
                max_nr = db_sql.session.query(db_sql.func.max(Entrega.numero_rota)).scalar() or 0
                current_nr = max_nr + 1
                
                ent_por_entregador = {}
                for e in velhas:
                    if e.entregador_id not in ent_por_entregador:
                        ent_por_entregador[e.entregador_id] = []
                    ent_por_entregador[e.entregador_id].append(e)
                    
                for eid, lst in ent_por_entregador.items():
                    current_rota = None
                    for e in lst:
                        try:
                            dt_saiu = datetime.datetime.fromisoformat(e.saiu_para_entrega_em.replace('Z', '+00:00'))
                        except:
                            continue
                            
                        if not current_rota:
                            current_rota = {'dt_saiu': dt_saiu, 'entregas': [e], 'nr': current_nr}
                            current_nr += 1
                        else:
                            diff_seconds = (dt_saiu - current_rota['dt_saiu']).total_seconds()
                            if diff_seconds > 30 * 60:
                                current_rota = {'dt_saiu': dt_saiu, 'entregas': [e], 'nr': current_nr}
                                current_nr += 1
                            else:
                                current_rota['entregas'].append(e)
                                
                        e.numero_rota = current_rota['nr']
                db_sql.session.commit()
        except Exception as ex:
            print("Erro migrando rotas velhas:", ex)
            db_sql.session.rollback()
            
        try:
            db_sql.session.execute(db_sql.text('ALTER TABLE message ADD COLUMN ack INTEGER DEFAULT 0'))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()

        if User.query.first() is None:
            print("Migrando dados do JSON para o SQL...")
            old_db = load_db()
            
            # Migrar Usuários
            for u in old_db.get('users', []):
                new_u = User(id=u['id'], name=u['name'], email=u['email'], 
                             password=u.get('password', '123456'), role=u.get('role', 'user'),
                             instances=old_db.get('userInstances', {}).get(str(u['id']), []))
                db_sql.session.add(new_u)
            
            # Migrar Contatos
            for c in old_db.get('contacts', []):
                new_c = Contact(id=c['id'], name=c['name'], phone=c['phone'], 
                                avatar=c.get('avatar'), instance=c.get('instance'),
                                tags=c.get('tags', []), last_msg=c.get('lastMsg'),
                                last_msg_time=c.get('time'), unread=c.get('unread', 0))
                db_sql.session.add(new_c)
            
            # Migrar Mensagens
            for phone, msgs in old_db.get('messages', {}).items():
                for m in msgs:
                    # Tenta descobrir a instancia (nao vai ser perfeito pra msgs velhas sem instance)
                    inst = m.get('instance', 'default')
                    cid = f"c_{phone}_{inst}"
                    if not Message.query.get(m['id']):
                        new_m = Message(id=m['id'], contact_id=cid, text=m['text'],
                                       type=m['type'], time=m['time'], 
                                       timestamp=m.get('timestamp', 0), instance=inst)
                        db_sql.session.add(new_m)
            
            # Migrar Settings
            settings = old_db.get('settings', {})
            for k, v in settings.items():
                new_s = Setting(key=k, value=str(v))
                db_sql.session.add(new_s)
            
            db_sql.session.commit()
            print("Migração concluída.")

        # Migração dos campos de alerta de tempo de espera (compatível com SQLite e PostgreSQL)
        for col_def in [
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS alerta_20min_enviado BOOLEAN DEFAULT FALSE",
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS alerta_40min_enviado BOOLEAN DEFAULT FALSE",
            "ALTER TABLE atendimentos_chat ADD COLUMN IF NOT EXISTS atendente_desde TEXT",
        ]:
            try:
                db_sql.session.execute(db_sql.text(col_def))
                db_sql.session.commit()
                print(f"[MIGRATE] Coluna adicionada/verificada: {col_def.split('IF NOT EXISTS ')[1].split(' ')[0]}")
            except Exception as e_col:
                db_sql.session.rollback()
                # SQLite não suporta IF NOT EXISTS, tenta sem ele
                try:
                    col_def_sqlite = col_def.replace('IF NOT EXISTS ', '').replace('DEFAULT FALSE', 'DEFAULT 0')
                    db_sql.session.execute(db_sql.text(col_def_sqlite))
                    db_sql.session.commit()
                    print(f"[MIGRATE] Coluna adicionada (SQLite fallback): {col_def_sqlite.split('ADD COLUMN ')[1].split(' ')[0]}")
                except Exception:
                    db_sql.session.rollback()  # Coluna já existe ou outro erro ignorado
        
        # Garantir que existe pelo menos um ADMIN se o banco estiver vazio
        if User.query.filter_by(role='admin').first() is None:
            print("Criando usuário administrador padrão...")
            admin_email = os.getenv('ADMIN_EMAIL', 'admin@admin.com')
            admin_pass = os.getenv('ADMIN_PASSWORD', 'admin123')
            admin = User(
                name="Administrador",
                email=admin_email,
                password=admin_pass,
                role="admin",
                instances=[]
            )
            db_sql.session.add(admin)
            db_sql.session.commit()
            print(f"Usuário {admin_email} criado (senha: {admin_pass}).")

migrate_to_sql()

# ─── Middleware ─────────────────────────────────────────────────────────────

@app.before_request
def log_request_info():
    if not request.path.startswith('/static') and request.path != '/api/whatsapp/instances':
        print(f"Solicitação: {request.method} {request.path}")

def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'Não autorizado - Sem token'}), 401
        try:
            token = token.split(" ")[1]
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user = data
        except Exception as e:
            return jsonify({'error': 'Token inválido ou expirado', 'details': str(e)}), 401
        
        return f(*args, **kwargs)
    return decorated

# ─── Entregas Routes ────────────────────────────────────────────────────────

@app.route('/api/entregas', methods=['GET'])
@auth_required
def get_entregas():
    entregas = Entrega.query.order_by(Entrega.id.desc()).all()
    return jsonify([{
        'id': e.id,
        'nome_peca': e.nome_peca,
        'tamanho_peca': e.tamanho_peca,
        'nome_cliente': e.nome_cliente,
        'localizacao': e.localizacao,
        'telefone_cliente': e.telefone_cliente,
        'pago': e.pago,
        'forma_pagamento': e.forma_pagamento,
        'valor': e.valor,
        'status': e.status,
        'nome_atendente': e.nome_atendente,
        'latitude': e.latitude,
        'longitude': e.longitude,
        'codigo_verificacao': e.codigo_verificacao,
        'entregador_id': e.entregador_id,
        'justificativa_falha': e.justificativa_falha,
        'criado_em': e.criado_em.isoformat() if e.criado_em else None
    } for e in entregas])

@app.route('/api/entregador/entregas/disponiveis', methods=['GET'])
@auth_required
def get_entregas_disponiveis():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado. Apenas entregadores.'}), 403
        
    # Retorna entregas com status "Pronto para coleta"
    entregas = Entrega.query.filter_by(status='Pronto para coleta').order_by(Entrega.id.desc()).all()
    return jsonify([{
        'id': e.id,
        'nome_peca': e.nome_peca,
        'tamanho_peca': e.tamanho_peca,
        'nome_cliente': e.nome_cliente,
        'localizacao': e.localizacao,
        'telefone_cliente': e.telefone_cliente,
        'pago': e.pago,
        'forma_pagamento': e.forma_pagamento,
        'valor': e.valor,
        'status': e.status,
        'nome_atendente': e.nome_atendente,
        'latitude': e.latitude,
        'longitude': e.longitude,
        'entregador_id': e.entregador_id,
        'justificativa_falha': e.justificativa_falha,
        'criado_em': e.criado_em.isoformat() if e.criado_em else None
    } for e in entregas])

@app.route('/api/entregador/push/subscribe', methods=['POST'])
@auth_required
def push_subscribe():
    user_id = request.user.get('id')
    sub_data = request.json.get('subscription')
    if not sub_data:
        return jsonify({'error': 'Subscription data missing'}), 400
        
    endpoint = sub_data.get('endpoint')
    keys = sub_data.get('keys', {})
    p256dh = keys.get('p256dh')
    auth = keys.get('auth')
    
    if not endpoint or not p256dh or not auth:
        return jsonify({'error': 'Invalid subscription data'}), 400
        
    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if not sub:
        sub = PushSubscription(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        db_sql.session.add(sub)
    else:
        sub.user_id = user_id
        sub.p256dh = p256dh
        sub.auth = auth
    db_sql.session.commit()
    return jsonify({'success': True})

def send_push_to_entregadores(title, body):
    from pywebpush import webpush, WebPushException
    from urllib.parse import urlparse
    import json
    
    # Obter todos os usuários entregadores
    entregadores_ids = [u.id for u in User.query.filter_by(role='entregador').all()]
    if not entregadores_ids:
        return
        
    subs = PushSubscription.query.filter(PushSubscription.user_id.in_(entregadores_ids)).all()
    if not subs:
        return
        
    vapid_private_key = os.environ.get('VAPID_PRIVATE_KEY', 'BAMfTYg3RHaf4cFRuMZa9T1RvZFr7gsUDLBw6CqwzSU')
    payload = json.dumps({
        "title": title,
        "body": body,
        "url": "/entregador"
    })
    
    vapid_claims = {
        "sub": "mailto:contato@presidentedisel.com"
    }
    
    for sub in subs:
        sub_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.p256dh,
                "auth": sub.auth
            }
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims
            )
        except Exception as ex:
            if hasattr(ex, 'response') and ex.response is not None:
                if getattr(ex.response, 'status_code', 500) in [404, 410]:
                    db_sql.session.delete(sub)
    try:
        db_sql.session.commit()
    except:
        db_sql.session.rollback()

@app.route('/api/admin/push/test', methods=['POST'])
@auth_required
def push_test():
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
        
    from pywebpush import webpush, WebPushException
    from urllib.parse import urlparse
    import time
    
    subs = PushSubscription.query.all()
    # Chave privada VAPID em formato raw base64url (não PEM)
    # O pywebpush aceita a chave bruta de 32 bytes codificada em base64url
    vapid_private_key = os.environ.get('VAPID_PRIVATE_KEY', 'BAMfTYg3RHaf4cFRuMZa9T1RvZFr7gsUDLBw6CqwzSU')
    
    payload = json.dumps({
        "title": "Teste WPCRM",
        "body": "Esta é uma notificação nativa do PWA! Se estiver lendo isso com o app fechado, funcionou perfeitamente.",
        "url": "/entregador"
    })
    
    success_count = 0
    errors = []
    for sub in subs:
        sub_info = {
            "endpoint": sub.endpoint,
            "keys": {
                "p256dh": sub.p256dh,
                "auth": sub.auth
            }
        }
        # Apple exige que aud, exp e sub estejam EXPLÍCITOS no JWT
        parsed = urlparse(sub.endpoint)
        aud = f"{parsed.scheme}://{parsed.netloc}"
        vapid_claims = {
            "sub": "mailto:contato@presidentedisel.com"
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims
            )
            success_count += 1
        except Exception as ex:
            err_msg = str(ex)
            # Captura body da resposta para debug
            if hasattr(ex, 'response') and ex.response is not None:
                err_msg = f"Status {ex.response.status_code}: {ex.response.text}"
                # Se o endpoint não for mais válido, removemos
                if getattr(ex.response, 'status_code', 500) in [404, 410]:
                    db_sql.session.delete(sub)
            print(f"WebPush Error para {aud}: {err_msg}")
            errors.append(f"{aud}: {err_msg}")
                
    db_sql.session.commit()
    return jsonify({'success': True, 'sent': success_count, 'total_subs': len(subs), 'errors': errors})

@app.route('/api/entregador/location', methods=['POST'])
@auth_required
def update_driver_location():
    user_id = request.user.get('id')
    data = request.json
    lat = data.get('lat')
    lng = data.get('lng')
    
    if lat is None or lng is None:
        return jsonify({'error': 'Coordenadas ausentes'}), 400
        
    try:
        lat = float(lat)
        lng = float(lng)
    except ValueError:
        return jsonify({'error': 'Coordenadas inválidas'}), 400
        
    loc = DriverLocation.query.filter_by(user_id=user_id).first()
    if not loc:
        loc = DriverLocation(user_id=user_id, lat=lat, lng=lng)
        db_sql.session.add(loc)
    else:
        loc.lat = lat
        loc.lng = lng
        loc.updated_at = get_now()
        
    db_sql.session.commit()
    return jsonify({'success': True})

@app.route('/api/admin/drivers/locations', methods=['GET'])
@auth_required
def get_drivers_locations():
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
        
    # Pega localizações que foram atualizadas nas últimas 2 horas
    two_hours_ago = get_now() - datetime.timedelta(hours=2)
    locations = DriverLocation.query.filter(DriverLocation.updated_at >= two_hours_ago).all()
    
    results = []
    for loc in locations:
        results.append({
            'user_id': loc.user_id,
            'name': loc.user.name if loc.user else 'Desconhecido',
            'lat': loc.lat,
            'lng': loc.lng,
            'updated_at': loc.updated_at.isoformat() + 'Z'
        })
        
    return jsonify({'locations': results})

@app.route('/api/entregas', methods=['POST'])
@auth_required
def create_entrega():
    data = request.json
    
    # Gerar código de verificação: 2 letras e 2 números embaralhados
    import random, string
    letras = random.choices(string.ascii_uppercase, k=2)
    numeros = random.choices(string.digits, k=2)
    codigo_lista = letras + numeros
    random.shuffle(codigo_lista)
    codigo = ''.join(codigo_lista)

    nova_entrega = Entrega(
        nome_peca=data.get('nome_peca'),
        tamanho_peca=data.get('tamanho_peca'),
        nome_cliente=data.get('nome_cliente'),
        localizacao=data.get('localizacao'),
        telefone_cliente=data.get('telefone_cliente'),
        pago=data.get('pago', False),
        forma_pagamento=data.get('forma_pagamento'),
        valor=data.get('valor'),
        status=data.get('status', 'Pronto para coleta'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        nome_atendente=User.query.get(request.user['id']).name if User.query.get(request.user['id']) else 'Desconhecido',
        codigo_verificacao=codigo
    )
    db_sql.session.add(nova_entrega)
    db_sql.session.commit()
    
    # Disparar notificação push para os entregadores de forma assíncrona/não bloqueante
    try:
        send_push_to_entregadores(
            title="Nova Entrega Disponível!",
            body=f"Temos uma nova entrega para {nova_entrega.nome_cliente}. Acesse para coletar!"
        )
    except Exception as e:
        print("Erro ao enviar push de nova entrega:", e)
        
    socketio.emit('nova_entrega', {
        'id': nova_entrega.id, 
        'nome_cliente': nova_entrega.nome_cliente,
        'localizacao': nova_entrega.localizacao
    })
        
    return jsonify({'success': True, 'id': nova_entrega.id, 'codigo': codigo})

@app.route('/api/entregas/<int:id>/status', methods=['PUT'])
@auth_required
def update_entrega_status(id):
    entrega = Entrega.query.get(id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
    data = request.json
    entrega.status = data.get('status', entrega.status)
    db_sql.session.commit()
    return jsonify({'success': True, 'status': entrega.status})

@app.route('/api/entregas/<int:id>', methods=['DELETE'])
@auth_required
def delete_entrega(id):
    # Opcional: verificar se usuário tem cargo admin, ou fazer a verificação apenas no frontend se o backend confia no auth
    entrega = Entrega.query.get(id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
    db_sql.session.delete(entrega)
    db_sql.session.commit()
    return jsonify({'success': True, 'message': 'Entrega deletada com sucesso'}), 200

@app.route('/api/entregas/<int:id>', methods=['PUT'])
@auth_required
def update_entrega_full(id):
    entrega = Entrega.query.get(id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
    
    data = request.json
    if 'nome_peca' in data: entrega.nome_peca = data['nome_peca']
    if 'tamanho_peca' in data: entrega.tamanho_peca = data['tamanho_peca']
    if 'nome_cliente' in data: entrega.nome_cliente = data['nome_cliente']
    if 'localizacao' in data: entrega.localizacao = data['localizacao']
    if 'telefone_cliente' in data: entrega.telefone_cliente = data['telefone_cliente']
    if 'pago' in data: entrega.pago = data['pago']
    if 'forma_pagamento' in data: entrega.forma_pagamento = data['forma_pagamento']
    if 'valor' in data: entrega.valor = data['valor']
    if 'status' in data: entrega.status = data['status']
    if 'latitude' in data: entrega.latitude = data['latitude']
    if 'longitude' in data: entrega.longitude = data['longitude']
    
    db_sql.session.commit()
    return jsonify({'success': True})

@app.route('/api/entregador/aceitar_entrega', methods=['POST'])
@auth_required
def aceitar_entrega():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado'}), 403
        
    data = request.json
    entrega_id = data.get('entrega_id')
    codigo_fornecido = data.get('codigo_verificacao', '').upper().strip()
    
    entrega = Entrega.query.get(entrega_id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
        
    if entrega.status != 'Pronto para coleta':
        return jsonify({'error': 'Entrega já foi aceita por outro entregador ou não está disponível'}), 400
        
    if not entrega.codigo_verificacao or entrega.codigo_verificacao != codigo_fornecido:
        return jsonify({'error': 'Código de verificação incorreto'}), 400
        
    entrega.status = 'Saiu para entrega'
    entrega.entregador_id = request.user.get('id')
    db_sql.session.commit()
    return jsonify({'success': True, 'status': entrega.status})

@app.route('/api/entregador/entregas/minhas', methods=['GET'])
@auth_required
def get_minhas_entregas():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado. Apenas entregadores.'}), 403
        
    user_id = request.user.get('id')
    # Retorna entregas vinculadas ao entregador com status "Saiu para entrega"
    entregas = Entrega.query.filter_by(entregador_id=user_id, status='Saiu para entrega').order_by(Entrega.id.desc()).all()
    return jsonify([{
        'id': e.id,
        'nome_peca': e.nome_peca,
        'tamanho_peca': e.tamanho_peca,
        'nome_cliente': e.nome_cliente,
        'localizacao': e.localizacao,
        'telefone_cliente': e.telefone_cliente,
        'pago': e.pago,
        'forma_pagamento': e.forma_pagamento,
        'valor': e.valor,
        'status': e.status,
        'nome_atendente': e.nome_atendente,
        'latitude': e.latitude,
        'longitude': e.longitude,
        'entregador_id': e.entregador_id,
        'justificativa_falha': e.justificativa_falha,
        'criado_em': e.criado_em.isoformat() if e.criado_em else None
    } for e in entregas])

@app.route('/api/entregador/falha_entrega', methods=['POST'])
@auth_required
def falha_entrega():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado'}), 403
        
    data = request.json
    entrega_id = data.get('entrega_id')
    justificativa = data.get('justificativa', '')
    
    entrega = Entrega.query.get(entrega_id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
        
    user_id = request.user.get('id')
    if entrega.entregador_id != user_id and request.user.get('role') != 'admin':
        return jsonify({'error': 'Você não tem permissão para alterar esta entrega'}), 403
        
    entrega.status = 'Falha na Entrega'
    entrega.justificativa_falha = justificativa
    entrega.finalizado_em = get_now().isoformat()
    db_sql.session.commit()
    return jsonify({'success': True, 'status': entrega.status, 'justificativa_falha': entrega.justificativa_falha})

# ==========================================
# ADMIN ENTREGADORES
# ==========================================
@app.route('/api/admin/entregadores', methods=['GET'])
@auth_required
def get_admin_entregadores():
    # Retorna usuários do tipo entregador e suas entregas ativas
    if request.user.get('role') != 'admin' and request.user.get('role') != 'gestor':
        return jsonify({'error': 'Acesso negado'}), 403
        
    entregadores = User.query.filter_by(role='entregador').all()
    resultado = []
    
    for ent in entregadores:
        entregas_ativas = Entrega.query.filter(
            Entrega.entregador_id == ent.id,
            Entrega.status.notin_(['Entregue', 'Falha na Entrega'])
        ).all()
        
        resultado.append({
            'id': ent.id,
            'name': ent.name,
            'email': ent.email,
            'entregas_ativas': [{
                'id': e.id,
                'nome_cliente': e.nome_cliente,
                'status': e.status
            } for e in entregas_ativas]
        })
        
    return jsonify(resultado)

@app.route('/api/admin/entregadores/desativar_rota', methods=['POST'])
@auth_required
def desativar_rota():
    if request.user.get('role') != 'admin' and request.user.get('role') != 'gestor':
        return jsonify({'error': 'Acesso negado'}), 403
        
    data = request.json
    entregador_id = data.get('entregador_id')
    
    entregas_ativas = Entrega.query.filter(
        Entrega.entregador_id == entregador_id,
        Entrega.status.notin_(['Entregue', 'Falha na Entrega'])
    ).all()
    
    for e in entregas_ativas:
        e.entregador_id = None
        e.status = 'Pronto para coleta'
        e.codigo_verificacao = None
        
    db_sql.session.commit()
    return jsonify({'success': True, 'modificadas': len(entregas_ativas)})

@app.route('/api/admin/entregadores/transferir', methods=['POST'])
@auth_required
def transferir_rota():
    if request.user.get('role') != 'admin' and request.user.get('role') != 'gestor':
        return jsonify({'error': 'Acesso negado'}), 403
        
    data = request.json
    origem_id = data.get('origem_id')
    destino_id = data.get('destino_id')
    
    entregas_ativas = Entrega.query.filter(
        Entrega.entregador_id == origem_id,
        Entrega.status.notin_(['Entregue', 'Falha na Entrega'])
    ).all()
    
    for e in entregas_ativas:
        e.entregador_id = destino_id
        
    db_sql.session.commit()
    return jsonify({'success': True, 'modificadas': len(entregas_ativas)})

@app.route('/api/admin/entregadores/historico_rotas', methods=['GET'])
@auth_required
def get_historico_rotas():
    if request.user.get('role') not in ('admin', 'gestor'):
        return jsonify({'error': 'Acesso negado'}), 403

    # Busca entregas que têm um numero_rota definido
    entregas = Entrega.query.filter(
        Entrega.entregador_id.isnot(None),
        Entrega.numero_rota.isnot(None)
    ).order_by(Entrega.numero_rota.desc()).all()

    users = {u.id: u.name for u in User.query.filter_by(role='entregador').all()}
    
    # Agrupar entregas por numero_rota
    rotas_dict = {}
    
    for e in entregas:
        nr = e.numero_rota
        if nr not in rotas_dict:
            entregador_nome = users.get(e.entregador_id, f'Entregador #{e.entregador_id}')
            rotas_dict[nr] = {
                'numero_rota': nr,
                'entregador_nome': entregador_nome,
                'saiu_para_entrega_em': e.saiu_para_entrega_em,
                'entregas': [],
                'finalizado_em': e.finalizado_em
            }
            
        # Adiciona a entrega na rota atual
        rotas_dict[nr]['entregas'].append({
            'id': e.id,
            'cliente': e.nome_cliente,
            'status': e.status,
            'justificativa': e.justificativa_falha,
            'finalizado_em': e.finalizado_em
        })
        
        # Atualiza o finalizado_em da rota para ser o mais recente
        if e.finalizado_em:
            if not rotas_dict[nr]['finalizado_em'] or e.finalizado_em > rotas_dict[nr]['finalizado_em']:
                rotas_dict[nr]['finalizado_em'] = e.finalizado_em
                
    # Converte o dict para lista
    historico = list(rotas_dict.values())
    
    # Ordena da rota mais recente para a mais antiga pelo numero_rota
    historico.sort(key=lambda x: x['numero_rota'], reverse=True)

    return jsonify(historico)

@app.route('/api/admin/gerar_codigo', methods=['POST'])
@auth_required
def gerar_codigo():
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
        
    import random
    import json
    import time
    
    # Gera 6 dígitos aleatórios
    codigo = str(random.randint(100000, 999999))
    # Validade: 10 minutos (600 segundos)
    expires_at = int(time.time()) + 600
    
    # Salva ou atualiza a config
    config = Setting.query.get('override_code')
    if not config:
        config = Setting(key='override_code')
        db_sql.session.add(config)
        
    config.value = json.dumps({'code': codigo, 'expires_at': expires_at})
    db_sql.session.commit()
    
    return jsonify({'success': True, 'codigo': codigo})

@app.route('/api/entregador/verificar_codigo', methods=['POST'])
@auth_required
def verificar_codigo():
    import json
    import time
    
    data = request.json
    codigo_inserido = data.get('codigo')
    
    if not codigo_inserido:
        return jsonify({'error': 'Código não fornecido'}), 400
        
    config = Setting.query.get('override_code')
    if not config or not config.value:
        return jsonify({'error': 'Nenhum código ativo no momento.'}), 400
        
    try:
        dados = json.loads(config.value)
        if int(time.time()) > dados.get('expires_at', 0):
            return jsonify({'error': 'Este código expirou.'}), 400
            
        if codigo_inserido.strip() != dados.get('code'):
            return jsonify({'error': 'Código incorreto.'}), 400
            
        return jsonify({'success': True})
    except:
        return jsonify({'error': 'Erro ao validar código interno.'}), 500

@app.route('/api/entregador/concluir_entrega', methods=['POST'])
@auth_required
def concluir_entrega():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado'}), 403
        
    data = request.json
    entrega_id = data.get('entrega_id')
    justificativa_distancia = data.get('justificativa_distancia')
    
    entrega = Entrega.query.get(entrega_id)
    if not entrega:
        return jsonify({'error': 'Entrega não encontrada'}), 404
        
    # Somente o próprio entregador (ou admin) pode concluir
    user_id = request.user.get('id')
    if entrega.entregador_id != user_id and request.user.get('role') != 'admin':
        return jsonify({'error': 'Você não tem permissão para concluir esta entrega'}), 403
        
    entrega.status = 'Entregue'
    entrega.finalizado_em = get_now().isoformat()
    if justificativa_distancia:
        # Check if attribute exists in case the table hasn't been altered yet
        if hasattr(entrega, 'justificativa_distancia'):
            entrega.justificativa_distancia = justificativa_distancia
            
    db_sql.session.commit()
    return jsonify({'success': True, 'status': entrega.status})

@app.route('/api/entregador/otimizar_rota', methods=['POST'])
@auth_required
def otimizar_rota():
    if request.user.get('role') not in ('entregador', 'admin'):
        return jsonify({'error': 'Acesso negado'}), 403
    
    data = request.json
    coords = data.get('coords')
    if not coords:
        return jsonify({'error': 'Coordenadas não fornecidas'}), 400
        
    try:
        url = f"https://router.project-osrm.org/trip/v1/driving/{coords}?roundtrip=false&source=first"
        # Faz a requisição no backend para evitar problemas de CORS no frontend
        res = requests.get(url, timeout=10)
        
        if res.status_code != 200:
            return jsonify({'error': f'Falha no OSRM: {res.status_code}'}), 400
            
        # O OSRM retornou sucesso, o que significa que o motoboy iniciou uma Rota válida.
        # Vamos gerar um numero_rota e atualizar as entregas pendentes dele
        user_id = request.user.get('id')
        entregas_ativas = Entrega.query.filter_by(entregador_id=user_id, status='Saiu para entrega', numero_rota=None).all()
        
        if entregas_ativas:
            max_rota = db_sql.session.query(db_sql.func.max(Entrega.numero_rota)).scalar() or 0
            novo_numero_rota = max_rota + 1
            agora = get_now().isoformat()
            
            for e in entregas_ativas:
                e.numero_rota = novo_numero_rota
                e.saiu_para_entrega_em = agora
                
            db_sql.session.commit()
            
        return jsonify(res.json())
    except Exception as e:
        print(f"Erro ao otimizar rota: {e}")
        return jsonify({'error': str(e)}), 500

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.user.get('role') != 'admin':
            return jsonify({'error': 'Acesso negado. Apenas administradores.'}), 403
        return f(*args, **kwargs)
    return decorated

def admin_or_gestor_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        role = request.user.get('role')
        if role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'port': 3008,
        'waha_url': WAHA_API_URL
    })

@app.route('/api/bot/tags', methods=['POST'])
def add_bot_tag():
    """Rota para o N8N (ou outro bot) adicionar etiquetas via API"""
    data = request.json
    if not data:
        return jsonify({'error': 'Body vazio'}), 400
        
    phone = data.get('phone')
    inst = data.get('instance')
    filial = data.get('filial')
    setor = data.get('setor')
    custom_tag = data.get('tag')
    
    if not phone or not inst:
        return jsonify({'error': 'phone e instance são obrigatórios'}), 400
        
    if custom_tag and ':' in custom_tag and not (filial and setor):
        partes = custom_tag.split(':', 1)
        filial = partes[0].strip()
        setor = partes[1].strip()
        
    phone = normalize_br_phone(str(phone).strip())
    inst = str(inst).strip()
    contact_id = f"c_{phone}_{inst}"
    
    print(f"[BOT/TAGS] Buscando contato: id={contact_id}, phone={phone}, instance={inst}")
    
    # Tentativa 1: busca pelo ID exato
    contact = Contact.query.filter_by(id=contact_id).first()
    
    # Tentativa 2: busca pelo phone + instance (caso o ID tenha alguma diferença)
    if not contact:
        contact = Contact.query.filter_by(phone=phone, instance=inst).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado por phone+instance: {contact.id}")
    
    # Tentativa 3: busca apenas pelo phone (pega o mais recente)
    if not contact:
        contact = Contact.query.filter_by(phone=phone).first()
        if contact:
            print(f"[BOT/TAGS] Encontrado apenas por phone: {contact.id} (instance no banco: {contact.instance})")
    
    if not contact:
        print(f"[BOT/TAGS] Contato não encontrado, criando novo: {contact_id}")
        contact = Contact(
            id=contact_id, name=phone, phone=phone,
            avatar=phone[0] if phone else "?", instance=inst,
            tags=['Novo Lead'], last_msg='', last_msg_time='', unread=0
        )
        db_sql.session.add(contact)
        db_sql.session.flush()
        
    current_tags = list(contact.tags or [])
    added = False
    
    if filial and setor:
        new_ftag = f"{filial}:{setor}"
        # Verifica se já existe alguma tag de filial:setor
        existing_filial_tags = [
            t for t in current_tags
            if isinstance(t, str) and ':' in t and not t.lower().startswith('atendente:')
        ]
        
        if new_ftag in current_tags:
            # Já tem a tag correta — não precisa alterar
            print(f"[BOT/TAGS] Tag '{new_ftag}' já existe, nenhuma alteração necessária.")
        else:
            # Remove qualquer tag Filial:Setor antiga e adiciona a nova
            for old_tag in existing_filial_tags:
                current_tags.remove(old_tag)
                print(f"[BOT/TAGS] Removendo tag antiga: {old_tag}")
            current_tags.append(new_ftag)
            added = True
            
            # Registra no SLA que o chat entrou na fila deste setor
            track_sla_event(phone, filial=filial, setor=setor, event_type='QUEUE_ENTER')
    elif filial:
        if filial not in current_tags:
            current_tags.append(filial)
            added = True
            
    if custom_tag and custom_tag not in current_tags:
        current_tags.append(custom_tag)
        added = True
        
    # --- Início da Lógica de Distribuição Igualitária (Round-Robin) ---
    if not contact.assigned_to:
        # Busca atendentes (role='user')
        query = User.query.filter(User.role == 'user')
        
        # Filtra por filial e setor apenas se foram informados
        if filial:
            query = query.filter(db_sql.func.lower(User.filial) == filial.lower())
        if setor:
            query = query.filter(db_sql.func.lower(User.setor) == setor.lower())
            
        users_query = query.all()
        
        if users_query:
            selected_user = None
            min_chats = float('inf')
            
            # Descobre qual atendente tem o menor número de chats no momento
            for u in users_query:
                chat_count = Contact.query.filter_by(assigned_to=u.id).count()
                if chat_count < min_chats:
                    min_chats = chat_count
                    selected_user = u
                    
            if selected_user:
                contact.assigned_to = selected_user.id
                contact.assigned_name = selected_user.name
                
                # Remove a tag BOT caso exista, pois o chat já foi atribuído
                current_tags = [t for t in current_tags if isinstance(t, str) and t.upper() != 'BOT']
                
                # Adiciona a tag de identificação do Atendente (sempre em minúsculo para consistência)
                at_tag = (selected_user.email or '').lower()
                if at_tag not in [t.lower() if isinstance(t, str) else t for t in current_tags]:
                    current_tags.append(at_tag)
                
                added = True
                print(f"[BOT/TAGS] Auto-atribuído {contact.id} para {selected_user.name} ({min_chats} chats ativos)")
                track_sla_event(phone, atendente=selected_user.name, event_type='ASSIGNED')
    # --- Fim da Lógica ---
        
    if added:
        contact.tags = current_tags
        flag_modified(contact, 'tags')
        db_sql.session.commit()
        print(f"[BOT/TAGS] Tags atualizadas para '{contact.id}': {contact.tags}")
        _inst_room = contact.instance or 'unknown'
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags)
        }, room=f'instance_{_inst_room}')
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags)
        }, room='admin')
        
        if contact.assigned_to:
            assign_data = {
                'contact_id': contact.id,
                'assigned_to': contact.assigned_to,
                'assigned_name': contact.assigned_name,
                'tags': list(contact.tags)
            }
            socketio.emit('chat_assignment', assign_data, room=f'instance_{_inst_room}')
            socketio.emit('chat_assignment', assign_data, room='admin')
    else:
        print(f"[BOT/TAGS] Nenhuma tag alterada (filial={filial}, setor={setor}, tag={custom_tag})")
        
    return jsonify({'success': True, 'contact_id': contact.id, 'tags': contact.tags}), 200

# ─── Webhooks WAHA API ────────────────────────────────────────────────────────────

# ─── Auth Routes ────────────────────────────────────────────────────────────

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')
    
    user = User.query.filter_by(email=email, password=password).first()
    if user:
        token = jwt.encode({
            'id': user.id,
            'email': user.email,
            'role': user.role,
            'filial_id': user.filial_id,
            'setor_id': user.setor_id,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(days=1)
        }, JWT_SECRET, algorithm="HS256")
        
        # Resolve filial/setor names for the user response
        filial_name = user.filial
        setor_name = user.setor
        if not filial_name and user.filial_id:
            f_obj = Filial.query.get(user.filial_id)
            if f_obj: filial_name = f_obj.name
        if not setor_name and user.setor_id:
            s_obj = Setor.query.get(user.setor_id)
            if s_obj: setor_name = s_obj.name
        
        return jsonify({
            'token': token if isinstance(token, str) else token.decode('utf-8'),
            'user': {
                'id': user.id,
                'name': user.name,
                'email': user.email,
                'role': user.role,
                'filial_id': user.filial_id,
                'setor_id': user.setor_id,
                'filial': filial_name,
                'setor': setor_name,
                'instances': user.instances or []
            }
        })
    return jsonify({'error': 'Credenciais inválidas'}), 401

@app.route('/api/admin/users', methods=['GET'])
@auth_required
@admin_required
def list_users():
    users = User.query.filter(User.role != 'admin').all()
    users_list = []
    for u in users:
        users_list.append({
            'id': u.id,
            'name': u.name,
            'email': u.email,
            'phone': u.phone,
            'role': u.role,
            'instances': u.instances or [],
            'filial_id': u.filial_id,
            'setor_id': u.setor_id,
            'filial': u.filial,
            'setor': u.setor
        })
    return jsonify(users_list)

@app.route('/api/admin/users', methods=['POST'])
@auth_required
@admin_required
def create_user():
    data = request.json
    f_id = data.get('filial_id')
    s_id = data.get('setor_id')
    
    # Normaliza string vazia para None para não quebrar a consulta no banco
    if not f_id: f_id = None
    if not s_id: s_id = None

    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'E-mail já cadastrado'}), 400
        
    filial_name = None
    setor_name = None
    
    f_obj = Filial.query.get(f_id) if f_id else None
    if f_obj: filial_name = f_obj.name
    
    s_obj = Setor.query.get(s_id) if s_id else None
    if s_obj: setor_name = s_obj.name
    # Auto-preencher instances quando for gestor, baseado na filial
    auto_instances = []
    role = data.get('role', 'user') if data.get('role') in ('user', 'gestor', 'entregador') else 'user'
    if role == 'gestor' and f_obj and f_obj.instance:
        auto_instances = [f_obj.instance]
    
    new_user = User(
        name=data.get('name'),
        email=data.get('email'),
        phone=data.get('phone'),
        password=data.get('password'),
        role=role,
        instances=auto_instances,
        filial_id=f_id,
        setor_id=s_id,
        filial=data.get('filial') or filial_name,
        setor=data.get('setor') or setor_name
    )
    db_sql.session.add(new_user)
    db_sql.session.commit()
    
    return jsonify({
        'id': new_user.id,
        'name': new_user.name,
        'email': new_user.email,
        'role': new_user.role
    }), 201

@app.route('/api/admin/users/<int:user_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_required
def manage_user(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404
    
    if request.method == 'PUT':
        data = request.json
        if not data.get('filial_id') or not data.get('setor_id'):
            pass # Filial e Setor agora são opcionais

        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        if 'phone' in data:
            user.phone = data.get('phone')
        if data.get('password'):
            user.password = data['password']
            
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        
        if not f_id: f_id = None
        if not s_id: s_id = None
        
        user.filial_id = f_id
        if f_id:
            f_obj = Filial.query.get(f_id)
            if f_obj: user.filial = f_obj.name
        else:
            user.filial = None
            
        user.setor_id = s_id
        if s_id:
            s_obj = Setor.query.get(s_id)
            if s_obj: user.setor = s_obj.name
        else:
            user.setor = None
            
        if data.get('filial'):
            user.filial = data.get('filial')
        if data.get('setor'):
            user.setor = data.get('setor')
        
        # Permite atualizar role (apenas user, gestor, entregador, nunca admin)
        new_role = data.get('role')
        if new_role in ('user', 'gestor', 'entregador'):
            user.role = new_role
            
        db_sql.session.commit()
        return jsonify({
            'id': user.id,
            'name': user.name,
            'email': user.email,
            'phone': user.phone,
            'role': user.role
        })
    
    if request.method == 'DELETE':
        if user.role == 'admin':
            return jsonify({'error': 'Não permitido excluir admin'}), 403
        db_sql.session.delete(user)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/admin/link-user-instance', methods=['POST'])
@auth_required
@admin_required
def link_instance():
    data = request.json
    user_id = data['userId']
    inst_name = data['instanceName']
    action = data['action']
    
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    instances = list(user.instances or [])
    if action == 'add':
        if inst_name not in instances:
            instances.append(inst_name)
    else:
        if inst_name in instances:
            instances.remove(inst_name)
            
    user.instances = instances
    db_sql.session.commit()
    return jsonify({'success': True, 'instances': user.instances})

# ─── Gestor / Filial / Setor Routes ─────────────────────────────────────────

def get_gestor_allowed_instances(user):
    """Resolve as instâncias permitidas de um gestor.
    Prioridade: user.instances > derivado do filial_id.
    Retorna um set de nomes de instância."""
    # Se já tem instâncias explicitamente atribuídas, usar elas
    if user.instances and len(user.instances) > 0:
        return set(user.instances)
    # Senão, derivar da filial vinculada ao gestor
    if user.filial_id:
        filial = Filial.query.get(user.filial_id)
        if filial and filial.instance:
            return {filial.instance}
    return set()

@app.route('/api/admin/filiais', methods=['GET', 'POST'])
@auth_required
def manage_filiais():
    user = User.query.get(request.user['id'])
    if request.method == 'POST':
        if user.role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
            
        data = request.json
        name = data.get('name')
        instance = data.get('instance')
        
        if not name or not instance:
            return jsonify({'error': 'Nome e Instância são obrigatórios'}), 400
        if user.role == 'gestor' and instance not in get_gestor_allowed_instances(user):
            return jsonify({'error': 'Você não tem permissão para gerenciar esta instância.'}), 403

        nova_filial = Filial(name=name, instance=instance)
        db_sql.session.add(nova_filial)
        db_sql.session.commit()
        return jsonify({'id': nova_filial.id, 'name': nova_filial.name, 'instance': nova_filial.instance}), 201

    if user.role == 'user':
        # Se for para transferência, permite ver todas as filiais
        if request.args.get('action') == 'transfer':
            filiais = Filial.query.all()
            return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])
            
        if user.filial_id:
            filial = Filial.query.get(user.filial_id)
            if filial:
                return jsonify([{'id': filial.id, 'name': filial.name, 'instance': filial.instance}])
        return jsonify([])
        
    elif user.role == 'gestor':
        # Se for para transferência, permite ver todas as filiais
        if request.args.get('action') == 'transfer':
            filiais = Filial.query.all()
            return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])
            
        allowed_instances = get_gestor_allowed_instances(user)
        print(f"[GESTOR FILIAIS] user={user.id} allowed_instances={allowed_instances}")
        
        from sqlalchemy import or_
        filters = []
        if allowed_instances:
            filters.append(Filial.instance.in_(list(allowed_instances)))
        if user.filial_id:
            filters.append(Filial.id == user.filial_id)
            
        if filters:
            filiais = Filial.query.filter(or_(*filters)).all()
        else:
            filiais = []
    else:
        filiais = Filial.query.all()
        
    return jsonify([{'id': f.id, 'name': f.name, 'instance': f.instance} for f in filiais])

@app.route('/api/admin/filiais/<int:filial_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def manage_filial_single(filial_id):
    filial = Filial.query.get(filial_id)
    if not filial:
        return jsonify({'error': 'Filial não encontrada'}), 404

    user = User.query.get(request.user['id'])
    # Gestor só pode gerenciar filiais das suas instâncias
    if user.role == 'gestor' and filial.instance not in get_gestor_allowed_instances(user):
        return jsonify({'error': 'Sem permissão para esta filial.'}), 403

    if request.method == 'PUT':
        data = request.json
        name = data.get('name', filial.name)
        instance = data.get('instance', filial.instance)
        if user.role == 'gestor' and instance not in get_gestor_allowed_instances(user):
            return jsonify({'error': 'Sem permissão para esta instância.'}), 403
            
        if name != filial.name:
            User.query.filter_by(filial_id=filial_id).update({'filial': name})
            
        filial.name = name
        filial.instance = instance
        db_sql.session.commit()
        return jsonify({'id': filial.id, 'name': filial.name, 'instance': filial.instance})

    if request.method == 'DELETE':
        # Remover referências nos usuários
        User.query.filter_by(filial_id=filial_id).update({'filial_id': None, 'filial': None})
        # Remover setores vinculados e suas referências nos usuários
        setores = Setor.query.filter_by(filial_id=filial_id).all()
        for s in setores:
            User.query.filter_by(setor_id=s.id).update({'setor_id': None, 'setor': None})
            db_sql.session.delete(s)
            
        db_sql.session.delete(filial)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/admin/setores', methods=['GET', 'POST'])
@auth_required
def manage_setores():
    user = User.query.get(request.user['id'])
    if request.method == 'POST':
        if user.role not in ('admin', 'gestor'):
            return jsonify({'error': 'Acesso negado. Apenas administradores ou gestores.'}), 403
            
        data = request.json
        name = data.get('name')
        filial_id = data.get('filial_id')
        
        if not name or not filial_id:
            return jsonify({'error': 'Nome e Filial são obrigatórios'}), 400

        filial = Filial.query.get(filial_id)
        if not filial:
            return jsonify({'error': 'Filial não encontrada'}), 400
        if user.role == 'gestor' and filial.instance not in (user.instances or []):
            return jsonify({'error': 'Sem permissão para esta filial.'}), 403

        novo_setor = Setor(name=name, filial_id=filial_id, filial_name=filial.name)
        db_sql.session.add(novo_setor)
        db_sql.session.commit()
        return jsonify({'id': novo_setor.id, 'name': novo_setor.name, 'filial_id': novo_setor.filial_id, 'filial_name': novo_setor.filial_name}), 201

    if user.role == 'user':
        # Se for para transferência, permite ver todos os setores
        if request.args.get('action') == 'transfer':
            setores = Setor.query.all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
            
        if user.filial_id:
            setores = Setor.query.filter_by(filial_id=user.filial_id).all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
        return jsonify([])
        
    elif user.role == 'gestor':
        # Se for para transferência, permite ver todos os setores
        if request.args.get('action') == 'transfer':
            setores = Setor.query.all()
            return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])
            
        allowed_instances = get_gestor_allowed_instances(user)
        print(f"[GESTOR SETORES] user={user.id} allowed_instances={allowed_instances}")
        
        allowed_f_ids = set()
        if user.filial_id:
            allowed_f_ids.add(user.filial_id)
            
        if allowed_instances:
            allowed_filiais = Filial.query.filter(Filial.instance.in_(list(allowed_instances))).all()
            for f in allowed_filiais:
                allowed_f_ids.add(f.id)
                
        if not allowed_f_ids:
            setores = []
        else:
            setores = Setor.query.filter(Setor.filial_id.in_(list(allowed_f_ids))).all()
    else:
        setores = Setor.query.all()

    return jsonify([{'id': s.id, 'name': s.name, 'filial_id': s.filial_id, 'filial_name': s.filial_name} for s in setores])

@app.route('/api/admin/setores/<int:setor_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def manage_setor_single(setor_id):
    setor = Setor.query.get(setor_id)
    if not setor:
        return jsonify({'error': 'Setor não encontrado'}), 404

    user = User.query.get(request.user['id'])
    filial = Filial.query.get(setor.filial_id)
    # Gestor só pode gerenciar setores das suas instâncias
    if user.role == 'gestor' and filial and filial.instance not in get_gestor_allowed_instances(user):
        return jsonify({'error': 'Sem permissão para este setor.'}), 403

    if request.method == 'PUT':
        data = request.json
        new_name = data.get('name', setor.name)
        new_filial_id = data.get('filial_id', setor.filial_id)
        
        if new_filial_id:
            new_filial = Filial.query.get(new_filial_id)
            if user.role == 'gestor' and new_filial and new_filial.instance not in get_gestor_allowed_instances(user):
                return jsonify({'error': 'Sem permissão para esta filial.'}), 403
            setor.filial_id = new_filial_id
            setor.filial_name = new_filial.name if new_filial else setor.filial_name
            
        if new_name != setor.name:
            User.query.filter_by(setor_id=setor_id).update({'setor': new_name})
            setor.name = new_name
            
        db_sql.session.commit()
        return jsonify({'id': setor.id, 'name': setor.name, 'filial_id': setor.filial_id, 'filial_name': setor.filial_name})

    if request.method == 'DELETE':
        # Remover referências nos usuários
        User.query.filter_by(setor_id=setor_id).update({'setor_id': None, 'setor': None})
        db_sql.session.delete(setor)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/gestor/users', methods=['GET', 'POST'])
@auth_required
@admin_or_gestor_required
def gestor_manage_users():
    user_req = User.query.get(request.user['id'])
    allowed_instances = get_gestor_allowed_instances(user_req) if user_req.role == 'gestor' else None

    if request.method == 'POST':
        data = request.json
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if not f_id or not s_id:
            pass # Filial e Setor agora são opcionais

        email = data.get('email')
        instances_to_assign = set(data.get('instances', []))

        if allowed_instances is not None:
            if not instances_to_assign:
                instances_to_assign = allowed_instances
            elif not instances_to_assign.issubset(allowed_instances):
                return jsonify({'error': 'Você só pode criar usuários para suas próprias instâncias. Deve selecionar pelo menos uma.'}), 403
        
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'E-mail já cadastrado'}), 400

        filial_name = None
        setor_name = None
        f_obj = Filial.query.get(f_id)
        if f_obj: filial_name = f_obj.name
        s_obj = Setor.query.get(s_id)
        if s_obj: setor_name = s_obj.name

        novo_usr = User(
            name=data.get('name'),
            email=email,
            phone=data.get('phone'),
            password=data.get('password', '123456'),
            role='user',
            instances=list(instances_to_assign),
            filial_id=f_id,
            setor_id=s_id,
            filial=data.get('filial') or filial_name,
            setor=data.get('setor') or setor_name
        )
        db_sql.session.add(novo_usr)
        db_sql.session.commit()

        return jsonify({'id': novo_usr.id, 'name': novo_usr.name, 'email': novo_usr.email}), 201

    # GET
    all_users = User.query.filter(User.role == 'user').all()
    if allowed_instances is not None:
        visible_users = []
        for u in all_users:
            has_instance = set(u.instances or []).intersection(allowed_instances)
            is_same_filial = (u.filial_id == user_req.filial_id) if user_req.filial_id else False
            if has_instance or is_same_filial:
                visible_users.append(u)
    else:
        visible_users = all_users

    users_list = []
    for u in visible_users:
        users_list.append({
            'id': u.id,
            'name': u.name,
            'email': u.email,
            'phone': u.phone,
            'instances': u.instances or [],
            'filial_id': u.filial_id,
            'setor_id': u.setor_id,
            'filial': u.filial,
            'setor': u.setor
        })
    return jsonify(users_list)

@app.route('/api/gestor/users/<int:user_id>', methods=['PUT', 'DELETE'])
@auth_required
@admin_or_gestor_required
def gestor_update_user(user_id):
    target_user = User.query.get(user_id)
    if not target_user or target_user.role != 'user':
        return jsonify({'error': 'Usuário não encontrado ou não permitido'}), 404

    user_req = User.query.get(request.user['id'])
    allowed_instances = get_gestor_allowed_instances(user_req) if user_req.role == 'gestor' else None
    
    if allowed_instances is not None:
        has_instance = bool(set(target_user.instances or []).intersection(allowed_instances))
        is_same_filial = (target_user.filial_id == user_req.filial_id) if user_req.filial_id else False
        if not (has_instance or is_same_filial):
             return jsonify({'error': 'Você não tem permissão sobre este usuário'}), 403

    if request.method == 'PUT':
        data = request.json
        if not data.get('filial_id') or not data.get('setor_id'):
            pass # Filial e Setor agora são opcionais

        target_user.name = data.get('name', target_user.name)
        if 'phone' in data:
            target_user.phone = data.get('phone')
        if data.get('password'):
            target_user.password = data['password']
        if 'instances' in data and (allowed_instances is None or set(data['instances']).issubset(allowed_instances)):
            target_user.instances = list(data['instances'])
            
        f_id = data.get('filial_id')
        s_id = data.get('setor_id')
        if f_id:
            target_user.filial_id = f_id
            f_obj = Filial.query.get(f_id)
            if f_obj: target_user.filial = f_obj.name
        if s_id:
            target_user.setor_id = s_id
            s_obj = Setor.query.get(s_id)
            if s_obj: target_user.setor = s_obj.name
            
        if data.get('filial'):
            target_user.filial = data.get('filial')
        if data.get('setor'):
            target_user.setor = data.get('setor')
            
        db_sql.session.commit()
        return jsonify({'success': True})

    if request.method == 'DELETE':
        db_sql.session.delete(target_user)
        db_sql.session.commit()
        return jsonify({'success': True})

@app.route('/api/whatsapp/instances', methods=['GET'])
@auth_required
def get_instances():
    try:
        url = f"{WAHA_API_URL}/api/sessions/?all=true"
        response = requests.get(url, headers=get_waha_headers())
        all_inst = response.json()
        
        if request.user.get('role') != 'admin':
            user = User.query.get(request.user['id'])
            allowed = get_gestor_allowed_instances(user)
            all_inst = [i for i in all_inst if (i.get('name') or i.get('instanceName')) in allowed]
            
        return jsonify(all_inst)
    except Exception as e:
        print(f"Erro ao buscar instâncias: {str(e)}")
        return jsonify({'error': f"Erro na WAHA API: {str(e)}"}), 500

def auto_assign_chat_to_sender(contact, user_data):
    if not user_data: return
    user_email = user_data.get('email', '')
    contact.assigned_to = user_data.get('id')
    contact.assigned_name = user_email
    
    tags = list(contact.tags or [])
    tags = [t for t in tags if t != 'BOT']
    at_tag = f"Atendente: {user_email}"
    if at_tag not in tags:
        tags.append(at_tag)
        
    f_id, s_id = user_data.get('filial_id'), user_data.get('setor_id')
    if f_id and s_id:
        _f = Filial.query.get(f_id)
        _s = Setor.query.get(s_id)
        if _f and _s:
            fs_tag = f"{_f.name}:{_s.name}"
            if fs_tag not in tags:
                tags.append(fs_tag)
                
    contact.tags = tags
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(contact, 'tags')
    
    # Atualiza tabela atendimentos_chat
    agora_iso = get_now().isoformat()
    atend = AtendimentoChat.query.filter_by(numero=contact.phone).first()
    if atend:
        atend.atendente = user_email
        atend.status = 'atendente'
        atend.ultimo_atendente = user_email
        atend.registro_time_chat = agora_iso
        atend.atendente_desde = agora_iso
    else:
        atend = AtendimentoChat(numero=contact.phone, status='atendente', atendente=user_email, ultimo_atendente=user_email, registro_time_chat=agora_iso, atendente_desde=agora_iso)
        db_sql.session.add(atend)

    # Dispara evento socket para atualizar interface
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': user_data.get('id'),
        'assigned_name': user_email,
        'tags': tags
    })

@app.route('/api/whatsapp/send', methods=['POST'])
@auth_required
def send_message():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    text = data.get('text', '')
    
    if not inst or not number:
        return jsonify({'error': 'Instância e número são obrigatórios'}), 400

    # --- Chat locking check ---
    contact_id_check = f"c_{number}_{inst}"
    locked_contact = Contact.query.filter_by(id=contact_id_check).first()
    if locked_contact and locked_contact.assigned_to and locked_contact.assigned_to != request.user['id']:
        return jsonify({'error': f'Chat sendo atendido por {locked_contact.assigned_name or "outro atendente"}. Não é possível enviar mensagens.'}), 403

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")
        
        # Chamada para a API externa (WAHA)
        url = f"{WAHA_API_URL}/api/sendText"
        payload = {
            "chatId": f"{number}@c.us",
            "id": None,
            "reply_to": None,
            "text": text,
            "linkPreview": True,
            "linkPreviewHighQuality": False,
            "session": inst
        }
        print(f"[SEND] URL: {url}")
        print(f"[SEND] Payload: {json.dumps(payload)}")
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=30)
        print(f"[SEND] Response status: {res.status_code}")
        print(f"[SEND] Response body: {res.text[:300]}")
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        
        # Verificar se a API retornou erro
        if res.status_code != 200 and res.status_code != 201:
            error_msg = res_data.get('response', {}).get('message', res_data.get('message', str(res_data)))
            print(f"[SEND] ERRO WAHA API: {error_msg}")
            return jsonify({'error': f'WAHA API erro: {error_msg}'}), res.status_code
        
        msg_id = extract_waha_msg_id(res_data, f"out_{int(now.timestamp())}")
        contact_id = f"c_{number}_{inst}"
        
        # Atualizar ou Criar Contato
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
            auto_assign_chat_to_sender(contact, request.user)
        else:
            contact = Contact(
                id=contact_id,
                name=number,
                phone=number,
                avatar=number[0] if number else "?",
                instance=inst,
                tags=['Novo Lead'],
                last_msg=text,
                last_msg_time=time_str,
                unread=0
            )
            auto_assign_chat_to_sender(contact, request.user)
            db_sql.session.add(contact)
        
        db_sql.session.flush()

        # Salvar mensagem no Banco SE NÃO EXISTIR
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id,
                contact_id=contact_id,
                text=text,
                type='out',
                time=time_str,
                timestamp=int(now.timestamp()),
                instance=inst
            )
            db_sql.session.add(new_msg)
        
        # --- Forward to N8N (Attendant Message) ---
        webhook_key = f"n8n_webhook_{inst}"
        n8n_set = Setting.query.get(webhook_key)
        if n8n_set and n8n_set.value:
            try:
                n8n_payload = {
                    "event": "send.message",
                    "instance": inst,
                    "attendant": True,
                    "data": {
                        "key": {"remoteJid": f"{number}@s.whatsapp.net", "fromMe": True, "id": msg_id},
                        "message": {"conversation": text}
                    }
                }
                requests.post(n8n_set.value, json=n8n_payload, timeout=5)
            except Exception as w_e:
                print(f"Erro ao disparar webhook N8N para atendente: {w_e}")
                
        db_sql.session.commit()

        # --- Corpal Webhook (Attendant sent message) ---
        try:
            user_obj = User.query.get(request.user['id'])
            _contact_send = Contact.query.filter_by(id=contact_id).first()
            _filial = None
            _setor = None
            if user_obj:
                if user_obj.filial_id:
                    _f = Filial.query.get(user_obj.filial_id)
                    _filial = _f.name if _f else None
                if user_obj.setor_id:
                    _s = Setor.query.get(user_obj.setor_id)
                    _setor = _s.name if _s else None
            corpal_payload = {
                "evento": "mensagem",
                "atendimento_id": str(uuid.uuid4()),
                "numero_lead": number,
                "instancia": inst,
                "filial": _filial,
                "setor": _setor,
                "nome_atendente": user_obj.name if user_obj else "Desconhecido",
                "atendente_id": str(user_obj.id) if user_obj else None,
                "direcao": "atendente",
                "mensagem": text,
                "timestamp": now.isoformat()
            }
            requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
        except Exception as corpal_e:
            print(f"Erro webhook corpal (send): {corpal_e}")
        return jsonify(res_data)
    except Exception as e:
        print(f"Erro ao enviar: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-audio', methods=['POST'])
@auth_required
def send_audio():
    """Envia audio gravado pelo atendente ao cliente via WAHA API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    audio_b64 = data.get('audio', '')

    if not inst or not number or not audio_b64:
        return jsonify({'error': 'instance, number e audio são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        # Enviar via WAHA API
        audio_raw = audio_b64
        mimetype = "audio/ogg; codecs=opus"
        if ';base64,' in audio_raw:
            mime_part, audio_raw = audio_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendVoice"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "file": {
                "mimetype": mimetype,
                "data": audio_raw
            },
            "convert": True
        }
        print(f"[Send Audio] Enviando audio para {number} via {inst}")
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        print(f"[Send Audio] Resposta: status={res.status_code} body={json.dumps(res_data)[:300]}")

        msg_id = extract_waha_msg_id(res_data, f"audio_out_{int(now.timestamp())}")
        
        # Salvar o arquivo no MinIO
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', audio_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            short_id = msg_id.split('_')[-1]
            
            # Forçar extensão
            file_ext = '.webm'
            if 'audio/ogg' in mimetype: file_ext = '.ogg'
            
            file_bytes = base64.b64decode(pad_raw)
            
            minio_upload(file_bytes, msg_id + file_ext, mimetype)
            if msg_id != short_id:
                minio_upload(file_bytes, short_id + file_ext, mimetype)
        except Exception as e:
            print(f"[Send Audio] Erro ao salvar arquivo no MinIO: {e}")

        text = f"[AUDIO_REF] {inst}|{msg_id}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        # Salvar mensagem
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🎤 Áudio'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        # NÃO emitir socket — o frontend já renderiza via optimistic update
        # Isso evita a duplicação de mensagem

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_audio: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-image', methods=['POST'])
@auth_required
def send_image():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    image_b64 = data.get('image', '')
    caption = data.get('caption', '')

    if not inst or not number or not image_b64:
        return jsonify({'error': 'instance, number e image são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        image_raw = image_b64
        mimetype = "image/jpeg"
        if ';base64,' in image_raw:
            mime_part, image_raw = image_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendImage"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "data": image_raw
            }
        }
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"img_out_{int(now.timestamp())}")
        
        # --- UPLOAD TO MINIO ---
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', image_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            short_id = msg_id.split('_')[-1]
            
            file_ext = '.jpeg'
            if 'image/png' in mimetype: file_ext = '.png'
            elif 'image/webp' in mimetype: file_ext = '.webp'
            
            file_bytes = base64.b64decode(pad_raw)
            minio_upload(file_bytes, msg_id + file_ext, mimetype)
            if msg_id != short_id:
                minio_upload(file_bytes, short_id + file_ext, mimetype)
        except Exception as e:
            print(f"[Send Image] Erro ao salvar arquivo no MinIO: {e}")
        # -------------------------

        text = f"[IMAGE_REF] {inst}|{msg_id}"
        if caption:
            text += f"\n{caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🖼️ Imagem'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_image: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-video', methods=['POST'])
@auth_required
def send_video():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    video_b64 = data.get('video', '')
    caption = data.get('caption', '')

    if not inst or not number or not video_b64:
        return jsonify({'error': 'instance, number e video são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        video_raw = video_b64
        mimetype = "video/mp4"
        if ';base64,' in video_raw:
            mime_part, video_raw = video_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendVideo"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "data": video_raw
            }
        }
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=60)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"vid_out_{int(now.timestamp())}")
        
        # --- UPLOAD TO MINIO ---
        try:
            import base64, re
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', video_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            short_id = msg_id.split('_')[-1]
            
            file_ext = '.mp4'
            file_bytes = base64.b64decode(pad_raw)
            minio_upload(file_bytes, msg_id + file_ext, mimetype)
            if msg_id != short_id:
                minio_upload(file_bytes, short_id + file_ext, mimetype)
        except Exception as e:
            print(f"[Send Video] Erro ao salvar arquivo no MinIO: {e}")
        # -------------------------

        text = f"[VIDEO_REF] {inst}|{msg_id}"
        if caption:
            text += f"\n{caption}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '🎥 Vídeo'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_video: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-document', methods=['POST'])
@auth_required
def send_document():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    doc_b64 = data.get('document', '')
    doc_name = data.get('fileName', 'documento.pdf')
    caption = data.get('caption', '')

    if not inst or not number or not doc_b64:
        return jsonify({'error': 'instance, number e document são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        doc_raw = doc_b64
        mimetype = "application/pdf"
        if ';base64,' in doc_raw:
            mime_part, doc_raw = doc_raw.split(';base64,', 1)
            mimetype = mime_part.replace('data:', '')

        url = f"{WAHA_API_URL}/api/sendFile"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "caption": caption,
            "file": {
                "mimetype": mimetype,
                "filename": doc_name,
                "data": doc_raw
            }
        }
        print(f"[Send Document] Enviando para WAHA via data (base64)")
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=60)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"doc_out_{int(now.timestamp())}")
        
        # --- UPLOAD TO MINIO ---
        try:
            import base64, re, os
            clean_b64 = re.sub(r'[^A-Za-z0-9+/]', '', doc_raw)
            pad_raw = clean_b64 + "=" * ((4 - len(clean_b64) % 4) % 4)
            short_id = msg_id.split('_')[-1]
            
            _, file_ext = os.path.splitext(doc_name)
            if not file_ext:
                if 'application/pdf' in mimetype: file_ext = '.pdf'
                elif 'application/zip' in mimetype: file_ext = '.zip'
                elif 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' in mimetype: file_ext = '.docx'
                elif 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in mimetype: file_ext = '.xlsx'
                elif 'text/plain' in mimetype: file_ext = '.txt'
                else: file_ext = '.bin'
                
            file_bytes = base64.b64decode(pad_raw)
            minio_upload(file_bytes, msg_id + file_ext, mimetype)
            if msg_id != short_id:
                minio_upload(file_bytes, short_id + file_ext, mimetype)
        except Exception as e:
            print(f"[Send Document] Erro ao fazer upload no MinIO: {e}")
        # -------------------------
        
        # --- NEW CACHING LOGIC ---


        text = f"[DOC_REF] {inst}|{msg_id}|{doc_name}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '📎 Arquivo'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_document: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-location', methods=['POST'])
@auth_required
def send_location():
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    name = data.get('name', 'Localização')
    address = data.get('address', '')
    latitude = data.get('latitude')
    longitude = data.get('longitude')

    if not inst or not number or latitude is None or longitude is None:
        return jsonify({'error': 'instance, number, latitude e longitude são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        url = f"{WAHA_API_URL}/api/sendLocation"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "latitude": float(latitude),
            "longitude": float(longitude),
            "title": name,
            "description": address
        }
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=60)
        res.raise_for_status()
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}

        msg_id = extract_waha_msg_id(res_data, f"loc_out_{int(now.timestamp())}")
        
        text = f"[LOCATION_REF] {latitude}|{longitude}|{name}|{address}"

        contact_id = f"c_{number}_{inst}"

        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = '📍 Localização'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        fake_event = {
            'event': 'send.message',
            'instance': inst,
            'data': {
                'key': {'remoteJid': f"{number}@s.whatsapp.net", 'fromMe': True, 'id': msg_id},
                'message': {'locationMessage': {}}
            },
            '_processed_text': text
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_location: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/whatsapp/send-contact', methods=['POST'])
@auth_required
def send_contact():
    """Envia um cartão de contato (vCard) para o cliente via WAHA API."""
    data = request.json
    inst = data.get('instance')
    number = "".join(filter(str.isdigit, str(data.get('number', ''))))
    number = normalize_br_phone(number)
    contact_name = data.get('contact_name', 'Contato').strip()
    contact_phone = "".join(filter(str.isdigit, str(data.get('contact_phone', ''))))
    contact_phone = normalize_br_phone(contact_phone)

    if not inst or not number or not contact_phone:
        return jsonify({'error': 'instance, number e contact_phone são obrigatórios'}), 400

    try:
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")

        # Montar vCard padrão
        vcard = (
            "BEGIN:VCARD\r\n"
            "VERSION:3.0\r\n"
            f"FN:{contact_name}\r\n"
            f"TEL;type=CELL;type=VOICE;waid={contact_phone}:+{contact_phone}\r\n"
            "END:VCARD"
        )

        url = f"{WAHA_API_URL}/api/sendContactVcard"
        payload = {
            "session": inst,
            "chatId": f"{number}@c.us",
            "contacts": [
                {
                    "fullName": contact_name,
                    "phoneNumber": contact_phone
                }
            ]
        }
        print(f"[Send Contact] Enviando contato '{contact_name}' ({contact_phone}) para {number} via {inst}")
        res = waha_post_with_fallback(url, payload, headers=get_waha_headers(), timeout=30)
        try:
            res_data = res.json()
        except Exception:
            res_data = {'message': res.text}
        print(f"[Send Contact] Resposta: status={res.status_code} body={json.dumps(res_data)[:300]}")

        msg_id = extract_waha_msg_id(res_data, f"contact_out_{int(now.timestamp())}")
        text = f"[CONTACT_REF] {contact_name}|+{contact_phone}|{vcard}"

        contact_id = f"c_{number}_{inst}"
        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            contact = Contact(id=contact_id, phone=number, name=f"Novo {number}", instance=inst)
            db_sql.session.add(contact)
        auto_assign_chat_to_sender(contact, request.user)
        db_sql.session.flush()

        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id, contact_id=contact_id, text=text,
                type='out', time=time_str, timestamp=int(now.timestamp()), instance=inst
            )
            db_sql.session.add(new_msg)

        contact.last_msg = f'👤 {contact_name}'
        contact.last_msg_time = time_str
        db_sql.session.commit()

        # Emitir socket para o frontend renderizar imediatamente
        fake_event = {
            'event': 'send.message',
            'instance': inst,
            'data': {
                'key': {'remoteJid': f"{number}@s.whatsapp.net", 'fromMe': True, 'id': msg_id},
                'message': {'contactMessage': {'displayName': contact_name, 'vcard': vcard}}
            },
            '_processed_text': text
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')

        return jsonify({'ok': True, 'msg_id': msg_id, 'key': res_data.get('key', {})})
    except Exception as e:
        print(f"Erro send_contact: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bot-message', methods=['POST'])
def bot_message_webhook():
    try:
        data = request.json
        if not data: return jsonify({'error': 'Body vazio'}), 400
        
        # Suporte para o array bruto do N8N/WAHA
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        if 'data' in data and 'key' in data.get('data', {}):
            # Formato do Sistema Interno (mantido para retrocompatibilidade com N8N)
            d = data['data']
            inst = d.get('instanceId') or d.get('instance')
            phone = str(d.get('key', {}).get('remoteJid', '')).split('@')[0].split(':')[0]
            phone = normalize_br_phone(phone)
            
            raw_id = d.get('key', {}).get('id', '')
            msg_id = f"bot_{raw_id}" if not raw_id.startswith('bot_') else raw_id

            # Parse message content including audio
            m_data = d.get('message', {})
            if 'audioMessage' in m_data:
                audio_base64 = data.get('base64') or d.get('base64') or m_data.get('base64') or m_data.get('audioMessage', {}).get('url')
                
                if not audio_base64 or str(audio_base64).startswith('http'):
                    fetched_b64 = get_media_base64(inst, d)
                    if fetched_b64:
                        audio_base64 = fetched_b64

                if audio_base64:
                    if str(audio_base64).startswith('data:') or str(audio_base64).startswith('http'):
                        text = f"[AUDIO] {audio_base64}"
                    else:
                        text = f"[AUDIO] data:audio/ogg;base64,{audio_base64}"
                else:
                    text = "[Áudio do Bot]"
            else:
                text = m_data.get('conversation') or m_data.get('extendedTextMessage', {}).get('text') or "[Mensagem do Bot]"
        else:
            # Formato Customizado Opcional
            inst = data.get('instanceId') or data.get('instance')
            phone = str(data.get('phone'))
            phone = normalize_br_phone(phone)
            text = data.get('text')
            msg_id = f"bot_{int(get_now().timestamp())}_{str(phone)[-4:]}"
        
        if not inst or not phone or not text:
            return jsonify({'error': 'Faltam campos obrigatorios: instance/instanceId, phone (ou remoteJid), e text'}), 400
            
        now = get_now()
        time_str = now.strftime("%d/%m %H:%M")
        contact_id = f"c_{phone}_{inst}"
        
        # Update Contact
        # Update Contact
        contact = Contact.query.filter_by(id=contact_id).first()
        if contact:
            contact.last_msg = text
            contact.last_msg_time = time_str
            tags = list(contact.tags or [])
            if 'BOT' not in tags:
                tags.append('BOT')
                contact.tags = tags
                flag_modified(contact, 'tags')
        else:
            new_contact = Contact(
                id=contact_id, name=phone, phone=phone,
                avatar=phone[0] if phone else "?", instance=inst,
                tags=['Novo Lead', 'BOT'], last_msg=text, last_msg_time=time_str, unread=0
            )
            db_sql.session.add(new_contact)
            
        db_sql.session.flush()

        # Save Message (com verificação de duplicata)
        if not Message.query.get(msg_id):
            new_msg = Message(
                id=msg_id,
                contact_id=contact_id,
                text=text,
                type='out',
                time=time_str,
                timestamp=int(now.timestamp()),
                instance=inst
            )
            db_sql.session.add(new_msg)
        
        db_sql.session.commit()
        
        # Emit to frontend
        fake_event = {
            "event": "send.message",
            "instance": inst,
            "data": {
                "key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": True, "id": msg_id},
                "message": {"conversation": text}
            }
        }
        socketio.emit('whatsapp_event', fake_event, room=f'instance_{inst}')
        socketio.emit('whatsapp_event', fake_event, room='admin')
        
        return jsonify({"success": True, "message_id": msg_id}), 200
    except Exception as e:
        print(f"Erro bot-message: {e}")
        return jsonify({'error': str(e)}), 500

import threading
import time

# ─── Monitor de Tempo de Espera ─────────────────────────────────────────────

WEBHOOK_CHAMAR_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/alerta-tempo'
WEBHOOK_CHAMAR_GERENTE_URL = 'https://n8n-n8n.ioms5g.easypanel.host/webhook/alerta-tempo'

def wait_time_monitor_loop():
    """Thread em background que monitora clientes aguardando atendimento.
    Dispara alertas em 20 min (atendentes) e 40 min (gerentes)."""
    print("[MONITOR] Thread de monitoramento de tempo de espera iniciada.")
    while True:
        try:
            with app.app_context():
                agora = get_now()
                pendentes = AtendimentoChat.query.filter_by(status='atendente').all()
                for reg in pendentes:
                    if not reg.atendente_desde:
                        continue
                    try:
                        inicio = datetime.datetime.fromisoformat(reg.atendente_desde)
                        if inicio.tzinfo is None:
                            inicio = pytz.timezone('America/Sao_Paulo').localize(inicio)
                        decorrido_min = (agora - inicio).total_seconds() / 60

                        # Busca dados extras do contato para enriquecer o payload
                        contact_obj = Contact.query.filter_by(phone=reg.numero).order_by(Contact.id).first()
                        nome_cliente = contact_obj.name if contact_obj else reg.numero
                        instance_obj = contact_obj.instance if contact_obj else None

                        # Busca filial e setor: tenta pelo atendente primeiro, depois pelo ultimo_setor
                        filial_nome = None
                        setor_nome = reg.ultimo_setor  # fallback: setor de destino da transferência
                        if contact_obj and contact_obj.assigned_to:
                            atend_user = User.query.get(contact_obj.assigned_to)
                            if atend_user:
                                if atend_user.filial_id:
                                    _f = Filial.query.get(atend_user.filial_id)
                                    filial_nome = _f.name if _f else atend_user.filial
                                if atend_user.setor_id:
                                    _s = Setor.query.get(atend_user.setor_id)
                                    setor_nome = _s.name if _s else atend_user.setor
                                if not filial_nome:
                                    filial_nome = atend_user.filial
                                if not setor_nome:
                                    setor_nome = atend_user.setor
                        elif reg.atendente:
                            atend_user = User.query.filter_by(name=reg.atendente).first()
                            if atend_user:
                                if atend_user.filial_id:
                                    _f = Filial.query.get(atend_user.filial_id)
                                    filial_nome = _f.name if _f else atend_user.filial
                                if atend_user.setor_id:
                                    _s = Setor.query.get(atend_user.setor_id)
                                    setor_nome = _s.name if _s else atend_user.setor
                                if not filial_nome:
                                    filial_nome = atend_user.filial
                                if not setor_nome:
                                    setor_nome = atend_user.setor

                        payload_base = {
                            "numero": reg.numero,
                            "nome_cliente": nome_cliente,
                            "atendente": reg.atendente or "Aguardando atendente",
                            "filial": filial_nome,
                            "setor": setor_nome,
                            "instancia": instance_obj,
                            "minutos_esperando": round(decorrido_min, 1),
                            "atendente_desde": reg.atendente_desde,
                        }

                        # ── Alerta 20 minutos — notifica atendentes ──
                        if decorrido_min >= 20 and not reg.alerta_20min_enviado:
                            try:
                                payload_20 = dict(payload_base)
                                payload_20["nivel_alerta"] = "atendente"
                                payload_20["mensagem"] = f"Cliente {nome_cliente} aguardando há {round(decorrido_min, 1)} minutos sem atendimento."
                                requests.post(WEBHOOK_CHAMAR_URL, json=payload_20, timeout=10)
                                reg.alerta_20min_enviado = True
                                db_sql.session.commit()
                                print(f"[MONITOR] Alerta 20min enviado para {reg.numero} ({round(decorrido_min, 1)} min)")
                            except Exception as e20:
                                db_sql.session.rollback()
                                print(f"[MONITOR] Erro ao enviar alerta 20min para {reg.numero}: {e20}")

                        # ── Alerta 40 minutos — notifica gerentes ──
                        if decorrido_min >= 40 and not reg.alerta_40min_enviado:
                            try:
                                payload_40 = dict(payload_base)
                                payload_40["nivel_alerta"] = "gerente"
                                payload_40["mensagem"] = f"URGENTE: Cliente {nome_cliente} aguardando há {round(decorrido_min, 1)} minutos sem atendimento."
                                requests.post(WEBHOOK_CHAMAR_GERENTE_URL, json=payload_40, timeout=10)
                                reg.alerta_40min_enviado = True
                                db_sql.session.commit()
                                print(f"[MONITOR] Alerta 40min (gerente) enviado para {reg.numero} ({round(decorrido_min, 1)} min)")
                            except Exception as e40:
                                db_sql.session.rollback()
                                print(f"[MONITOR] Erro ao enviar alerta 40min para {reg.numero}: {e40}")

                    except Exception as e_reg:
                        print(f"[MONITOR] Erro ao processar registro {reg.numero}: {e_reg}")
        except Exception as e_loop:
            print(f"[MONITOR] Erro no loop de monitoramento: {e_loop}")
        time.sleep(60)  # Verifica a cada 60 segundos

def start_wait_time_monitor():
    """Inicia a thread de monitoramento de tempo de espera em background."""
    t = threading.Thread(target=wait_time_monitor_loop, daemon=True, name="WaitTimeMonitor")
    t.start()
    print("[MONITOR] Thread WaitTimeMonitor iniciada com sucesso.")

start_wait_time_monitor()


def fetch_and_update_avatar_async(contact_id, phone, instance):
    def _fetch():
        try:
            url = f"{WAHA_API_URL}/api/contacts/profilePicture?session={instance}&phone={phone}@c.us"
            res = requests.get(url, headers=get_waha_headers(), timeout=10)
            if res.status_code == 200:
                data = res.json()
                picture_url = data.get('profilePictureUrl')
                if picture_url:
                    with app.app_context():
                        contact = Contact.query.get(contact_id)
                        if contact:
                            contact.avatar = picture_url
                            db_sql.session.commit()
                            
                            # Emitir socket para atualizar o frontend
                            socketio.emit('chat_avatar_updated', {
                                'id': contact_id,
                                'avatar': picture_url
                            }, room=f'instance_{instance}')
                            socketio.emit('chat_avatar_updated', {
                                'id': contact_id,
                                'avatar': picture_url
                            }, room='admin')
        except Exception as e:
            print(f"[Avatar] Erro ao buscar foto para {phone}: {e}")

    threading.Thread(target=_fetch).start()

@app.route('/api/webhooks/waha', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data: return 'OK', 200
        # ---- WAHA TO INTERNAL CONVERTER ----
        if data.get('event') in ('message', 'message.any', 'message.ack') and 'payload' in data:
            waha_event = data.get('event')
            session = data.get('session')
            payload = data.get('payload', {})
            
            if waha_event == 'message.ack':
                ack_val = payload.get('ack', 0)
                ack_name = payload.get('ackName', '')
                waha_id = payload.get('id', '')
                
                # WAHA costuma enviar "false_numero@c.us_HASH" no message.ack
                db_msg_id = waha_id
                if waha_id and '_' in waha_id:
                    db_msg_id = waha_id.split('_')[-1]
                
                # Tenta atualizar no BD (case-insensitive para lidar com hashes do WAHA/Baileys)
                msg_obj = Message.query.filter(Message.id.ilike(db_msg_id)).first()
                if not msg_obj:
                    # Fallback pro id completo
                    msg_obj = Message.query.filter(Message.id.ilike(waha_id)).first()
                    if msg_obj:
                        db_msg_id = msg_obj.id
                elif msg_obj:
                    # Garantir que o db_msg_id enviado ao front seja exatamente como está no BD
                    db_msg_id = msg_obj.id
                
                print(f"[ACK] Recebido ack={ack_val} ({ack_name}) para msg={waha_id}. Encontrou BD? {'Sim' if msg_obj else 'Nao'}")
                        
                if msg_obj:
                    msg_obj.ack = ack_val
                    db_sql.session.commit()
                
                # Emitir socket para a interface com o ID real
                ack_data = {
                    'event': 'message.ack',
                    'instance': session,
                    'messageId': db_msg_id,
                    'ack': ack_val,
                    'ackName': ack_name
                }
                socketio.emit('whatsapp_ack', ack_data, room=f'instance_{session}')
                socketio.emit('whatsapp_ack', ack_data, room='admin')
                return 'OK', 200
            waha_id = payload.get('id', '')
            waha_from = payload.get('from', '')
            waha_to = payload.get('to', '')
            
            # NOWEB support for @lid fallback
            remote_jid_alt = payload.get('_data', {}).get('key', {}).get('remoteJidAlt')
            if remote_jid_alt:
                if waha_from and waha_from.endswith('@lid'):
                    waha_from = remote_jid_alt
                if waha_to and waha_to.endswith('@lid'):
                    waha_to = remote_jid_alt
                    
            fromMe = payload.get('fromMe', False)
            body = payload.get('body', '')
            msg_type = payload.get('type', 'chat')
            
            # Inferir tipo de mídia pelo mimetype ou por campos presentes se necessário
            if payload.get('hasMedia') and msg_type in ('chat', None, ''):
                mimetype = payload.get('media', {}).get('mimetype', '')
                if mimetype.startswith('audio/'):
                    msg_type = 'audio'
                elif mimetype.startswith('image/'):
                    msg_type = 'image'
                elif mimetype.startswith('video/'):
                    msg_type = 'video'
                else:
                    msg_type = 'document'
            elif msg_type in ('chat', None, ''):
                if payload.get('location'):
                    msg_type = 'location'
                elif payload.get('vCards'):
                    msg_type = 'contact'

            # --- DOWNLOAD AUTOMÁTICO DE MEDIA DA PAYLOAD DO WAHA ---
            # Sempre forçar download se for mídia (mesmo que a payload.media não venha completa)
            if payload.get('hasMedia') or msg_type in ('image', 'video', 'document', 'audio', 'ptt', 'voice'):
                media_dir = os.path.join(DATA_DIR, 'media')
                os.makedirs(media_dir, exist_ok=True)
                short_id = waha_id.split('_')[-1] if '_' in waha_id else waha_id
                
                # --- Determinar extensão CORRETA baseada no mimetype real ---
                media_info = payload.get('media', {})
                media_mimetype = media_info.get('mimetype', '')
                media_url = media_info.get('url')
                media_b64 = media_info.get('data')
                
                ext = ''
                if msg_type == 'image':
                    if 'png' in media_mimetype: ext = '.png'
                    elif 'webp' in media_mimetype: ext = '.webp'
                    elif 'gif' in media_mimetype: ext = '.gif'
                    else: ext = '.jpeg'
                elif msg_type in ('audio', 'voice', 'ptt'):
                    ext = '.oga'
                elif msg_type == 'video':
                    ext = '.mp4'
                elif msg_type == 'document':
                    # Tentar pela extensão do fileName primeiro
                    doc_filename = payload.get('body', '') or payload.get('_data', {}).get('message', {}).get('documentMessage', {}).get('fileName', '')
                    if doc_filename:
                        _, doc_ext = os.path.splitext(doc_filename)
                        if doc_ext: ext = doc_ext
                    # Fallback pelo mimetype
                    if not ext:
                        import mimetypes as _mt
                        guessed = _mt.guess_extension(media_mimetype.split(';')[0].strip())
                        ext = guessed if guessed else '.bin'
                
                filepath = os.path.join(media_dir, f"{short_id}{ext}")
                # Também salvar com o ID completo para que o proxy encontre por ambos
                filepath_full = os.path.join(media_dir, f"{waha_id}{ext}") if waha_id != short_id else None
                
                import glob as _glob
                # Verificar se JÁ existe com qualquer extensão (evitar re-download)
                already_exists = False
                for check_id in set([short_id, waha_id]):
                    check_path = os.path.join(media_dir, check_id)
                    if _glob.glob(check_path + '.*') or os.path.exists(check_path):
                        already_exists = True
                        break

                if not already_exists:
                    saved_locally = False
                    file_bytes_saved = None
                    try:
                        if media_b64:
                            import base64
                            file_bytes_saved = base64.b64decode(media_b64)
                            with open(filepath, 'wb') as f:
                                f.write(file_bytes_saved)
                            saved_locally = True
                            print(f"[Media] Arquivo {filepath} salvo localmente via Base64 do webhook")
                        elif media_url:
                            # Corrige URL caso venha localhost
                            if media_url.startswith('http://localhost') or media_url.startswith('http://127.0.0.1'):
                                from urllib.parse import urlparse
                                parsed = urlparse(media_url)
                                media_url = f"{WAHA_API_URL}{parsed.path}?{parsed.query}" if parsed.query else f"{WAHA_API_URL}{parsed.path}"
                                
                            dl_res = requests.get(media_url, headers=get_waha_headers(), timeout=15)
                            if dl_res.status_code == 200:
                                file_bytes_saved = dl_res.content
                                with open(filepath, 'wb') as f:
                                    f.write(file_bytes_saved)
                                saved_locally = True
                                print(f"[Media] Arquivo {filepath} salvo localmente via URL {media_url} do webhook")
                            else:
                                print(f"[Media] Falha ao baixar da URL {media_url}: HTTP {dl_res.status_code}")
                    except Exception as e:
                        print(f"[Media] Erro ao salvar media via payload: {e}")
                        
                    # Fallback Agressivo: Se falhou ao salvar pela payload, forçar busca via GET /api/files
                    if not saved_locally:
                        try:
                            waha_url_1 = f"{WAHA_API_URL}/api/files"
                            dl_res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': session, 'messageId': waha_id}, timeout=15)
                            
                            # Tentar fallback com id curto
                            if dl_res.status_code == 404 and short_id != waha_id:
                                dl_res = requests.get(waha_url_1, headers=get_waha_headers(), params={'session': session, 'messageId': short_id}, timeout=15)
                                
                            if dl_res.status_code == 200:
                                file_bytes_saved = dl_res.content
                                ctype = dl_res.headers.get('Content-Type', '')
                                if 'application/json' in ctype:
                                    import base64, re
                                    json_data = dl_res.json()
                                    if 'data' in json_data:
                                        raw = json_data['data']
                                        raw = re.sub(r'[^A-Za-z0-9+/]', '', raw)
                                        raw += "=" * ((4 - len(raw) % 4) % 4)
                                        file_bytes_saved = base64.b64decode(raw)
                                    elif 'url' in json_data:
                                        real_url = json_data['url']
                                        if real_url.startswith('http://localhost') or real_url.startswith('http://127.0.0.1'):
                                            from urllib.parse import urlparse
                                            real_url = f"{WAHA_API_URL}{urlparse(real_url).path}"
                                        real_res = requests.get(real_url, headers=get_waha_headers(), timeout=15)
                                        if real_res.status_code == 200:
                                            file_bytes_saved = real_res.content
                                with open(filepath, 'wb') as f:
                                    f.write(file_bytes_saved)
                                saved_locally = True
                                print(f"[Media] Arquivo {filepath} salvo via FALLBACK Agressivo (GET /api/files)")
                            else:
                                print(f"[Media] FALLBACK Agressivo falhou: HTTP {dl_res.status_code}")
                        except Exception as e:
                            print(f"[Media] Erro no FALLBACK Agressivo: {e}")
                    
                    # Criar cópia com o ID completo (para o proxy encontrar por ambos os nomes)
                    if saved_locally and filepath_full and file_bytes_saved:
                        try:
                            with open(filepath_full, 'wb') as f:
                                f.write(file_bytes_saved)
                        except Exception:
                            pass  # Não é crítico, o short_id já foi salvo
                            
                    # ── Upload para MinIO ──────────────────────────────────────
                    if saved_locally and file_bytes_saved:
                        _mime_map = {
                            'image': 'image/jpeg', 'video': 'video/mp4',
                            'audio': 'audio/ogg', 'ptt': 'audio/ogg',
                            'voice': 'audio/ogg', 'document': 'application/octet-stream'
                        }
                        upload_ct = _mime_map.get(msg_type, 'application/octet-stream')
                        if media_mimetype:
                            upload_ct = media_mimetype.split(';')[0].strip()
                        
                        minio_key = f"{short_id}{ext}"
                        minio_upload(file_bytes_saved, minio_key, upload_ct)
                        
                        if waha_id != short_id:
                            minio_upload(file_bytes_saved, f"{waha_id}{ext}", upload_ct)
                    # ──────────────────────────────────────────────────────────
            # -------------------------------------------------------

            # --- Normalizar JID para 12 dígitos ---
            raw_jid = waha_to if fromMe else waha_from
            norm_phone = normalize_br_phone(raw_jid)
            final_jid = f"{norm_phone}@s.whatsapp.net" if norm_phone else raw_jid
            # ----------------------------------------
            
            evo_data = {
                "event": "messages.upsert",
                "instance": session,
                "data": {
                    "key": {
                        "remoteJid": final_jid,
                        "fromMe": fromMe,
                        "id": waha_id
                    },
                    "message": {}
                }
            }
            
            media_b64 = ''
            waha_media_url = ''
            public_media_url = ''
            if payload.get('hasMedia') and 'media' in payload:
                media_info = payload.get('media', {})
                media_b64 = media_info.get('data', '')
                waha_media_url = media_info.get('url', '')
                if waha_media_url.startswith('http://localhost'):
                    from urllib.parse import urlparse
                    parsed = urlparse(waha_media_url)
                    waha_media_url = f"{WAHA_API_URL}{parsed.path}?{parsed.query}" if parsed.query else f"{WAHA_API_URL}{parsed.path}"
                try:
                    media_token = jwt.encode({'media_access': True, 'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)}, JWT_SECRET, algorithm="HS256")
                    public_media_url = f"{request.host_url.rstrip('/')}/api/media/audio?instance={session}&msg_id={waha_id}&token={media_token}"
                except Exception:
                    pass

            if payload.get('hasMedia') or msg_type in ('image', 'video', 'document', 'audio', 'ptt', 'voice'):
                base_media_data = {"base64": media_b64, "wahaUrl": waha_media_url}
                if msg_type == 'image':
                    evo_data['data']['message']['imageMessage'] = {"caption": body, "url": public_media_url.replace('/audio?', '/image?'), **base_media_data}
                elif msg_type == 'video':
                    evo_data['data']['message']['videoMessage'] = {"caption": body, "url": public_media_url.replace('/audio?', '/video?'), **base_media_data}
                elif msg_type in ('audio', 'ptt', 'voice'):
                    evo_data['data']['message']['audioMessage'] = {"url": public_media_url, **base_media_data}
                elif msg_type == 'document':
                    evo_data['data']['message']['documentMessage'] = {"fileName": body or 'Arquivo', "url": public_media_url.replace('/audio?', '/document?'), **base_media_data}
                else:
                    # fallback
                    evo_data['data']['message']['documentMessage'] = {"fileName": 'Arquivo', "url": public_media_url.replace('/audio?', '/document?'), **base_media_data}
            elif msg_type == 'location':
                loc_name = payload.get('location', {}).get('description') or payload.get('body') or ''
                evo_data['data']['message']['locationMessage'] = {
                    "degreesLatitude": payload.get('location', {}).get('latitude', ''),
                    "degreesLongitude": payload.get('location', {}).get('longitude', ''),
                    "name": loc_name,
                }
            elif msg_type in ('vcard', 'contact'):
                vcards = payload.get('vCards') or []
                vcard_str = vcards[0] if isinstance(vcards, list) and len(vcards) > 0 else (body or '')
                
                display_name = "Contato"
                if vcard_str and isinstance(vcard_str, str):
                    for line in vcard_str.split('\n'):
                        if line.startswith('FN:'):
                            display_name = line.split('FN:')[1].strip()
                            break
                            
                evo_data['data']['message']['contactMessage'] = {
                    "vcard": vcard_str,
                    "displayName": display_name
                }
            else:
                evo_data['data']['message']['conversation'] = body

            data = evo_data
        # ---- FIM CONVERTER ----
        
        event = data.get('event')
        instance = data.get('instance')
        
        # n8n Forwarding per instance
        webhook_key = f"n8n_webhook_{instance}"
        n8n_set = Setting.query.get(webhook_key)
        if n8n_set and n8n_set.value:
            try: requests.post(n8n_set.value, json=data, timeout=5)
            except: pass

        if event in ('messages.upsert', 'send.message'):
            msg_data = data.get('data', {})
            key = msg_data.get('key', {})
            remoteJid = key.get('remoteJid', '')
            if not remoteJid or remoteJid == 'status@broadcast': return 'OK', 200

            phone_original = remoteJid.split('@')[0].split(':')[0]
            phone = normalize_br_phone(phone_original)
            
            if phone != phone_original:
                key['remoteJid'] = f"{phone}@s.whatsapp.net"

            fromMe = key.get('fromMe', False)
            
            m = msg_data.get('message', {})
            msg_id_key = key.get('id', '')
            if 'audioMessage' in m:
                text = f"[AUDIO_REF] {instance}|{msg_id_key}"
                print(f"[Audio] Guardando ref de audio: instance={instance} msg_id={msg_id_key}")
            elif 'imageMessage' in m:
                caption = m.get('imageMessage', {}).get('caption', '')
                text = f"[IMAGE_REF] {instance}|{msg_id_key}"
                if caption:
                    text += f"\n{caption}"
                print(f"[Image] Guardando ref de imagem: instance={instance} msg_id={msg_id_key}")
            elif 'videoMessage' in m:
                caption = m.get('videoMessage', {}).get('caption', '')
                text = f"[VIDEO_REF] {instance}|{msg_id_key}"
                if caption:
                    text += f"\n{caption}"
                print(f"[Video] Guardando ref de video: instance={instance} msg_id={msg_id_key}")
            elif 'documentMessage' in m:
                doc_name = m.get('documentMessage', {}).get('fileName', 'Arquivo')
                text = f"[DOC_REF] {instance}|{msg_id_key}|{doc_name}"
                print(f"[Doc] Guardando ref de documento: instance={instance} msg_id={msg_id_key}")
            elif 'locationMessage' in m:
                lat = m.get('locationMessage', {}).get('degreesLatitude', '')
                lng = m.get('locationMessage', {}).get('degreesLongitude', '')
                name = m.get('locationMessage', {}).get('name', '')
                address = m.get('locationMessage', {}).get('address', '')
                text = f"[LOCATION_REF] {lat}|{lng}|{name}|{address}"
                print(f"[Location] Guardando ref de localizacao: instance={instance} msg_id={msg_id_key}")
            elif 'contactMessage' in m:
                contact_data = m.get('contactMessage', {})
                display_name = contact_data.get('displayName', 'Contato')
                vcard = contact_data.get('vcard', '')
                
                contact_phone = ''
                for line in vcard.split('\n'):
                    if 'waid=' in line:
                        contact_phone = line.split('waid=')[1].split(':')[0]
                        break
                    elif line.strip().upper().startswith('TEL'):
                        contact_phone = line.split(':')[-1].strip().replace('\r', '')
                
                text = f"[CONTACT_REF] {display_name}|{contact_phone}|{vcard}"
                print(f"[Contact] Guardando ref de contato: name={display_name} phone={contact_phone}")
                
            elif 'contactsArrayMessage' in m:
                contacts_list = m.get('contactsArrayMessage', {}).get('contacts', [])
                names = ', '.join([c.get('displayName', '?') for c in contacts_list])
                
                contact_phone = ''
                if contacts_list:
                    vcard = contacts_list[0].get('vcard', '')
                    for line in vcard.split('\n'):
                        if 'waid=' in line:
                            contact_phone = line.split('waid=')[1].split(':')[0]
                            break
                        elif line.strip().upper().startswith('TEL'):
                            contact_phone = line.split(':')[-1].strip().replace('\r', '')
                            
                text = f"[CONTACT_REF] {names}|{contact_phone}|{str(contacts_list)}"
                print(f"[Contact] Guardando array de contatos: {names}")
            else:
                text = m.get('conversation') or \
                       m.get('extendedTextMessage', {}).get('text') or \
                       m.get('buttonsResponseMessage', {}).get('selectedDisplayText') or \
                       m.get('listResponseMessage', {}).get('title') or \
                       "[Mensagem N8N/Mídia]"

            now = get_now()
            time_str = now.strftime("%d/%m %H:%M")
            contact_id = f"c_{phone}_{instance}"

            # Update/Create Contact
            contact = Contact.query.filter_by(id=contact_id).first()
            if not contact:
                contact = Contact(
                    id=contact_id, name=phone, phone=phone,
                    avatar=phone[0] if phone else "?",
                    instance=instance,
                    tags=['Novo Lead'] if fromMe else ['Novo Lead', 'Não Lido'], last_msg=text, last_msg_time=time_str,
                    unread=0 if fromMe else 1
                )
                db_sql.session.add(contact)
                db_sql.session.flush()
                # Chama a busca de foto
                fetch_and_update_avatar_async(contact_id, phone, instance)
                # Inicia contagem de tempo de espera para novo contato (se não enviado por nós)
                if not fromMe:
                    try:
                        te_aberto = TempoEspera.query.filter_by(numero_cliente=phone).filter(TempoEspera.atendido == None).first()
                        if not te_aberto:
                            novo_te = TempoEspera(numero_cliente=phone, inicio=get_now())
                            db_sql.session.add(novo_te)
                            db_sql.session.flush()
                    except Exception as e_te_new:
                        print(f"[TempoEspera] Erro ao criar registro (novo contato): {e_te_new}")
            else:
                contact.last_msg = text
                contact.last_msg_time = time_str
                if not fromMe:
                    contact.unread = (contact.unread or 0) + 1
                    if not contact.assigned_to:
                        tags = list(contact.tags or [])
                        if 'Não Lido' not in tags:
                            tags.append('Não Lido')
                            # Inicia/reinicia contagem de tempo de espera
                            try:
                                te_aberto = TempoEspera.query.filter_by(numero_cliente=phone).filter(TempoEspera.atendido == None).first()
                                if not te_aberto:
                                    novo_te = TempoEspera(numero_cliente=phone, inicio=get_now())
                                    db_sql.session.add(novo_te)
                                    db_sql.session.flush()
                            except Exception as e_te_ex:
                                print(f"[TempoEspera] Erro ao criar registro (contato existente): {e_te_ex}")
                        contact.tags = tags
                        flag_modified(contact, 'tags')
                    
                # Se não tem foto real (é só uma letra ou '?'), tenta buscar
                if not contact.avatar or len(contact.avatar) <= 2 or not contact.avatar.startswith('http'):
                    fetch_and_update_avatar_async(contact_id, phone, instance)
            
            db_sql.session.flush()

            # Save Message

            msg_id = key.get('id')
            if not Message.query.get(msg_id):
                new_msg = Message(
                    id=msg_id,
                    contact_id=contact_id,
                    text=text,
                    type='out' if fromMe else 'in',
                    time=time_str,
                    timestamp=int(now.timestamp()),
                    instance=instance
                )
                db_sql.session.add(new_msg)
                
                # Registra evento SLA de mensagem
                track_sla_event(phone, event_type='ATTENDANT_MSG' if fromMe else 'CLIENT_MSG')
                
                if not fromMe:
                    # Verifica se este cliente estava na fila aguardando resposta
                    pending_reqs = ContactRequest.query.filter_by(status='PENDING').all()
                    req = None
                    for pr in pending_reqs:
                        # Compara os últimos 8 dígitos (ignora DDD e o 9 extra do BR) para evitar falhas
                        if pr.phone[-8:] == phone[-8:]:
                            req = pr
                            break
                            
                    if req:
                        req.status = 'ANSWERED'
                        
                        # Atribuir o chat automaticamente para o atendente
                        # Primeiro tenta buscar por email (novo padrão), se não achar tenta por nome (requests antigas)
                        att_user = User.query.filter_by(email=req.attendant_name).first()
                        if not att_user:
                            att_user = User.query.filter_by(name=req.attendant_name).first()
                            
                        # Notifica o N8N que o cliente respondeu
                        try:
                            n8n_payload = {
                                "phone": req.phone,
                                "attendant": att_user.name if att_user else req.attendant_name,
                                "attendant_email": req.attendant_name,
                                "filial": req.filial,
                                "setor": req.setor,
                                "reason": req.reason,
                                "is_first_time": False,
                                "request_id": req.id
                            }
                            requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/chamar-atendente-solicitado", json=n8n_payload, timeout=5)
                        except Exception as e:
                            print(f"Erro N8N atendido: {e}")
                            
                        if att_user:
                            contact.assigned_to = att_user.id
                            contact.assigned_name = att_user.name
                            tags = list(contact.tags or [])
                            tags = [t for t in tags if str(t).upper() != 'BOT']
                            at_tag = f"Atendente: {att_user.email}"
                            if at_tag not in tags: tags.append(at_tag)
                            if att_user.filial and att_user.setor:
                                fst = f"{att_user.filial}:{att_user.setor}"
                                if fst not in tags: tags.append(fst)
                            contact.tags = tags
                            flag_modified(contact, 'tags')
                            
                            # Atualiza AtendimentoChat
                            agora_iso = get_now().isoformat()
                            atend = AtendimentoChat.query.filter_by(numero=phone).first()
                            if atend:
                                atend.atendente = att_user.name
                                atend.status = 'atendente'
                                atend.ultimo_atendente = att_user.name
                                atend.registro_time_chat = agora_iso
                                atend.atendente_desde = agora_iso
                            else:
                                atend = AtendimentoChat(numero=phone, status='atendente', atendente=att_user.name, ultimo_atendente=att_user.name, registro_time_chat=agora_iso, atendente_desde=agora_iso)
                                db_sql.session.add(atend)

            db_sql.session.commit()

            # --- Corpal Webhook Removido ---

            # Emitir evento com texto processado para o frontend
            emit_data = dict(data)
            emit_data['_processed_text'] = text
            emit_data['_instance'] = instance
            socketio.emit('whatsapp_event', emit_data, room=f'instance_{instance}')
            socketio.emit('whatsapp_event', emit_data, room='admin')
        return 'OK', 200
    except Exception as e:
        print(f"Erro webhook: {e}")
        return 'ERR', 500

@app.route('/api/contacts', methods=['GET'])
@auth_required
def get_contacts():
    # Todos os usuários veem todos os chats sem restrições
    contacts = Contact.query.all()

    contacts_list = []
    for c in contacts:
        contacts_list.append({
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'avatar': c.avatar,
            'instance': c.instance,
            'tags': c.tags or [],
            'lastMsg': c.last_msg,
            'time': c.last_msg_time,
            'unread': c.unread,
            'assigned_to': c.assigned_to,
            'assigned_name': c.assigned_name
        })
    return jsonify(contacts_list)

@app.route('/api/contacts/<id>', methods=['PUT'])
@auth_required
def update_contact(id):
    data = request.json
    contact = Contact.query.filter_by(id=id).first()
    
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    if 'name' in data:
        new_name = data.get('name')
        if not new_name:
            return jsonify({'error': 'Nome é obrigatório'}), 400
        contact.name = new_name
        if contact.avatar and len(contact.avatar) <= 1:
            contact.avatar = new_name[0].upper()
            
    if 'tags' in data:
        contact.tags = data.get('tags')
        flag_modified(contact, 'tags')
        
    db_sql.session.commit()
    
    if 'tags' in data:
        _inst_room = contact.instance or 'unknown'
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags or [])
        }, room=f'instance_{_inst_room}')
        socketio.emit('chat_tags_updated', {
            'id': contact.id,
            'tags': list(contact.tags or [])
        }, room='admin')
        
    return jsonify({
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'avatar': contact.avatar,
        'tags': contact.tags
    })


@app.route('/api/contacts', methods=['POST'])
@auth_required
def create_contact():
    data = request.json
    phone = data.get('phone')
    instance = data.get('instance')
    
    if not phone or not instance:
        return jsonify({'error': 'Telefone e Instância são obrigatórios'}), 400
        
    # Normaliza ANTES de validar o tamanho para evitar rejeitar números válidos
    phone = normalize_br_phone(str(phone).strip())
    if len(phone) < 12 or len(phone) > 13:
        return jsonify({'error': 'Formato inválido! Insira DDI + DDD + Número (Ex: 5535999888777)'}), 400
        
    valid_phone = get_valid_waha_number(phone, instance, WAHA_API_URL, get_waha_headers())
    if not valid_phone:
        return jsonify({'error': 'Este número não possui WhatsApp ativo ou é inválido.'}), 400
    phone = valid_phone
        
    contact_id = f"c_{phone}_{instance}"
    
    contact = Contact.query.filter_by(id=contact_id).first()
    if not contact:
        contact = Contact(
            id=contact_id,
            name=phone,
            phone=phone,
            avatar=phone[0] if phone else "?",
            instance=instance,
            tags=['Novo Lead'],
            last_msg='Iniciando conversa...',
            last_msg_time=get_now().strftime('%H:%M'),
            unread=0
        )
        db_sql.session.add(contact)
    else:
        # Contato já existe — remove tag BOT para o atendente assumir o controle
        current_tags = list(contact.tags or [])
        current_tags = [t for t in current_tags if isinstance(t, str) and t.strip().upper() != 'BOT']
        contact.tags = current_tags
        flag_modified(contact, 'tags')
    
    user = User.query.get(request.user['id'])
    contact.assigned_to = user.id
    contact.assigned_name = user.name
    
    atendente_tag = f"Atendente: {user.name}"
    tags = list(contact.tags or [])
    if atendente_tag not in tags:
        tags.append(atendente_tag)
    
    # Adicionar tag Filial:Setor do atendente para roteamento correto
    if user.filial and user.setor:
        filial_setor_tag = f"{user.filial}:{user.setor}"
        if filial_setor_tag not in tags:
            tags.append(filial_setor_tag)
    elif user.filial_id and user.setor_id:
        _f = Filial.query.get(user.filial_id)
        _s = Setor.query.get(user.setor_id)
        if _f and _s:
            filial_setor_tag = f"{_f.name}:{_s.name}"
            if filial_setor_tag not in tags:
                tags.append(filial_setor_tag)
    
    contact.tags = tags
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    now = get_now()
    _filial_a = None
    _setor_a = None
    if user.filial_id:
        _f = Filial.query.get(user.filial_id)
        _filial_a = _f.name if _f else None
    if user.setor_id:
        _s = Setor.query.get(user.setor_id)
        _setor_a = _s.name if _s else None
        
    try:
        corpal_payload = {
            "evento": "atender",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_a,
            "setor": _setor_a,
            "nome_atendente": user.name,
            "atendente_id": str(user.id),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook corpal (assign novo chat): {e}")

    # Atualiza tabela atendimentos_chat diretamente para bloquear o bot
    try:
        agora_iso = get_now().isoformat()
        atend_chat = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat:
            status_anterior = atend_chat.status
            atend_chat.atendente = user.name
            atend_chat.status = 'atendente'
            atend_chat.ultimo_atendente = user.name
            atend_chat.registro_time_chat = agora_iso
            if status_anterior != 'atendente':
                atend_chat.atendente_desde = agora_iso
                atend_chat.alerta_20min_enviado = False
                atend_chat.alerta_40min_enviado = False
        else:
            atend_chat = AtendimentoChat(
                numero=contact.phone,
                status='atendente',
                atendente=user.name,
                ultimo_atendente=user.name,
                registro_time_chat=agora_iso,
                atendente_desde=agora_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(atend_chat)
        db_sql.session.commit()
        print(f"[NOVO CHAT] atendimentos_chat atualizado: numero={contact.phone}, atendente={user.name}")
    except Exception as e_ac:
        db_sql.session.rollback()
        print(f"Erro ao atualizar atendimentos_chat (novo chat): {e_ac}")

    try:
        if os.getenv('WEBHOOK_ATENDIMENTO_URL'):
            webhook_payload = {
                "evento": "atendimento_iniciado",
                "contato": {
                    "id": contact.id,
                    "phone": contact.phone,
                    "name": contact.name,
                    "instance": contact.instance
                },
                "atendente": {
                    "id": user.id,
                    "name": user.name,
                    "email": user.email
                },
                "timestamp": now.isoformat()
            }
            requests.post(os.getenv('WEBHOOK_ATENDIMENTO_URL'), json=webhook_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook atendimento (assign novo chat): {e}")

    socketio.emit('chat_assigned', {
        'contact_id': contact.id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags
    })

    return jsonify({
        'id': contact.id,
        'name': contact.name,
        'phone': contact.phone,
        'avatar': contact.avatar,
        'instance': contact.instance,
        'tags': contact.tags,
        'assigned_to': contact.assigned_to,
        'assigned_name': contact.assigned_name
    }), 201

@app.route('/api/contact-requests', methods=['GET'])
@auth_required
def list_contact_requests():
    user = User.query.get(request.user['id'])
    # Retorna apenas as solicitações feitas pelo atendente logado (agora rastreado por email)
    requests_db = ContactRequest.query.filter_by(attendant_name=user.email).order_by(ContactRequest.created_at.desc()).limit(50).all()
    result = []
    for r in requests_db:
        result.append({
            'id': r.id,
            'phone': r.phone,
            'attendant_name': r.attendant_name,
            'reason': r.reason,
            'status': r.status,
            'created_at': r.created_at.isoformat() + 'Z'
        })
    return jsonify(result), 200

@app.route('/api/contact-requests', methods=['POST'])
@auth_required
def create_contact_request():
    data = request.json
    phone = data.get('phone')
    reason = data.get('reason')
    
    if not phone or not reason:
        return jsonify({'error': 'Telefone e Motivo são obrigatórios'}), 400
        
    phone = normalize_br_phone(str(phone).strip())
    if len(phone) < 12 or len(phone) > 13:
        return jsonify({'error': 'Formato inválido! Insira DDI + DDD + Número (Ex: 5535999888777)'}), 400
        
    user = User.query.get(request.user['id'])
    
    # 1. Verificar se o número já está sendo atendido por outra pessoa
    atend_chat = AtendimentoChat.query.filter_by(numero=phone).first()
    if atend_chat and atend_chat.status == 'atendente' and atend_chat.atendente != user.name:
        return jsonify({'error': f'Este número já está em atendimento por {atend_chat.atendente}.'}), 403
        
    # Verificar se já existe uma solicitação pendente para este número
    existing_req = ContactRequest.query.filter_by(phone=phone, status='PENDING').first()
    if existing_req:
        if existing_req.attendant_name != user.email:
            # existing_req.attendant_name stores the email now
            return jsonify({'error': f'Este número já possui uma solicitação pendente por outro atendente ({existing_req.attendant_name}).'}), 403
        else:
            return jsonify({'error': 'Você já possui uma solicitação pendente para este número.'}), 403
        
    # 2. Criar a solicitação
    _f = Filial.query.get(user.filial_id) if user.filial_id else None
    _s = Setor.query.get(user.setor_id) if user.setor_id else None
    
    filial_name = user.filial or (_f.name if _f else '')
    setor_name = user.setor or (_s.name if _s else '')
    
    new_req = ContactRequest(
        phone=phone,
        attendant_name=user.email,  # Mudança crucial: gravamos o EMAIL para diferenciar homônimos
        filial=filial_name,
        setor=setor_name,
        reason=reason,
        status='PENDING',
        is_first_time=True
    )
    db_sql.session.add(new_req)
    db_sql.session.commit()
    
    # 3. Disparar webhook para o N8N (bot de disparo inicial)
    try:
        n8n_payload = {
            "phone": phone,
            "attendant": user.name,
            "attendant_email": user.email, # N8N agora tem acesso ao e-mail exato
            "filial": filial_name,
            "setor": setor_name,
            "reason": reason,
            "is_first_time": True,
            "request_id": new_req.id
        }
        requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/atendido", json=n8n_payload, timeout=5)
    except Exception as e:
        print(f"Erro ao notificar N8N da solicitacao: {e}")
        
    return jsonify({'success': True, 'message': 'Solicitação enviada com sucesso!'}), 201


@app.route('/api/contacts/<id>/read', methods=['POST'])
@auth_required
def read_contact(id):
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    contact.unread = 0
    db_sql.session.commit()
    return jsonify({'success': True})

@app.route('/api/contacts/<id>/messages', methods=['GET'])
@auth_required
def get_messages(id):
    # Expect id to be the full c_phone_instance string
    msgs = Message.query.filter(Message.contact_id == id).order_by(Message.timestamp).all()
    
    msgs_list = []
    for m in msgs:
        msgs_list.append({
            'id': m.id,
            'text': m.text,
            'type': m.type,
            'time': m.time,
            'timestamp': m.timestamp,
            'ack': m.ack if m.ack is not None else 2
        })
    return jsonify(msgs_list)

# ─── Atendimento (Assign / Release) ─────────────────────────────────────────

@app.route('/api/contacts/<id>/assign', methods=['POST'])
@auth_required
def assign_chat(id):
    """Atender: atribui o chat ao usuário logado."""
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
    
    if contact.assigned_to and contact.assigned_to != request.user['id']:
        return jsonify({'error': f'Chat já está sendo atendido por {contact.assigned_name}'}), 409
    
    user = User.query.get(request.user['id'])
    contact.assigned_to = user.id
    contact.assigned_name = user.name
    
    atendente_tag = f"Atendente: {user.name}"
    # Preserva tags de Filial:Setor, remove BOT e tag de atendente anterior
    current_tags = list(contact.tags or [])
    new_tags = [
        t for t in current_tags
        if isinstance(t, str)
        and not t.strip().lower().startswith('atendente:')
        and t.strip().upper() != 'BOT'
        and t.strip().lower() != 'não lido'
        and t.strip().lower() != 'nao lido'
    ]
    if atendente_tag not in new_tags:
        new_tags.append(atendente_tag)
    contact.tags = new_tags
    flag_modified(contact, 'tags')
    
    db_sql.session.commit()
    
    track_sla_event(contact.phone, atendente=user.name, event_type='ASSIGNED')
    
    # Atualiza o registro de TempoEspera para marcar o momento do atendimento
    try:
        te_aberto = TempoEspera.query.filter_by(numero_cliente=contact.phone).filter(TempoEspera.atendido == None).order_by(TempoEspera.id.desc()).first()
        if te_aberto:
            te_aberto.atendido = get_now()
            te_aberto.nome_atendente = user.name
            sf = ""
            if user.setor and user.filial:
                sf = f"{user.setor}:{user.filial}"
            elif user.setor:
                sf = user.setor
            elif user.filial:
                sf = user.filial
            te_aberto.setor_filial = sf
            db_sql.session.commit()
    except Exception as e_te:
        db_sql.session.rollback()
        print(f"[TempoEspera] Erro ao atualizar no assign: {e_te}")
    
    # Corpal Webhook — evento atender
    now = get_now()
    _filial_a = None
    _setor_a = None
    if user.filial_id:
        _f = Filial.query.get(user.filial_id)
        _filial_a = _f.name if _f else None
    if user.setor_id:
        _s = Setor.query.get(user.setor_id)
        _setor_a = _s.name if _s else None
        
    try:
        corpal_payload = {
            "evento": "atender",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_a,
            "setor": _setor_a,
            "nome_atendente": user.name,
            "atendente_id": str(user.id),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
    except Exception as e:
        print(f"Erro webhook corpal (assign): {e}")

    # Atualiza tabela atendimentos_chat diretamente para bloquear o bot
    try:
        agora_iso = get_now().isoformat()
        atend_chat = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat:
            status_anterior = atend_chat.status
            atend_chat.atendente = user.name
            atend_chat.status = 'atendente'
            atend_chat.ultimo_atendente = user.name
            atend_chat.registro_time_chat = agora_iso
            # Só reseta o timer de espera se estava em outro status (ex: bot)
            if status_anterior != 'atendente':
                atend_chat.atendente_desde = agora_iso
                atend_chat.alerta_20min_enviado = False
                atend_chat.alerta_40min_enviado = False
        else:
            atend_chat = AtendimentoChat(
                numero=contact.phone,
                status='atendente',
                atendente=user.name,
                ultimo_atendente=user.name,
                registro_time_chat=agora_iso,
                atendente_desde=agora_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(atend_chat)
        db_sql.session.commit()
        print(f"[ASSIGN] atendimentos_chat atualizado: numero={contact.phone}, atendente={user.name}")
    except Exception as e_ac:
        db_sql.session.rollback()
        print(f"Erro ao atualizar atendimentos_chat (assign): {e_ac}")
    
    _inst_room = contact.instance or 'unknown'
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags,
        'action': 'assign'
    }, room=f'instance_{_inst_room}')
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags,
        'action': 'assign'
    }, room='admin')
    
    return jsonify({
        'success': True,
        'assigned_to': user.id,
        'assigned_name': user.name,
        'tags': contact.tags
    })

@app.route('/api/contacts/<id>/release', methods=['POST'])
@auth_required
def release_chat(id):
    """Finalizar atendimento: libera o chat."""
    contact = Contact.query.filter_by(id=id).first()
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
    
    # Apenas o atendente atual ou admin podem finalizar
    if contact.assigned_to and contact.assigned_to != request.user['id'] and request.user.get('role') != 'admin':
        return jsonify({'error': 'Apenas o atendente atual pode finalizar o atendimento'}), 403
    
    user = User.query.get(request.user['id'])
    old_name = contact.assigned_name or user.name
    contact.assigned_to = None
    contact.assigned_name = None
    
    _filial_r = None
    _setor_r = None
    if user:
        if user.filial_id:
            _f = Filial.query.get(user.filial_id)
            _filial_r = _f.name if _f else None
        if user.setor_id:
            _s = Setor.query.get(user.setor_id)
            _setor_r = _s.name if _s else None
            
    # Ao finalizar: remove tag do atendente, mantém Filial:Setor, adiciona BOT
    current_tags = list(contact.tags or [])
    
    # Remove tags de atendente (ex: "Atendente: Fulano") — case-insensitive com strip
    preserved_tags = [
        t for t in current_tags
        if not (isinstance(t, str) and t.strip().lower().startswith('atendente:'))
    ]
    
    # Remove BOT caso já exista (para não duplicar) e adiciona no início
    preserved_tags = [t for t in preserved_tags if t.strip().upper() != 'BOT']
    preserved_tags.insert(0, 'BOT')
    
    contact.tags = preserved_tags
    flag_modified(contact, 'tags')
    
    # Lê motivo e detalhes do payload JSON
    data = request.get_json(silent=True) or {}
    motivo = data.get('motivo')
    detalhes = data.get('detalhes', '')

    if motivo:
        novo_motivo = MotivoFinalizacao(
            contact_id=contact.id,
            numero_cliente=contact.phone,
            atendente=old_name,
            motivo=motivo,
            detalhes=detalhes
        )
        db_sql.session.add(novo_motivo)
    
    db_sql.session.commit()
    
    # Registra no SLA que o atendimento foi finalizado
    track_sla_event(contact.phone, event_type='RELEASED')
    
    # Atualiza registro de TempoEspera marcando como finalizado
    try:
        te_aberto = TempoEspera.query.filter_by(numero_cliente=contact.phone).filter(TempoEspera.finalizado == None).order_by(TempoEspera.id.desc()).first()
        if te_aberto:
            te_aberto.finalizado = get_now()
            db_sql.session.commit()
    except Exception as e_te:
        db_sql.session.rollback()
        print(f"[TempoEspera] Erro ao finalizar no release: {e_te}")
    
    # Dispara Webhook NPS para o N8N
    try:
        nps_payload = {
            "numero": contact.phone,
            "instancia": contact.instance,
            "atendente": old_name,
            "filial": _filial_r,
            "setor": _setor_r,
            "timestamp": get_now().isoformat()
        }
        requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/acionar-nps", json=nps_payload, timeout=5)
    except Exception as nps_e:
        print(f"Erro ao disparar webhook NPS: {nps_e}")

    # Resetar alertas de espera ao finalizar o atendimento
    try:
        atend_chat_rel = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat_rel:
            atend_chat_rel.status = 'bot'
            atend_chat_rel.atendente = ''
            atend_chat_rel.atendente_desde = None
            atend_chat_rel.alerta_20min_enviado = False
            atend_chat_rel.alerta_40min_enviado = False
            db_sql.session.commit()
            print(f"[RELEASE] Alertas e atendente resetados para {contact.phone}")
    except Exception as e_rel:
        db_sql.session.rollback()
        print(f"Erro ao resetar alertas no release: {e_rel}")
    
    # Corpal Webhook — evento finalizar
    try:
        now = get_now()
        corpal_payload = {
            "evento": "finalizar",
            "atendimento_id": str(uuid.uuid4()),
            "numero_lead": contact.phone,
            "instancia": contact.instance,
            "filial": _filial_r,
            "setor": _setor_r,
            "nome_atendente": old_name,
            "atendente_id": str(request.user['id']),
            "direcao": None,
            "mensagem": None,
            "timestamp": now.isoformat()
        }
        requests.post(CORPAL_WEBHOOK_URL, json=corpal_payload, timeout=5)
        
        # Novo Webhook específico para finalizar atendimento
        try:
            n8n_final_payload = {
                "numero_lead": contact.phone,
                "nome_atendente": old_name,
                "setor": _setor_r,
                "filial": _filial_r
            }
            requests.post("https://n8n-n8n.ioms5g.easypanel.host/webhook/corpal-final-atendimento", json=n8n_final_payload, timeout=5)
        except Exception as e_n8n:
            print(f"Erro no webhook n8n-final-atendimento: {e_n8n}")
    except Exception as e:
        print(f"Erro webhook corpal (release): {e}")
    
    # Emitir socket para todos os clientes atualizarem
    _inst_room = contact.instance or 'unknown'
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': contact.tags,
        'action': 'release'
    }, room=f'instance_{_inst_room}')
    socketio.emit('chat_assignment', {
        'contact_id': id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': contact.tags,
        'action': 'release'
    }, room='admin')
    
    return jsonify({
        'success': True,
        'tags': contact.tags
    })

@app.route('/api/admin/settings', methods=['GET', 'POST'])
@auth_required
@admin_required
def manage_settings():
    if request.method == 'POST':
        data = request.json
        for k, v in data.items():
            setting = Setting.query.get(k)
            if setting:
                setting.value = str(v)
            else:
                db_sql.session.add(Setting(key=k, value=str(v)))
        db_sql.session.commit()
        
    all_s = Setting.query.all()
    return jsonify({s.key: s.value for s in all_s})

@app.route('/api/admin/deduplicate', methods=['POST'])
@auth_required
@admin_required
def api_deduplicate():
    try:
        from limpar_duplicados import run_deduplication
        stats = run_deduplication()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/migrate-filial-names', methods=['POST'])
@auth_required
@admin_required
def api_migrate_filial_names():
    try:
        # Criar a coluna no banco se não existir
        try:
            db_sql.session.execute(db_sql.text("ALTER TABLE setor ADD COLUMN filial_name VARCHAR(100)"))
            db_sql.session.commit()
        except Exception:
            db_sql.session.rollback()  # Coluna já existe, segue normal

        setores = Setor.query.all()
        count = 0
        for s in setores:
            filial = Filial.query.get(s.filial_id)
            if filial:
                s.filial_name = filial.name
                count += 1
        db_sql.session.commit()
        return jsonify({'updated': count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/fix-13-digits', methods=['POST'])
@auth_required
@admin_required
def api_fix_13_digits():
    try:
        updated_contacts = 0
        contacts = Contact.query.all()
        for c in contacts:
            number = c.phone
            if number and len(number) == 13 and number.startswith('55') and number[4] == '9':
                new_number = number[:4] + number[5:]
                old_id = c.id
                new_id = f"c_{new_number}_{c.instance}"
                
                # Verificar se o contato novo já existe, senao cria
                new_contact = Contact.query.get(new_id)
                if not new_contact:
                    new_contact = Contact(
                        id=new_id,
                        name=new_number if c.name == number else c.name,
                        phone=new_number,
                        avatar=c.avatar,
                        instance=c.instance,
                        tags=c.tags,
                        last_msg=c.last_msg,
                        last_msg_time=c.last_msg_time,
                        unread=c.unread,
                        assigned_to=c.assigned_to,
                        assigned_name=c.assigned_name
                    )
                    db_sql.session.add(new_contact)
                    db_sql.session.flush() # Salva no banco para que a foreign key seja satisfeita
                
                # Atualizar as mensagens para apontar para o novo contato
                Message.query.filter_by(contact_id=old_id).update({"contact_id": new_id})
                
                # Deletar o contato antigo
                db_sql.session.delete(c)
                updated_contacts += 1
        
        db_sql.session.commit()
        return jsonify({'success': True, 'updated': updated_contacts})
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/admin/migrate-to-corpal', methods=['POST'])
@auth_required
@admin_required
def api_migrate_to_corpal():
    try:
        updated_contacts = 0
        contacts = Contact.query.all()
        for c in contacts:
            if c.instance != 'corpal':
                old_id = c.id
                new_id = f"c_{c.phone}_corpal"
                
                new_contact = Contact.query.get(new_id)
                if not new_contact:
                    new_contact = Contact(
                        id=new_id,
                        name=c.name,
                        phone=c.phone,
                        avatar=c.avatar,
                        instance='corpal',
                        tags=c.tags,
                        last_msg=c.last_msg,
                        last_msg_time=c.last_msg_time,
                        unread=c.unread,
                        assigned_to=c.assigned_to,
                        assigned_name=c.assigned_name
                    )
                    db_sql.session.add(new_contact)
                    db_sql.session.flush()
                
                # Update messages
                Message.query.filter_by(contact_id=old_id).update({
                    "contact_id": new_id,
                    "instance": "corpal"
                })
                
                db_sql.session.delete(c)
                updated_contacts += 1
                
        db_sql.session.commit()
        return jsonify({'success': True, 'updated': updated_contacts})
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/chat/<path:contact_id>', methods=['DELETE'])
@auth_required
def api_delete_chat(contact_id):
    # Opcional: verificar se o usuário é admin. Por enquanto vou deixar o gestor e admin apagarem.
    try:
        contact = Contact.query.filter_by(id=contact_id).first()
        if not contact:
            return jsonify({'error': 'Contato não encontrado'}), 404
        
        # Deletar todas as mensagens vinculadas a esse contato
        Message.query.filter_by(contact_id=contact_id).delete()
        
        # Deletar o contato
        db_sql.session.delete(contact)
        db_sql.session.commit()
        
        return jsonify({'success': True}), 200
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e), 'success': False}), 500

@app.route('/api/chat/transfer', methods=['POST'])
@auth_required
def chat_transfer():
    data = request.json
    contact_id = data.get('contact_id')
    filial = data.get('filial')
    setor = data.get('setor')
    
    if not contact_id or not filial or not setor:
        return jsonify({'error': 'Parâmetros inválidos (contact_id, filial, setor são obrigatórios)'}), 400
        
    contact = Contact.query.get(contact_id)
    if not contact:
        return jsonify({'error': 'Contato não encontrado'}), 404
        
    user = User.query.get(request.user['id'])
    
    # Bloquear transferência se chat está sendo atendido por outra pessoa
    if contact.assigned_to and contact.assigned_to != user.id:
        return jsonify({'error': 'Este chat já está sendo atendido por outra pessoa'}), 403
        
    # Regra removida: Todos os usuários podem transferir para qualquer filial e setor
    # if user.role == 'user':
    #     if not user.filial or user.filial != filial:
    #         return jsonify({'error': 'Você só pode transferir para a sua própria filial'}), 403
        
    # Atualiza as tags para refletir o novo setor de destino
    # MANTÉM a tag do setor de origem (do usuário que fez a transferência)
    # para que o setor de origem ainda possa visualizar/acompanhar o chat
    tag_destino = f"{filial}:{setor}"
    tag_origem = None
    if user.filial and user.setor:
        tag_origem = f"{user.filial}:{user.setor}"
    
    current_tags = list(contact.tags or [])
    # Remove apenas tags de atendente e BOT; mantém outras tags filial:setor existentes
    new_tags = [
        t for t in current_tags
        if isinstance(t, str)
        and not t.strip().lower().startswith('atendente:')
        and t.strip().upper() != 'BOT'
    ]
    # Adiciona a tag de destino se ainda não existir
    if tag_destino not in new_tags:
        new_tags.append(tag_destino)
    # Garante que a tag de origem do usuário que transferiu está presente (para leitura)
    if tag_origem and tag_origem not in new_tags:
        new_tags.append(tag_origem)
    
    contact.tags = new_tags
    flag_modified(contact, 'tags')
        
    # Liberar o atendimento (remover assigned_to)
    if contact.assigned_to:
        contact.assigned_to = None
        contact.assigned_name = None
    
    db_sql.session.commit()
    
    # Registra no SLA que o chat entrou na fila de transferência
    track_sla_event(contact.phone, filial=filial, setor=setor, event_type='QUEUE_ENTER')

    # Ao transferir: reinicia o timer de espera (contagem começa do zero a partir da transferência)
    try:
        agora_tr_iso = get_now().isoformat()
        atend_chat_tr = AtendimentoChat.query.filter_by(numero=contact.phone).first()
        if atend_chat_tr:
            atend_chat_tr.status = 'atendente'      # mantém 'atendente' para o monitor rastrear
            atend_chat_tr.atendente = None           # sem atendente fixo (aguardando novo)
            atend_chat_tr.atendente_desde = agora_tr_iso  # timer reinicia agora
            atend_chat_tr.alerta_20min_enviado = False
            atend_chat_tr.alerta_40min_enviado = False
            atend_chat_tr.ultimo_setor = setor       # registra setor de destino
            db_sql.session.commit()
            print(f"[TRANSFER] Timer de espera reiniciado para {contact.phone} → {filial}/{setor}")
    except Exception as e_tr:
        db_sql.session.rollback()
        print(f"Erro ao resetar alertas na transferência: {e_tr}")

    n8n_webhook_url = "https://n8n-n8n.ioms5g.easypanel.host/webhook/chamar"
    
    payload = {
        "numero": contact.phone,
        "filial": filial,
        "setor": setor
    }
    
    # Dispara o webhook em background ou após o commit, para que o N8N leia o banco já atualizado
    try:
        res = requests.post(n8n_webhook_url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"Erro ao disparar webhook de transferência n8n: {e}")
        return jsonify({'error': 'Erro ao comunicar com n8n (mas chat foi transferido)'}), 500
    
    # Emite evento para os clientes atualizarem
    socketio.emit('chat_tags_updated', {
        'id': contact.id,
        'tags': list(contact.tags or [])
    }, room=f"instance_{contact.instance or 'unknown'}")
    
    socketio.emit('chat_tags_updated', {
        'id': contact.id,
        'tags': list(contact.tags or [])
    }, room='admin')
    
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': list(contact.tags or [])
    }, room=f"instance_{contact.instance or 'unknown'}")
    
    socketio.emit('chat_assignment', {
        'contact_id': contact.id,
        'assigned_to': None,
        'assigned_name': None,
        'tags': list(contact.tags or [])
    }, room='admin')
    
    return jsonify({'success': True})

@app.route('/api/media/<media_type>')
def stream_media(media_type):
    """Proxy de midia: busca o arquivo da WAHA e retorna como stream.
    Aceita token via query param porque a tag media pode nao enviar headers customizados."""
    token = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', ''))
    if not token:
        return jsonify({'error': 'Token obrigatorio'}), 401
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return jsonify({'error': 'Token invalido'}), 401

    instance = request.args.get('instance')
    msg_id = request.args.get('msg_id')
    if not instance or not msg_id:
        return jsonify({'error': 'instance e msg_id sao obrigatorios'}), 400
    try:
        # ── 1. Verificar no MinIO (prioridade máxima) ──────────────────────────
        _ext_map = {
            'image': ['.jpeg', '.jpg', '.png', '.webp', ''],
            'video': ['.mp4', '.webm_video', ''],
            'audio': ['.oga', '.ogg', '.webm', '.mp4', '.mp3', '.m4a', '.aac', '.wav', ''],
            'document': ['.pdf', '.doc', '.docx', '.bin', '']
        }
        _exts = _ext_map.get(media_type, [''])
        minio_key_found = None
        
        short_id = msg_id.split('_')[-1] if '_' in msg_id else msg_id
        for ext in _exts:
            _candidate = f"{short_id}{ext}"
            if minio_exists(_candidate):
                minio_key_found = _candidate
                break
        
        if not minio_key_found and msg_id != short_id:
            for ext in _exts:
                _candidate = f"{msg_id}{ext}"
                if minio_exists(_candidate):
                    minio_key_found = _candidate
                    break
                    
        if minio_key_found:
            presigned = minio_presigned_url(minio_key_found, expiry=3600)
            if presigned:
                print(f"[{media_type.capitalize()} Proxy] Redirecionando para MinIO: {minio_key_found}")
                from flask import redirect
                return redirect(presigned)

        print(f"[{media_type.capitalize()} Proxy] Arquivo {msg_id} não encontrado no MinIO. (Fallback local/WAHA desativado)")
        return jsonify({'error': 'Arquivo não encontrado no MinIO'}), 404

    except Exception as e:
        print(f"[{media_type.capitalize()} Proxy] ERRO GERAL: {e}")
        return jsonify({'error': str(e)}), 500

# ─── Admin: Media Browser ────────────────────────────────────────────────────

@app.route('/api/admin/media', methods=['GET'])
@auth_required
def admin_list_media():
    """Lista arquivos na pasta data/media/ com paginação e filtros. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    import glob, mimetypes as _mt
    media_dir = os.path.join(DATA_DIR, 'media')
    if not os.path.exists(media_dir):
        return jsonify({'files': [], 'total': 0, 'total_size': 0, 'page': 1, 'per_page': 50})

    filter_type = request.args.get('type', 'all')  # all, audio, image, video, document
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(100, max(10, int(request.args.get('per_page', 50))))
    search = request.args.get('search', '').strip().lower()

    all_files = []
    for entry in os.scandir(media_dir):
        if not entry.is_file():
            continue
        name = entry.name
        stat = entry.stat()
        _, ext = os.path.splitext(name)
        ext_lower = ext.lower()

        # Determinar tipo
        if ext_lower in ('.oga', '.ogg', '.webm', '.opus', '.mp3', '.wav', '.aac', '.m4a'):
            ftype = 'audio'
        elif ext_lower in ('.jpeg', '.jpg', '.png', '.webp', '.gif', '.bmp', '.svg'):
            ftype = 'image'
        elif ext_lower in ('.mp4', '.avi', '.mov', '.mkv', '.webm_video', '.3gp'):
            ftype = 'video'
        elif ext_lower == '.webm':
            ftype = 'audio'  # webm no contexto do WhatsApp é áudio
        elif ext_lower in ('', ):
            # Sem extensão — tentar detectar pelos bytes
            ftype = 'unknown'
        else:
            ftype = 'document'

        if filter_type != 'all' and ftype != filter_type:
            continue
        if search and search not in name.lower():
            continue

        all_files.append({
            'name': name,
            'size': stat.st_size,
            'modified': stat.st_mtime,
            'type': ftype,
            'ext': ext_lower or '(sem ext)'
        })

    # Ordenar por data de modificação (mais recente primeiro)
    all_files.sort(key=lambda f: f['modified'], reverse=True)

    total = len(all_files)
    total_size = sum(f['size'] for f in all_files)
    start = (page - 1) * per_page
    page_files = all_files[start:start + per_page]

    # Contar por tipo (para stats)
    type_counts = {}
    for f in all_files:
        type_counts[f['type']] = type_counts.get(f['type'], 0) + 1

    return jsonify({
        'files': page_files,
        'total': total,
        'total_size': total_size,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page,
        'type_counts': type_counts
    })

@app.route('/api/admin/media/serve/<path:filename>')
def admin_serve_media(filename):
    """Serve um arquivo de mídia diretamente. Apenas admin.
    Aceita token via query param porque tags <audio>/<img>/<video> não enviam headers customizados."""
    token = request.args.get('token') or (request.headers.get('Authorization', '').replace('Bearer ', ''))
    if not token:
        return jsonify({'error': 'Token obrigatório'}), 401
    try:
        user_data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return jsonify({'error': 'Token inválido'}), 401
    if user_data.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    import mimetypes as _mt
    media_dir = os.path.join(DATA_DIR, 'media')
    filepath = os.path.join(media_dir, filename)

    # Segurança: impedir path traversal
    if not os.path.abspath(filepath).startswith(os.path.abspath(media_dir)):
        return jsonify({'error': 'Caminho inválido'}), 400

    if not os.path.exists(filepath):
        return jsonify({'error': 'Arquivo não encontrado'}), 404

    with open(filepath, 'rb') as f:
        file_data = f.read()

    # Detectar content_type por magic bytes primeiro, fallback para extensão
    _, ext = os.path.splitext(filename)
    ext_lower = ext.lower()

    content_type = 'application/octet-stream'
    if file_data[:4] == b'OggS':
        content_type = 'audio/ogg'
    elif file_data[:4] == b'\x1aE\xdf\xa3':  # WebM/Matroska
        content_type = 'audio/webm'
    elif file_data[:3] == b'ID3' or file_data[:2] == b'\xff\xfb':
        content_type = 'audio/mpeg'
    elif file_data[:8] == b'\x89PNG\r\n\x1a\n':
        content_type = 'image/png'
    elif file_data[:2] == b'\xff\xd8':
        content_type = 'image/jpeg'
    elif file_data[:4] == b'RIFF' and file_data[8:12] == b'WEBP':
        content_type = 'image/webp'
    elif file_data[:3] == b'GIF':
        content_type = 'image/gif'
    elif file_data[4:8] == b'ftyp':  # MP4/M4A
        # Distinguir vídeo de áudio M4A
        ftyp_brand = file_data[8:12]
        if ftyp_brand in (b'M4A ', b'M4B '):
            content_type = 'audio/mp4'
        else:
            content_type = 'video/mp4'
    elif file_data[:4] == b'%PDF':
        content_type = 'application/pdf'
    else:
        # Fallback para extensão
        if ext_lower in ('.oga', '.ogg', '.opus'):
            content_type = 'audio/ogg'
        elif ext_lower == '.webm':
            content_type = 'audio/webm'
        elif ext_lower in ('.mp3',):
            content_type = 'audio/mpeg'
        elif ext_lower in ('.jpeg', '.jpg'):
            content_type = 'image/jpeg'
        elif ext_lower == '.png':
            content_type = 'image/png'
        elif ext_lower == '.webp':
            content_type = 'image/webp'
        elif ext_lower == '.mp4':
            content_type = 'video/mp4'
        elif ext_lower == '.pdf':
            content_type = 'application/pdf'
        else:
            guess, _ = _mt.guess_type(filepath)
            if guess:
                content_type = guess

    return Response(file_data, mimetype=content_type, headers={
        'Content-Disposition': 'inline',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=3600'
    })

@app.route('/api/admin/media/delete', methods=['POST'])
@auth_required
def admin_delete_media():
    """Deleta arquivos de mídia. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    data = request.json
    filenames = data.get('filenames', [])
    if not filenames:
        return jsonify({'error': 'Nenhum arquivo especificado'}), 400

    media_dir = os.path.join(DATA_DIR, 'media')
    deleted = 0
    for fn in filenames:
        fp = os.path.join(media_dir, fn)
        if os.path.abspath(fp).startswith(os.path.abspath(media_dir)) and os.path.exists(fp):
            try:
                os.remove(fp)
                deleted += 1
            except Exception:
                pass

    return jsonify({'deleted': deleted})

@app.route('/api/admin/media/stats', methods=['GET'])
@auth_required
def admin_media_stats():
    """Retorna estatísticas da pasta de mídia. Apenas admin."""
    if request.user.get('role') != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403

    media_dir = os.path.join(DATA_DIR, 'media')
    if not os.path.exists(media_dir):
        return jsonify({'total_files': 0, 'total_size': 0, 'types': {}})

    total_files = 0
    total_size = 0
    types = {}

    for entry in os.scandir(media_dir):
        if not entry.is_file():
            continue
        stat = entry.stat()
        total_files += 1
        total_size += stat.st_size
        _, ext = os.path.splitext(entry.name)
        ext = ext.lower() or '(sem ext)'
        types[ext] = types.get(ext, 0) + 1

    return jsonify({
        'total_files': total_files,
        'total_size': total_size,
        'types': types
    })

@app.route('/api/debug/test-alerta-espera', methods=['POST'])
def test_alerta_espera():
    """Rota de teste: simula um cliente esperando ha X minutos.
    Body JSON: { "numero": "5535999...", "minutos": 25, "atendente": "Teste" }
    Isso forca o monitor a disparar na proxima varredura (ate 60s).
    """
    data = request.json or {}
    numero = data.get('numero', '5500000000000')
    minutos = int(data.get('minutos', 21))
    atendente_nome = data.get('atendente', 'Atendente Teste')

    # Calcula o timestamp simulado (agora - X minutos)
    desde = get_now() - datetime.timedelta(minutes=minutos)
    desde_iso = desde.isoformat()

    try:
        reg = AtendimentoChat.query.filter_by(numero=numero).first()
        if reg:
            reg.status = 'atendente'
            reg.atendente = atendente_nome
            reg.atendente_desde = desde_iso
            reg.alerta_20min_enviado = False
            reg.alerta_40min_enviado = False
            reg.registro_time_chat = desde_iso
        else:
            reg = AtendimentoChat(
                numero=numero,
                status='atendente',
                atendente=atendente_nome,
                atendente_desde=desde_iso,
                registro_time_chat=desde_iso,
                alerta_20min_enviado=False,
                alerta_40min_enviado=False
            )
            db_sql.session.add(reg)
        db_sql.session.commit()
        return jsonify({
            'ok': True,
            'numero': numero,
            'atendente': atendente_nome,
            'minutos_simulados': minutos,
            'atendente_desde': desde_iso,
            'aviso': 'O monitor verifica a cada 60s. Aguarde ate 1 minuto e verifique os webhooks no N8N.'
        }), 200
    except Exception as e:
        db_sql.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/debug/last-webhook', methods=['GET', 'POST'])
def debug_webhook():
    """Dev-only: POST salva payload, GET retorna o ultimo."""
    debug_file = os.path.join(DATA_DIR, 'last_webhook.json')
    if request.method == 'POST':
        with open(debug_file, 'w', encoding='utf-8') as f:
            json.dump(request.json, f, indent=2, ensure_ascii=False)
        return 'OK', 200
    if os.path.exists(debug_file):
        with open(debug_file, 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype='application/json')
    return jsonify({})

@app.route('/api/reports/sla', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_sla():
    """
    Retorna os dados de SLA (Tempo de Fila, 1ª Resposta, Tempo de Resposta Contínuo).
    Pode filtrar por data (start_date, end_date).
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = SlaHistory.query
    
    if start_date:
        query = query.filter(SlaHistory.criado_em >= start_date)
    if end_date:
        query = query.filter(SlaHistory.criado_em <= end_date + 'T23:59:59')
        
    records = query.order_by(SlaHistory.id.desc()).all()
    
    data = []
    for r in records:
        media_continua = 0
        if r.qtd_respostas_atendente and r.qtd_respostas_atendente > 0:
            media_continua = r.soma_tempo_resposta_segundos / r.qtd_respostas_atendente
            
        data.append({
            'id': r.id,
            'numero': r.numero,
            'filial': r.filial,
            'setor': r.setor,
            'atendente': r.atendente,
            'entrou_na_fila_em': r.entrou_na_fila_em,
            'assumido_em': r.assumido_em,
            'finalizado_em': r.finalizado_em,
            'tempo_na_fila_segundos': r.tempo_na_fila_segundos,
            'tempo_primeira_resposta_segundos': r.tempo_primeira_resposta_segundos,
            'qtd_respostas_atendente': r.qtd_respostas_atendente,
            'soma_tempo_resposta_segundos': r.soma_tempo_resposta_segundos,
            'media_tempo_resposta_segundos': media_continua,
            'criado_em': r.criado_em
        })
        
    return jsonify({'success': True, 'data': data}), 200

@app.route('/')
def index_page():
    return send_from_directory(ROOT_DIR, 'index.html')

@app.route('/<path:path>')
def serve_frontend(path):
    if path in ('index', 'dashboard', 'admin', 'reports'):
        path = path + '.html'
    if path in ('index.html', 'dashboard.html', 'admin.html', 'reports.html', 'entregador', 'entregador.html'):
        return send_from_directory(ROOT_DIR, 'entregador.html') if path == 'entregador' else send_from_directory(ROOT_DIR, path)
    if path.startswith('css/') or path.startswith('js/') or path.startswith('img/'):
        return send_from_directory(ROOT_DIR, path)
    if path == 'manifest.json':
        return send_from_directory(ROOT_DIR, path, mimetype='application/manifest+json')
    if path == 'sw.js':
        return send_from_directory(ROOT_DIR, path, mimetype='application/javascript')
    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.svg', '.ico', '.webp')):
        return send_from_directory(ROOT_DIR, path)
    return jsonify({'error': 'Not found'}), 404

@socketio.on('connect')
def test_connect():
    print('>>> Cliente conectado ao SocketIO')
    emit('server_boot', {'boot_id': SERVER_BOOT_ID})

@socketio.on('join_company')
def on_join(company_id):
    join_room(company_id)
    print(f'Client joined room: {company_id}')

@socketio.on('join_instances')
def on_join_instances(data):
    """Usuário entra nas rooms das instâncias que tem acesso."""
    instances = data.get('instances', [])
    role = data.get('role', 'user')
    
    for inst_name in instances:
        room_name = f'instance_{inst_name}'
        join_room(room_name)
        print(f'Client joined instance room: {room_name}')
    
    if role == 'admin':
        join_room('admin')
        print('Client joined admin room')


# --- PORTED REPORTS ---
@app.route('/api/reports/ranking', methods=['GET'])
@auth_required
@admin_required
def report_ranking():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        query = Message.query
        if start_date:
            try:
                start_ts = int(datetime.datetime.strptime(start_date, '%Y-%m-%d').timestamp())
                query = query.filter(Message.timestamp >= start_ts)
            except Exception:
                pass
        if end_date:
            try:
                end_ts = int(datetime.datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S').timestamp())
                query = query.filter(Message.timestamp <= end_ts)
            except Exception:
                pass
            
        messages = query.order_by(Message.contact_id, Message.timestamp).all()
        
        attendant_stats = {} 
        last_in_time = None
        current_contact = None
        
        for msg in messages:
            if current_contact != msg.contact_id:
                current_contact = msg.contact_id
                last_in_time = None
                
            if msg.type == 'in':
                last_in_time = msg.timestamp
            elif msg.type == 'out':
                if msg.sender_id:
                    if msg.sender_id not in attendant_stats:
                        attendant_stats[msg.sender_id] = {'total_msgs': 0, 'conversations': {}}
                    
                    attendant_stats[msg.sender_id]['total_msgs'] += 1
                    
                    if last_in_time is not None:
                        resp_time = msg.timestamp - last_in_time
                        if resp_time < 0: resp_time = 0
                        
                        if msg.contact_id not in attendant_stats[msg.sender_id]['conversations']:
                            attendant_stats[msg.sender_id]['conversations'][msg.contact_id] = []
                        
                        attendant_stats[msg.sender_id]['conversations'][msg.contact_id].append(resp_time)
                    
                last_in_time = None 
                
        ranking = []
        users = {u.id: u for u in User.query.all()}
        for uid, stats in attendant_stats.items():
            user = users.get(uid)
            if user and stats['total_msgs'] > 0:
                conv_averages = []
                for contact_id, times in stats['conversations'].items():
                    if len(times) > 0:
                        conv_avg = sum(times) / len(times)
                        conv_averages.append(conv_avg)
                
                if len(conv_averages) > 0:
                    final_avg_time = sum(conv_averages) / len(conv_averages)
                else:
                    final_avg_time = 0
                
                ranking.append({
                    'id': user.id,
                    'name': user.name,
                    'email': user.email,
                    'avg_time': final_avg_time,
                    'count': stats['total_msgs']
                })
                
        ranking.sort(key=lambda x: (-x['count'], x['avg_time']))
        return jsonify({'success': True, 'data': ranking}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/motivos-geral', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_motivos_geral():
    """Retorna contagem geral de motivos de finalização (para gráfico de pizza)."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        filters = ""
        params = {}
        if start_date:
            filters += " AND (criado_em IS NULL OR criado_em >= :start_date)"
            params['start_date'] = start_date
        if end_date:
            filters += " AND (criado_em IS NULL OR criado_em <= :end_date)"
            params['end_date'] = end_date + ' 23:59:59'

        sql = db_sql.text(f"""
            SELECT motivo, COUNT(*) as qtd
            FROM motivo_finalizacao
            WHERE 1=1 {filters}
            GROUP BY motivo
            ORDER BY qtd DESC
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        vendas = 0
        orcamentos = 0
        outros = 0
        for row in rows:
            motivo = row[0]
            qtd = row[1]
            if motivo == 'Venda':
                vendas += qtd
            elif motivo == 'Orçamento':
                orcamentos += qtd
            else:
                outros += qtd

        total = vendas + orcamentos + outros
        return jsonify({
            'success': True,
            'data': {
                'vendas': vendas,
                'orcamentos': orcamentos,
                'outros': outros,
                'total': total
            }
        }), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/motivos-individuais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_motivos_individuais():
    """Retorna lista paginada de chats individuais com motivo de finalização."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        offset = (page - 1) * per_page

        filters = ""
        params = {}
        if start_date:
            filters += " AND (criado_em IS NULL OR criado_em >= :start_date)"
            params['start_date'] = start_date
        if end_date:
            filters += " AND (criado_em IS NULL OR criado_em <= :end_date)"
            params['end_date'] = end_date + ' 23:59:59'

        # Total count
        sql_count = db_sql.text(f"""
            SELECT COUNT(*) FROM motivo_finalizacao WHERE 1=1 {filters}
        """)
        total = db_sql.session.execute(sql_count, params).scalar() or 0

        params['limit'] = per_page
        params['offset'] = offset
        sql = db_sql.text(f"""
            SELECT m.id, m.contact_id, m.numero_cliente, m.atendente,
                   m.motivo, m.detalhes, m.criado_em
            FROM motivo_finalizacao m
            WHERE 1=1 {filters}
            ORDER BY m.criado_em DESC
            LIMIT :limit OFFSET :offset
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        items = []
        for row in rows:
            items.append({
                'id': row[0],
                'contact_id': row[1],
                'numero_cliente': row[2] or '',
                'atendente': row[3] or '',
                'motivo': row[4] or '',
                'detalhes': row[5] or '',
                'criado_em': row[6].strftime('%d/%m/%Y %H:%M') if row[6] else ''
            })

        return jsonify({
            'success': True,
            'data': items,
            'pagination': {
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': (total + per_page - 1) // per_page
            }
        }), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500

@app.route('/api/reports/motivos-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_motivos_atendentes():
    """Retorna agrupamento de motivos por Atendente."""
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        filters = ""
        params = {}
        if start_date:
            filters += " AND (m.criado_em IS NULL OR m.criado_em >= :start_date)"
            params['start_date'] = start_date
        if end_date:
            filters += " AND (m.criado_em IS NULL OR m.criado_em <= :end_date)"
            params['end_date'] = end_date + ' 23:59:59'

        sql = db_sql.text(f"""
            SELECT COALESCE(m.atendente, 'Sem Atendente') as atendente, 
                   COALESCE(u.filial, 'Sem Filial') as filial, 
                   COALESCE(u.setor, 'Sem Setor') as setor, 
                   m.motivo, 
                   COUNT(*) as qtd
            FROM motivo_finalizacao m
            LEFT JOIN "user" u ON u.name = m.atendente
            WHERE 1=1 {filters}
            GROUP BY m.atendente, u.filial, u.setor, m.motivo
            ORDER BY m.atendente, m.motivo
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        atendentes = {}
        for row in rows:
            atendente = row[0]
            filial = row[1]
            setor = row[2]
            motivo = row[3]
            qtd = row[4]

            if atendente not in atendentes:
                atendentes[atendente] = {
                    'atendente': atendente,
                    'filial': filial,
                    'setor': setor,
                    'vendas': 0,
                    'orcamentos': 0,
                    'outros': 0,
                    'total': 0
                }
            
            a = atendentes[atendente]
            a['total'] += qtd
            if motivo == 'Venda':
                a['vendas'] += qtd
            elif motivo == 'Orçamento':
                a['orcamentos'] += qtd
            else:
                a['outros'] += qtd

        result = list(atendentes.values())
        result.sort(key=lambda x: x['total'], reverse=True)

        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


def _segundos_espera_sql():
    return "EXTRACT(EPOCH FROM (atendido - inicio))"

def _segundos_chat_sql():
    return "EXTRACT(EPOCH FROM (finalizado - atendido))"


@app.route('/api/reports/tempo-espera-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_atendentes():
    """Ranking de atendentes por eficiência de tempo de espera."""
    try:
        import math
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        filters = ""
        params  = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'
        sql = db_sql.text(f"""
            SELECT nome_atendente, setor_filial, COUNT(*) as total_atendidos,
                   AVG({_segundos_espera_sql()}) as avg_espera_seg,
                   AVG(CASE WHEN finalizado IS NOT NULL THEN {_segundos_chat_sql()} END) as avg_chat_seg,
                   MIN({_segundos_espera_sql()}) as min_espera_seg,
                   MAX({_segundos_espera_sql()}) as max_espera_seg
            FROM tempo_espera WHERE 1=1 {filters}
            GROUP BY nome_atendente, setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()
        result = []
        for row in rows:
            nome       = row[0] or '-'
            sf         = row[1] or '-'
            total      = int(row[2] or 0)
            avg_espera = float(row[3] or 0)
            avg_chat   = float(row[4] or 0)
            total_med  = avg_espera + avg_chat
            score      = math.log(total + 1) * 10000 / (total_med + 1) if total > 0 else 0
            partes     = sf.split(':', 1) if ':' in sf else [sf, '-']
            setor      = partes[0].strip()
            filial     = partes[1].strip() if len(partes) > 1 else '-'
            result.append({
                'atendente': nome, 'setor_filial': sf, 'setor': setor, 'filial': filial,
                'total_atendidos': total,
                'avg_espera_seg': round(avg_espera, 0),
                'avg_chat_seg':   round(avg_chat, 0),
                'avg_total_seg':  round(total_med, 0),
                'min_espera_seg': round(float(row[5] or 0), 0),
                'max_espera_seg': round(float(row[6] or 0), 0),
                'score': round(score, 1)
            })
        result.sort(key=lambda x: -x['score'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/tempo-espera-chats', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_chats():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 50))
        offset = (page - 1) * limit

        filters = ""
        params = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'

        count_sql = db_sql.text(f"SELECT COUNT(*) FROM tempo_espera WHERE 1=1 {filters}")
        total_items = db_sql.session.execute(count_sql, params).scalar()

        sql = db_sql.text(f"""
            SELECT numero_cliente, nome_atendente, inicio, atendido, finalizado
            FROM tempo_espera
            WHERE 1=1 {filters}
            ORDER BY inicio DESC
            LIMIT :limit OFFSET :offset
        """)
        params['limit'] = limit
        params['offset'] = offset

        rows = db_sql.session.execute(sql, params).fetchall()

        def fmt(secs):
            s = int(secs)
            if s < 60: return f"{s}s"
            m = s // 60
            sec = s % 60
            if m < 60: return f"{m}m {sec}s"
            h = m // 60
            m = m % 60
            return f"{h}h {m}m"

        data = []
        import datetime as dt
        for r in rows:
            cliente = r[0]
            atendente = r[1] or "Sem Atendente"
            inicio = r[2]
            atendido = r[3]
            finalizado = r[4]

            if isinstance(inicio, str):
                inicio = dt.datetime.strptime(inicio, '%Y-%m-%d %H:%M:%S.%f') if '.' in inicio else dt.datetime.strptime(inicio, '%Y-%m-%d %H:%M:%S')
            if atendido and isinstance(atendido, str):
                atendido = dt.datetime.strptime(atendido, '%Y-%m-%d %H:%M:%S.%f') if '.' in atendido else dt.datetime.strptime(atendido, '%Y-%m-%d %H:%M:%S')
            if finalizado and isinstance(finalizado, str):
                finalizado = dt.datetime.strptime(finalizado, '%Y-%m-%d %H:%M:%S.%f') if '.' in finalizado else dt.datetime.strptime(finalizado, '%Y-%m-%d %H:%M:%S')

            str_espera = "Aguardando"
            if atendido:
                diff_espera = (atendido - inicio).total_seconds()
                str_espera = fmt(diff_espera)

            str_conversa = "-"
            if atendido:
                if finalizado:
                    diff_conv = (finalizado - atendido).total_seconds()
                    str_conversa = fmt(diff_conv)
                else:
                    str_conversa = "Em atendimento"

            data.append({
                'cliente': cliente,
                'atendente': atendente,
                'data_hora': inicio.strftime('%d/%m/%Y %H:%M'),
                'espera': str_espera,
                'conversa': str_conversa
            })

        return jsonify({'success': True, 'data': data, 'total': total_items, 'pages': (total_items + limit - 1) // limit})
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/tempo-espera-filiais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_tempo_espera_filiais():
    """Ranking de filiais/setores por eficiência de tempo de espera."""
    try:
        import math
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        filters = ""
        params  = {}
        if start_date:
            filters += " AND inicio >= :start_date"
            params['start_date'] = start_date
        if end_date:
            filters += " AND inicio <= :end_date"
            params['end_date'] = end_date + ' 23:59:59'
        sql = db_sql.text(f"""
            SELECT setor_filial, COUNT(*) as total_atendidos,
                   AVG({_segundos_espera_sql()}) as avg_espera_seg,
                   AVG(CASE WHEN finalizado IS NOT NULL THEN {_segundos_chat_sql()} END) as avg_chat_seg
            FROM tempo_espera WHERE 1=1 {filters}
            GROUP BY setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()
        filiais = {}
        for row in rows:
            sf         = row[0] or '-'
            total      = int(row[1] or 0)
            avg_espera = float(row[2] or 0)
            avg_chat   = float(row[3] or 0)
            total_med  = avg_espera + avg_chat
            score      = math.log(total + 1) * 10000 / (total_med + 1) if total > 0 else 0
            partes     = sf.split(':', 1) if ':' in sf else [sf, '-']
            setor      = partes[0].strip()
            filial     = partes[1].strip() if len(partes) > 1 else '-'
            if filial not in filiais:
                filiais[filial] = []
            filiais[filial].append({
                'setor': setor, 'total_atendidos': total,
                'avg_espera_seg': round(avg_espera, 0),
                'avg_chat_seg':   round(avg_chat, 0),
                'avg_total_seg':  round(total_med, 0),
                'score': round(score, 1)
            })
        result = []
        for filial, setores in filiais.items():
            setores.sort(key=lambda x: -x['score'])
            total_f    = sum(s['total_atendidos'] for s in setores)
            avg_esp_f  = sum(s['avg_espera_seg'] * s['total_atendidos'] for s in setores) / total_f if total_f else 0
            avg_chat_f = sum((s['avg_chat_seg'] or 0) * s['total_atendidos'] for s in setores) / total_f if total_f else 0
            score_f    = math.log(total_f + 1) * 10000 / (avg_esp_f + avg_chat_f + 1) if total_f > 0 else 0
            result.append({
                'filial': filial, 'total_atendidos': total_f,
                'avg_espera_seg': round(avg_esp_f, 0),
                'avg_chat_seg':   round(avg_chat_f, 0),
                'score': round(score_f, 1),
                'setores': setores
            })
        result.sort(key=lambda x: -x['score'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/volume-chats-filiais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_volume_chats_filiais():
    """Volume de chats criados, fechados e abertos por filial/setor (categorizados por tags)."""
    try:
        filiais_objs = Filial.query.all()
        valid_filiais = {f.name.lower().strip(): f.name.strip() for f in filiais_objs}
        
        def resolve_sf(sf_str):
            if not sf_str or ':' not in sf_str:
                return None, None
            partes = sf_str.split(':', 1)
            p0 = partes[0].strip()
            p1 = partes[1].strip()
            if not p0 or not p1 or p0 == '-' or p1 == '-' or p0.lower() == 'null' or p1.lower() == 'null':
                return None, None
            
            if p0.lower() in valid_filiais:
                return valid_filiais[p0.lower()], p1  # p0 = filial, p1 = setor
            elif p1.lower() in valid_filiais:
                return valid_filiais[p1.lower()], p0  # p1 = filial, p0 = setor
            else:
                return p0, p1  # Default assume p0 = filial

        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        
        params = {}
        date_filters = ""
        if start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date + ' 23:59:59'
            date_filters = """
                WHERE (inicio >= :start_date AND inicio <= :end_date)
                   OR (finalizado >= :start_date AND finalizado <= :end_date)
                   OR (finalizado IS NULL)
            """
        else:
            date_filters = "WHERE 1=1"

        sql = db_sql.text(f"""
            SELECT setor_filial,
                   SUM(CASE WHEN inicio >= :start_date AND inicio <= :end_date THEN 1 ELSE 0 END) as criados,
                   SUM(CASE WHEN finalizado >= :start_date AND finalizado <= :end_date THEN 1 ELSE 0 END) as fechados
            FROM tempo_espera
            {date_filters}
            GROUP BY setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        filiais = {}
        for row in rows:
            sf       = row[0] or '-'
            if not sf or sf == '-': continue
            criados  = int(row[1] or 0)
            fechados = int(row[2] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais:
                filiais[filial] = {}
            if setor not in filiais[filial]:
                filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            
            filiais[filial][setor]['criados'] += criados
            filiais[filial][setor]['fechados'] += fechados

        # Fila de ESPERA (tempo_espera sem atendente)
        sql_espera = db_sql.text("""
            SELECT setor_filial, COUNT(*) as qtd
            FROM tempo_espera
            WHERE finalizado IS NULL AND atendido IS NULL
            GROUP BY setor_filial
        """)
        espera_rows = db_sql.session.execute(sql_espera).fetchall()
        for row in espera_rows:
            sf = row[0] or '-'
            if not sf or sf == '-': continue
            qtd = int(row[1] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais: filiais[filial] = {}
            if setor not in filiais[filial]: filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            filiais[filial][setor]['espera'] += qtd

        # Fila de ATENDIMENTO (atendimentos_chat com status='atendente')
        sql_atend = db_sql.text("""
            SELECT COALESCE(u.setor, '-') || ':' || COALESCE(u.filial, '-') as sf, COUNT(*) as qtd
            FROM atendimentos_chat a
            JOIN users u ON u.name = a.atendente
            WHERE a.status = 'atendente'
            GROUP BY u.setor, u.filial
        """)
        atend_rows = db_sql.session.execute(sql_atend).fetchall()
        for row in atend_rows:
            sf = row[0] or '-'
            if not sf or sf == '-': continue
            qtd = int(row[1] or 0)
            
            filial, setor = resolve_sf(sf)
            if not filial or not setor:
                continue
            
            if filial not in filiais: filiais[filial] = {}
            if setor not in filiais[filial]: filiais[filial][setor] = {'criados': 0, 'fechados': 0, 'triagem': 0, 'espera': 0, 'atendimento': 0}
            filiais[filial][setor]['atendimento'] += qtd

        result = []
        for filial, setores_dict in filiais.items():
            setores_list = []
            for setor, stats in setores_dict.items():
                setores_list.append({
                    'setor': setor,
                    'criados': stats['criados'],
                    'fechados': stats['fechados'],
                    'triagem': stats['triagem'],
                    'espera': stats['espera'],
                    'atendimento': stats['atendimento']
                })
            setores_list.sort(key=lambda x: -x['criados'])
            result.append({
                'filial': filial,
                'criados': sum(s['criados'] for s in setores_list),
                'fechados': sum(s['fechados'] for s in setores_list),
                'triagem': sum(s['triagem'] for s in setores_list),
                'espera': sum(s['espera'] for s in setores_list),
                'atendimento': sum(s['atendimento'] for s in setores_list),
                'setores': setores_list
            })
        result.sort(key=lambda x: -x['criados'])
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/volume-chats-atendentes', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_volume_chats_atendentes():
    """Volume de chats criados, fechados e abertos por atendente."""
    try:
        start_date = request.args.get('start_date')
        end_date   = request.args.get('end_date')
        
        params = {}
        date_filters = ""
        if start_date and end_date:
            params['start_date'] = start_date
            params['end_date'] = end_date + ' 23:59:59'
            date_filters = """
                AND ((inicio >= :start_date AND inicio <= :end_date)
                   OR (finalizado >= :start_date AND finalizado <= :end_date)
                   OR (finalizado IS NULL))
            """
        
        sql = db_sql.text(f"""
            SELECT nome_atendente, setor_filial,
                   SUM(CASE WHEN inicio >= :start_date AND inicio <= :end_date THEN 1 ELSE 0 END) as criados,
                   SUM(CASE WHEN finalizado >= :start_date AND finalizado <= :end_date THEN 1 ELSE 0 END) as fechados
            FROM tempo_espera
            WHERE nome_atendente IS NOT NULL AND nome_atendente != '' {date_filters}
            GROUP BY nome_atendente, setor_filial
        """)
        rows = db_sql.session.execute(sql, params).fetchall()

        users = User.query.all()
        email_to_name = {u.email.lower().strip(): u.name.strip() for u in users if u.email and u.name}
        name_to_user = {u.name.lower().strip(): u for u in users if u.name}

        def normalize_atendente_nome(n):
            n_str = str(n).strip()
            if '@' in n_str:
                n_lower = n_str.lower()
                if n_lower in email_to_name:
                    return email_to_name[n_lower]
            return n_str

        atendentes_map = {}
        for row in rows:
            nome    = normalize_atendente_nome(row[0] or '-')
            criados = int(row[2] or 0)
            
            key = nome.lower()
            if key not in atendentes_map:
                atendentes_map[key] = {'nome': nome, 'criados': 0, 'fechados': 0, 'abertos': 0}
            
            atendentes_map[key]['criados'] += criados

        # Atendimentos abertos e fechados usando atendimentos_chat
        sql_abertos = db_sql.text("""
            SELECT atendente, 
                   SUM(CASE WHEN LOWER(status) = 'atendente' THEN 1 ELSE 0 END) as abertos,
                   SUM(CASE WHEN LOWER(status) = 'bot' THEN 1 ELSE 0 END) as fechados
            FROM atendimentos_chat
            WHERE atendente IS NOT NULL AND atendente != ''
            GROUP BY atendente
        """)
        abertos_rows = db_sql.session.execute(sql_abertos).fetchall()
        for row in abertos_rows:
            nome = normalize_atendente_nome(row[0] or '-')
            qtd_abertos  = int(row[1] or 0)
            qtd_fechados = int(row[2] or 0)
            
            key = nome.lower()
            if key not in atendentes_map:
                atendentes_map[key] = {'nome': nome, 'criados': 0, 'fechados': 0, 'abertos': 0}
                
            atendentes_map[key]['abertos']  = qtd_abertos
            atendentes_map[key]['fechados'] = qtd_fechados

        result = []
        for key, data in atendentes_map.items():
            user_obj = name_to_user.get(key)
            filial = user_obj.filial if user_obj and user_obj.filial else '-'
            setor  = user_obj.setor  if user_obj and user_obj.setor  else '-'
            
            result.append({
                'atendente': data['nome'],
                'filial': filial,
                'setor': setor,
                'criados': data['criados'],
                'fechados': data['fechados'],
                'abertos': data['abertos']
            })
            
        result.sort(key=lambda x: (-x['abertos'], -x['criados'], -x['fechados']))
        return jsonify({'success': True, 'data': result}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


# ----------------------


# --- NEW DELIVERY REPORTS ---
@app.route('/api/reports/entregas/rotas', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_entregas_rotas():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        query = Entrega.query.filter(
            Entrega.entregador_id.isnot(None),
            Entrega.numero_rota.isnot(None)
        )
        
        if start_date:
            dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Entrega.criado_em >= dt)
        if end_date:
            dt = datetime.datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
            query = query.filter(Entrega.criado_em <= dt)
            
        entregas = query.order_by(Entrega.numero_rota.desc()).all()
        users = {u.id: u.name for u in User.query.filter_by(role='entregador').all()}
        
        rotas_dict = {}
        for e in entregas:
            nr = e.numero_rota
            if nr not in rotas_dict:
                entregador_nome = users.get(e.entregador_id, f'Entregador #{e.entregador_id}')
                rotas_dict[nr] = {
                    'numero_rota': nr,
                    'entregador_nome': entregador_nome,
                    'saiu_para_entrega_em': e.saiu_para_entrega_em,
                    'entregas': [],
                    'finalizado_em': e.finalizado_em
                }
            # Add sub-deliveries
            rotas_dict[nr]['entregas'].append({
                'id': e.id,
                'nome_peca': e.nome_peca,
                'nome_cliente': e.nome_cliente,
                'localizacao': e.localizacao,
                'status': e.status,
                'justificativa_falha': e.justificativa_falha
            })
            
            if not rotas_dict[nr]['finalizado_em'] and e.finalizado_em:
                rotas_dict[nr]['finalizado_em'] = e.finalizado_em
                
        resultado = list(rotas_dict.values())
        return jsonify({'success': True, 'data': resultado}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/entregas/individuais', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_entregas_individuais():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        query = Entrega.query
        
        if start_date:
            dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Entrega.criado_em >= dt)
        if end_date:
            dt = datetime.datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
            query = query.filter(Entrega.criado_em <= dt)
            
        entregas = query.order_by(Entrega.criado_em.desc()).all()
        users = {u.id: u.name for u in User.query.filter_by(role='entregador').all()}
        
        resultado = []
        for e in entregas:
            entregador_nome = users.get(e.entregador_id, '-') if e.entregador_id else '-'
            resultado.append({
                'id': e.id,
                'nome_peca': e.nome_peca,
                'nome_cliente': e.nome_cliente,
                'localizacao': e.localizacao,
                'entregador_nome': entregador_nome,
                'status': e.status,
                'numero_rota': e.numero_rota,
                'criado_em': e.criado_em.strftime('%d/%m/%Y %H:%M:%S') if e.criado_em else '',
                'justificativa_falha': e.justificativa_falha or ''
            })
            
        return jsonify({'success': True, 'data': resultado}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/reports/entregas/metricas_entregador', methods=['GET'])
@auth_required
@admin_or_gestor_required
def report_entregas_metricas_entregador():
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        
        query = Entrega.query.filter(
            Entrega.entregador_id.isnot(None),
            Entrega.saiu_para_entrega_em.isnot(None)
        )
        
        if start_date:
            dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Entrega.criado_em >= dt)
        if end_date:
            dt = datetime.datetime.strptime(end_date + ' 23:59:59', '%Y-%m-%d %H:%M:%S')
            query = query.filter(Entrega.criado_em <= dt)
            
        entregas = query.all()
        users = {u.id: u.name for u in User.query.filter_by(role='entregador').all()}
        
        entregador_stats = {}
        for e in entregas:
            # Tempo para assumir: saiu_para_entrega_em - criado_em
            # Tempo de entrega: finalizado_em - saiu_para_entrega_em
            try:
                criado = e.criado_em
                saiu = datetime.datetime.strptime(e.saiu_para_entrega_em, '%d/%m/%Y %H:%M')
                
                tempo_assumir = (saiu - criado).total_seconds()
                if tempo_assumir < 0: tempo_assumir = 0
                
                tempo_entrega = None
                if e.finalizado_em:
                    try:
                        finalizado = datetime.datetime.strptime(e.finalizado_em, '%d/%m/%Y %H:%M')
                        tempo_entrega = (finalizado - saiu).total_seconds()
                        if tempo_entrega < 0: tempo_entrega = 0
                    except:
                        pass
                
                eid = e.entregador_id
                if eid not in entregador_stats:
                    entregador_stats[eid] = {
                        'nome': users.get(eid, f'Entregador #{eid}'),
                        'qtd_rotas_distintas': set(),
                        'qtd_entregas': 0,
                        'soma_assumir': 0,
                        'count_assumir': 0,
                        'soma_entrega': 0,
                        'count_entrega': 0
                    }
                
                stats = entregador_stats[eid]
                stats['qtd_entregas'] += 1
                if e.numero_rota:
                    stats['qtd_rotas_distintas'].add(e.numero_rota)
                
                stats['soma_assumir'] += tempo_assumir
                stats['count_assumir'] += 1
                
                if tempo_entrega is not None:
                    stats['soma_entrega'] += tempo_entrega
                    stats['count_entrega'] += 1
            except Exception:
                pass
                
        resultado = []
        for eid, stats in entregador_stats.items():
            avg_assumir = (stats['soma_assumir'] / stats['count_assumir']) if stats['count_assumir'] > 0 else 0
            avg_entrega = (stats['soma_entrega'] / stats['count_entrega']) if stats['count_entrega'] > 0 else 0
            
            resultado.append({
                'entregador_id': eid,
                'entregador_nome': stats['nome'],
                'qtd_entregas': stats['qtd_entregas'],
                'qtd_rotas': len(stats['qtd_rotas_distintas']),
                'avg_tempo_assumir': avg_assumir,
                'avg_tempo_entrega': avg_entrega
            })
            
        resultado.sort(key=lambda x: x['qtd_entregas'], reverse=True)
        return jsonify({'success': True, 'data': resultado}), 200
    except Exception as e:
        import traceback
        return jsonify({'success': False, 'error': str(e), 'trace': traceback.format_exc()}), 500

# ----------------------

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3008))
    print(f"Servidor Python rodando na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
