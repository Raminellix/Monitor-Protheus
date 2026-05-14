import os

# Pega o diretório absoluto de onde este arquivo (config.py) está localizado
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Define o caminho para a pasta 'instance' na mesma estrutura do config.py
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

# Garante que a pasta 'instance' seja criada caso ainda não exista
os.makedirs(INSTANCE_DIR, exist_ok=True)

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

    # Caminho final do arquivo do banco de dados dentro da pasta instance
    db_path = os.path.join(INSTANCE_DIR, "weepulse_monitor.db")
    
    # URI de conexão do SQLAlchemy
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{db_path}")

    SQLALCHEMY_TRACK_MODIFICATIONS = False