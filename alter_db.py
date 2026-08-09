from app import app, db_sql
from sqlalchemy import text

with app.app_context():
    try:
        db_sql.session.execute(text('ALTER TABLE entrega ADD COLUMN justificativa_distancia TEXT'))
        db_sql.session.commit()
        print('Coluna justificativa_distancia adicionada com sucesso.')
    except Exception as e:
        print('Erro (pode já existir):', e)
