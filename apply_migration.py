"""Aplica migraciones via psycopg2 - intenta varios hosts/metodos."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\mapos\Dropbox\Programas\agente_map')
from dotenv import load_dotenv
load_dotenv()
import config

try:
    import psycopg2
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"])
    import psycopg2

REF  = "rzdpfhflkzwylaaplgml"
JWT  = config.SUPABASE_SERVICE_ROLE_JWT   # 219 chars, valid JWT

# Posibles combinaciones de host/puerto/usuario para JWT auth
CONFIGS = [
    # Pooler transaction mode con JWT (Supabase 2024+)
    {"host": "aws-1-us-west-2.pooler.supabase.com", "port": 6543,
     "user": f"postgres.{REF}", "password": JWT},
    # Pooler session mode con JWT
    {"host": "aws-1-us-west-2.pooler.supabase.com", "port": 5432,
     "user": f"postgres.{REF}", "password": JWT},
    # Direct connection (necesita IP/IPv4 habilitado y DB password, no JWT)
    # Dejamos como ultima opcion con DB password si el usuario lo configuro
]

# Si el usuario puso SUPABASE_DB_PASSWORD en .env, agregar conexion directa
db_pass = None
lines = open('.env').read().splitlines()
for line in lines:
    if line.startswith("SUPABASE_DB_PASSWORD="):
        db_pass = line.split("=", 1)[1].strip()
        break

if db_pass:
    CONFIGS.insert(0, {
        "host": f"db.{REF}.supabase.co", "port": 5432,
        "user": "postgres", "password": db_pass
    })
    CONFIGS.insert(1, {
        "host": "aws-1-us-west-2.pooler.supabase.com", "port": 6543,
        "user": f"postgres.{REF}", "password": db_pass
    })

SQL_MIGRATIONS = [
    ("005_progress", """
ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS current_phase text DEFAULT '';
ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS progress_steps jsonb DEFAULT '[]'::jsonb;
ALTER TABLE public.sessions ADD COLUMN IF NOT EXISTS pause_requested boolean NOT NULL DEFAULT false;
"""),
    ("007_oficios", """
CREATE TABLE IF NOT EXISTS public.oficios (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    oficio_id      text        NOT NULL UNIQUE,
    owner_user_id  uuid        REFERENCES public.users(id) ON DELETE SET NULL,
    entity         text        NOT NULL,
    doc_type       text        NOT NULL DEFAULT 'peticion',
    subject        text        NOT NULL,
    requester_name text        NOT NULL,
    requester_id   text        NOT NULL DEFAULT '',
    requester_role text        NOT NULL DEFAULT '',
    requester_address text     NOT NULL DEFAULT '',
    requester_phone   text     NOT NULL DEFAULT '',
    request_detail text        NOT NULL,
    extra_legal    text        NOT NULL DEFAULT '',
    extra_facts    text        NOT NULL DEFAULT '',
    doc_number     text        NOT NULL DEFAULT '',
    city           text        NOT NULL DEFAULT 'Cuenca',
    content        text        NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_oficios_owner ON public.oficios(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_oficios_created ON public.oficios(created_at DESC);
"""),
]

conn = None
for cfg in CONFIGS:
    print(f"Intentando {cfg['host']}:{cfg['port']} user={cfg['user'][:30]}...")
    try:
        conn = psycopg2.connect(
            **cfg, dbname="postgres", sslmode="require", connect_timeout=10
        )
        print(f"  Conexion EXITOSA con {cfg['host']}")
        break
    except Exception as e:
        print(f"  Fallo: {str(e)[:100]}")

if not conn:
    print("\n[FALLO] No se pudo conectar a la base de datos.")
    print("\nOpciones:")
    print("1. Agrega en .env:")
    print("   SUPABASE_DB_PASSWORD=<contrasena>")
    print("   (La encuentras en: supabase.com/dashboard/project/rzdpfhflkzwylaaplgml/settings/database)")
    print("\n2. O ejecuta manualmente en Supabase SQL Editor:")
    print("   https://supabase.com/dashboard/project/rzdpfhflkzwylaaplgml/sql/new")
    sys.exit(1)

conn.autocommit = True
cur = conn.cursor()

for name, sql in SQL_MIGRATIONS:
    print(f"\nAplicando {name}...")
    try:
        cur.execute(sql)
        print(f"  OK")
    except Exception as e:
        print(f"  Error: {e}")

conn.close()
print("\nMigraciones aplicadas.")
