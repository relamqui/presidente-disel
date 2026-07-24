from app import app, db_sql, Entrega, User
import datetime

with app.app_context():
    all_entregas = Entrega.query.filter(
        Entrega.entregador_id.isnot(None),
        Entrega.saiu_para_entrega_em.isnot(None)
    ).order_by(Entrega.saiu_para_entrega_em).all()
    
    current_nr = 1
    
    ent_por_entregador = {}
    for e in all_entregas:
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
    print(f"Migracao concluida! Total rotas criadas: {current_nr - 1}")
