import sys
import os
import datetime
import pytz

# Add current dir to path to import app
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app import app, db_sql, Entrega, DriverLocation, ContactRequest, PushSubscription

def adjust_datetime(dt_obj):
    if not dt_obj:
        return dt_obj
    
    # Se já tiver fuso horário configurado, ignoramos
    if getattr(dt_obj, 'tzinfo', None) is not None:
        return dt_obj
        
    # Considerando que o tempo no banco está como UTC puro (ingênuo) e o Brasil é UTC-3
    return dt_obj - datetime.timedelta(hours=3)

def adjust_iso_string(iso_str):
    if not iso_str:
        return iso_str
    
    try:
        # Parse the string
        dt_obj = datetime.datetime.fromisoformat(iso_str)
        if getattr(dt_obj, 'tzinfo', None) is None:
            # Naive UTC string -> converter subtraindo 3h
            adjusted = dt_obj - datetime.timedelta(hours=3)
            # Adicionar o timezone -03:00 (America/Sao_Paulo)
            br_tz = pytz.timezone('America/Sao_Paulo')
            # Localize converte o ingênuo em consciente
            adjusted = br_tz.localize(adjusted)
            return adjusted.isoformat()
        return iso_str
    except ValueError:
        # Tenta fallback para formatos variados se houver erro
        # (Às vezes o fromisoformat do python não suporta o Z no final, etc, dependendo da versão)
        if iso_str.endswith('Z'):
            return adjust_iso_string(iso_str[:-1])
        return iso_str
    except Exception as e:
        print(f"Erro ao converter data {iso_str}: {e}")
        return iso_str

def main():
    with app.app_context():
        print("Ajustando horários das Entregas (criado_em, saiu_para_entrega_em, finalizado_em)...")
        entregas = Entrega.query.all()
        entregas_mod = 0
        for e in entregas:
            e.criado_em = adjust_datetime(e.criado_em)
            e.saiu_para_entrega_em = adjust_iso_string(e.saiu_para_entrega_em)
            e.finalizado_em = adjust_iso_string(e.finalizado_em)
            entregas_mod += 1
        
        print(f"  > {entregas_mod} entregas verificadas/ajustadas.")
        
        print("Ajustando horários de DriverLocation (updated_at)...")
        locations = DriverLocation.query.all()
        loc_mod = 0
        for loc in locations:
            loc.updated_at = adjust_datetime(loc.updated_at)
            loc_mod += 1
            
        print(f"  > {loc_mod} locais de entregadores verificados/ajustados.")

        print("Ajustando horários de ContactRequest (created_at)...")
        requests = ContactRequest.query.all()
        req_mod = 0
        for req in requests:
            req.created_at = adjust_datetime(req.created_at)
            req_mod += 1
            
        print(f"  > {req_mod} requests de contato verificados/ajustados.")

        print("Ajustando horários de PushSubscription (created_at)...")
        subs = PushSubscription.query.all()
        sub_mod = 0
        for sub in subs:
            sub.created_at = adjust_datetime(sub.created_at)
            sub_mod += 1
            
        print(f"  > {sub_mod} push subscriptions verificadas/ajustadas.")

        print("\nSalvando alterações no banco de dados...")
        try:
            db_sql.session.commit()
            print("Sucesso! Todos os horários antigos foram ajustados com sucesso.")
        except Exception as e:
            db_sql.session.rollback()
            print(f"Erro ao salvar no banco: {e}")

if __name__ == '__main__':
    main()
