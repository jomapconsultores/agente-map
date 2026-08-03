# Deploy de agente_map en Coolify

Despliega la API (`api/main.py`) como aplicación Docker en Coolify, usando el
`Dockerfile` de la raíz. La base de datos **sigue en Supabase gestionado**
(la app usa `supabase-py`, no SQL crudo, así que migrar la BD no aporta nada).

Arquitectura: **Coolify = app · Supabase = datos**.

## 0. Pre-requisitos
- Instancia de Coolify funcionando (self-host o cloud).
- Repo en GitHub: `https://github.com/jomapconsultores/agente-map` (rama `agente-map`).
- Claves a mano (Anthropic, Mistral, Codestral, DeepSeek, Supabase) — ver `.env`.

## 1. Crear el recurso
1. En tu proyecto Coolify → **+ New Resource** → **Public/Private Repository**.
2. Repo: `https://github.com/jomapconsultores/agente-map` · Rama: **`agente-map`**.
   - Si es privado, conecta una GitHub App / Deploy Key en Coolify primero.
3. **Build Pack: Dockerfile** (Coolify lo detecta al ver el `Dockerfile` en la raíz).
4. **Port: 8000** (el contenedor expone 8000; el `CMD` respeta `$PORT`).
5. **Health Check Path:** `/healthz` (la imagen ya trae HEALTHCHECK propio también).

## 2. Variables de entorno
Pega el bloque completo en *Environment Variables* (toma los valores de tu `.env`):

```
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-4-6
SYSTEM_LANGUAGE=es

MISTRAL_API_KEY=...
CODESTRAL_API_KEY=...
DEEPSEEK_API_KEY=...
MISTRAL_MODEL=mistral-large-latest
CODESTRAL_MODEL=codestral-latest
DEEPSEEK_MODEL=deepseek-chat
BUILDER_ROTATION=mistral,codestral,deepseek

ROLE_RESEARCH=deepseek
ROLE_REVIEW_RESEARCH=mistral
ROLE_WRITER=deepseek
ROLE_REVIEW_WRITER=mistral
ROLE_FINANCIAL=codestral
MAX_PIPELINE_RESTARTS=5
PHASE_REVIEW_THRESHOLD=90

SUPABASE_URL=https://rzdpfhflkzwylaaplgml.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
SUPABASE_SECRET_KEY=sb_secret_...
SUPABASE_SERVICE_ROLE_JWT=eyJhbGciOi...

AGENTE_MAP_API_KEY=<un_secreto_largo_y_random>

WEBAUTHN_RP_ID=proyectos.pensamiento-libre.org
WEBAUTHN_ORIGIN=https://proyectos.pensamiento-libre.org
```

> `AGENTE_MAP_API_KEY` es la clave del **panel web** (no la de los modelos).
> La defines tú: elige una cadena larga y aleatoria.

## 3. Dominio + HTTPS
El dominio público es **`proyectos.pensamiento-libre.org`**. Pasos:

1. **DNS**: crea un `CNAME` (o `A`) para `proyectos` apuntando a tu servidor
   Coolify (mismo destino que el resto de subdominios de `pensamiento-libre.org`).
2. **Coolify** → la app → **Domains**: pon `https://proyectos.pensamiento-libre.org`.
   Traefik gestiona el certificado HTTPS automáticamente.

La app fija el dominio vía las variables `WEBAUTHN_RP_ID` / `WEBAUTHN_ORIGIN`
(ver bloque del paso 2), así la **biometría (WebAuthn) queda atada a este
dominio** sin depender del header `Host`. Deben coincidir exactamente con el
dominio configurado en Coolify y DNS.

> ⚠️ Si cambias el dominio más adelante, las credenciales biométricas ya
> registradas dejan de servir: cada usuario deberá **re-registrar** su
> biometría en el nuevo dominio (el login con contraseña / API key no se afecta).

## 4. Base de datos (una sola vez)
La BD ya existe en Supabase. Verifica/aplica las migraciones pendientes en el
SQL Editor: https://supabase.com/dashboard/project/rzdpfhflkzwylaaplgml/sql/new
(ver `db/006_webauthn.sql` y `db/008_captacion.sql` si faltan las tablas
`webauthn_credentials`, `prospectos`, `clientes`).

> **Migración 013 (recomendada):** aplica `db/013_resume_attempts.sql` para
> habilitar el auto-resume tras redeploy y los checkpoints de gate. Es aditiva e
> idempotente; sin ella el sistema sigue funcionando (solo no auto-reanuda).

## 5. Deploy y verificación
Pulsa **Deploy**. Cuando esté "Running":

```bash
curl https://TU-DOMINIO/healthz          # → {"ok": true, ...}
curl -H "X-API-Key: TU_API_KEY" https://TU-DOMINIO/doc_types
```

Swagger en `https://TU-DOMINIO/docs`.

## Notas operativas
- **Tareas largas**: por defecto (sin `REDIS_URL`) el pipeline corre in-process en
  un hilo. Si Coolify reinicia el contenedor a mitad, el trabajo se pierde, pero al
  arrancar el servidor **reconcilia** las sesiones huérfanas y **auto-reanuda** las
  reanudables (investigación ya aprobada) sin repetir la fase cara.
- **Worker separado (recomendado, elimina la pérdida por redeploy)**: despliega con
  `docker-compose.yml` (build pack "Docker Compose"), que levanta `web` + `worker` +
  `redis`. Define `REDIS_URL` (el compose ya la inyecta como `redis://redis:6379/0`).
  Con Redis, los endpoints ENCOLAN el trabajo y el `worker` lo procesa en su propio
  proceso: un redeploy del `web` ya no mata los pipelines en curso.
- **Timeouts/umbrales** (opcionales, con defaults coherentes): `ANTHROPIC_TIMEOUT_SEC`,
  `ANTHROPIC_MAX_RETRIES`, `LLM_TIMEOUT_SEC`, `LLM_MAX_RETRIES`,
  `MAX_PIPELINE_WALLCLOCK_SEC`, `STALE_RUNNING_MINUTES`. Invariante: el wall-clock
  (1500 s) debe ser menor que el watchdog (`STALE_RUNNING_MINUTES`×60 = 1800 s).
- **Persistencia**: no se necesita disco persistente; los .docx/.xlsx se
  regeneran on-demand desde Supabase.
- **Actualizar**: cada push a `agente-map` en GitHub puede disparar redeploy
  (activa "Auto Deploy" en Coolify o usa el webhook que Coolify provee).
