import os
import secrets

# Pega o diretório absoluto de onde este arquivo (config.py) está localizado
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Define o caminho para a pasta 'instance' na mesma estrutura do config.py
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

# Garante que a pasta 'instance' seja criada caso ainda não exista
os.makedirs(INSTANCE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# SECRET_KEY — chave usada para assinar cookies de sessão.
#
# Como funciona:
#   1. Se a variável de ambiente SECRET_KEY estiver definida, ela é usada.
#   2. Caso contrário, a chave é lida do arquivo 'instance/secret.key'.
#   3. Se o arquivo não existir ainda (primeira execução), uma chave forte
#      é gerada automaticamente com secrets.token_hex(32) e salva no arquivo.
#
# Isso garante que a chave seja SEMPRE a mesma entre reinicializações do
# serviço Windows, sem precisar configurar nada manualmente.
# ---------------------------------------------------------------------------
def _load_or_create_secret_key() -> str:
    # Prioridade 1: variável de ambiente (útil para ambientes gerenciados)
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        return env_key

    # Prioridade 2: arquivo persistente dentro da pasta instance
    key_file = os.path.join(INSTANCE_DIR, "secret.key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            return stored

    # Primeira execução: gera uma chave forte e salva para reutilização
    new_key = secrets.token_hex(32)
    with open(key_file, "w", encoding="utf-8") as f:
        f.write(new_key)
    print(f"[CONFIG] Nova SECRET_KEY gerada e salva em: {key_file}")
    return new_key


class Config:
    SECRET_KEY = _load_or_create_secret_key()

    # Caminho final do arquivo do banco de dados dentro da pasta instance
    db_path = os.path.join(INSTANCE_DIR, "weepulse_monitor.db")

    # URI de conexão do SQLAlchemy
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{db_path}")

    SQLALCHEMY_TRACK_MODIFICATIONS = False
