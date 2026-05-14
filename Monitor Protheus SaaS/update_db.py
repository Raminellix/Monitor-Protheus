from weepulse_monitor import create_app
from weepulse_monitor.extensions import db
from sqlalchemy import text 

from weepulse_monitor.models import (
    User, Server, AppConfig, HighlightKeyword, 
    ServiceMonitor, ServerMetric, AuditLog, LogAnomaly, RadarIgnoredService, TableGrowthLog,
    AlertSnapshot # <--- IMPORTANTE: Adicionada a nova tabela aqui para o script a reconhecer!
)

app = create_app()

with app.app_context():
    print("A verificar a estrutura do banco de dados...")
    
    # Cria tabelas novas que não existam (ex: TableGrowthLog, AlertSnapshot)
    db.create_all()
    
    engine = db.engine
    
    # Motor inteligente: Vasculha todas as tabelas e todas as colunas declaradas no models.py
    try:
        for table_name, table in db.Model.metadata.tables.items():
            result = db.session.execute(text(f"PRAGMA table_info({table_name});"))
            existing_columns = [row[1] for row in result.fetchall()]
            
            for column in table.columns:
                if column.name not in existing_columns:
                    print(f"⚙️ Encontrada coluna nova '{column.name}' na tabela '{table_name}'...")
                    
                    # Converte o tipo do SQLAlchemy para a linguagem do banco atual
                    col_type = column.type.compile(engine.dialect)
                    
                    # Como adicionar colunas NOT NULL em tabelas já com dados gera erro no SQLite, 
                    # injectamos um valor default provisório e seguro para qualquer tipo de dado.
                    default_val = "NULL"
                    if str(col_type).startswith("VARCHAR") or str(col_type).startswith("TEXT"):
                        default_val = "''"
                    elif str(col_type).startswith("INTEGER") or str(col_type).startswith("FLOAT") or str(col_type).startswith("BOOLEAN") or str(col_type).startswith("BIGINT"):
                        default_val = "0"
                        
                    query = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type} DEFAULT {default_val};"
                    db.session.execute(text(query))
                    db.session.commit()
                    print(f"✔️ Coluna '{column.name}' adicionada com sucesso na tabela '{table_name}'!")
                    
    except Exception as e:
        print(f"⚠️ Erro ao tentar atualizar campos na tabela (Pode ser ignorado se você não usa SQLite): {e}")
        db.session.rollback()

    print("✅ Banco de dados atualizado com sucesso sem perder dados antigos!")
