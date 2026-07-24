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


@app.route('/')
def index_page():
    return send_from_directory(ROOT_DIR, 'index.html')

@app.route('/<path:path>')
def serve_frontend(path):
    if path in ('index.html', 'dashboard.html', 'admin.html', 'reports.html', 'relatorio.html', 'ranking.html', 'entregador', 'entregador.html'):
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

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3008))
    print(f"Servidor Python rodando na porta {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)