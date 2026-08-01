# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""API HTTP de agente_map.

Auth multiusuario:
- `POST /auth/register` (auto-registro; 1er usuario = admin aprobado, resto pending)
- `POST /auth/login` → token firmado (Bearer)
- La X-API-Key maestra (AGENTE_MAP_API_KEY) entra como ADMIN bootstrap.
- Cada usuario ve SUS entregables; el admin ve todos.

Endpoints de datos: /modules, /doc_types, /extract, /propuestas[...],
/propuestas/{id}/(markdown|word|excel|retry|reviews).

Trabajos largos: el pipeline se despacha vía core.jobs (cola Redis + worker
separado si REDIS_URL está configurada; si no, hilo daemon en el propio web). El
estado se persiste en sessions.status (pending → running → approved/failed) y al
arrancar se reconcilian/auto-reanudan las sesiones que un redeploy dejó a medias.
"""
from __future__ import annotations

import hmac
import io
import json
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx  # dependencia transitiva del SDK de Supabase; para detectar cortes de red
from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

import config
import api.auth as auth_lib
from core import jobs
from db import queries
from db import repository
from db import users as users_repo
from db import permissions_repo
from models.doc_types import get_doc_type, list_doc_types, list_modules
from models.schemas import DocumentBrief, FinancialPackage, ProjectSession


# Observabilidad opcional: nunca debe tumbar el arranque de la API si el paquete
# no está instalado o el DSN está mal — es puramente best-effort.
try:
    import sentry_sdk
    _sentry_dsn = os.environ.get("SENTRY_DSN", "")
    if _sentry_dsn:
        sentry_sdk.init(
            dsn=_sentry_dsn,
            environment=os.environ.get("ENVIRONMENT", "production"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
            send_default_pii=False,
        )
except Exception as _sentry_err:  # noqa: BLE001
    print(f"[sentry] observabilidad deshabilitada: {_sentry_err}")


def _startup_recover() -> None:
    """Al arrancar el servidor: (1) reconcilia sesiones 'running' huérfanas que un
    redeploy/crash dejó colgadas (sin esto quedan en loader infinito hasta que un
    humano las abre), y (2) auto-reanuda las interrumpidas que son reanudables
    (investigación ya aprobada), sin repetir la fase más cara. Todo best-effort:
    nunca debe impedir que la API levante."""
    try:
        n = queries.reconcile_orphaned_running()
        print(f"[startup] reconciliadas {n} sesiones 'running' huérfanas")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] reconciliación falló (no fatal): {e}")

    try:
        pendientes = repository.list_recently_interrupted(hours=6, max_attempts=3)
    except Exception as e:  # noqa: BLE001
        print(f"[startup] auto-resume: no se pudo listar interrumpidas ({e})")
        pendientes = []
    resumidas = 0
    for row in pendientes:
        try:
            if not repository.load_resumable_state(row["session_id"]):
                continue  # sin checkpoint seguro que reutilizar
            repository.bump_resume_attempts(row["session_id"])  # ANTES de relanzar
            jobs.submit_pipeline(
                user_input=row.get("user_input") or "",
                mode=row.get("input_mode") or "text",
                doc_type_key=row.get("doc_type_key") or "auto",
                template_text=row.get("template_text") or "",
                support_docs=[(d.get("name"), d.get("text"))
                              for d in (row.get("support_docs") or [])],
                session_id=row["session_id"],
                owner_user_id=row.get("owner_user_id"),
                # is_admin/allowed_modules NO se persisten en la fila; el owner ya pasó
                # el gate de módulos al crear la sesión, así que se omite el re-gate.
                is_admin=False,
                allowed_modules=None,
            )
            resumidas += 1
        except Exception as e:  # noqa: BLE001
            print(f"[startup] auto-resume de {row.get('session_id')} falló: {e}")
    if resumidas:
        print(f"[startup] auto-reanudadas {resumidas} sesiones interrumpidas")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _startup_recover()
    yield


app = FastAPI(
    title="agente_map API",
    description="Pipeline multiagente multiusuario de entregables de alto nivel.",
    version="0.3.0",
    lifespan=_lifespan,
)

API_KEY_ENV = "AGENTE_MAP_API_KEY"


# ── Auth / principal ────────────────────────────────────────────────────────
class Principal:
    def __init__(self, user_id: Optional[str], role: str,
                 email: Optional[str] = None, name: Optional[str] = None,
                 master: bool = False, modules: Optional[list[str]] = None):
        self.user_id = user_id
        self.role = role
        self.email = email
        self.name = name
        self.master = master
        # Módulos (captacion/clientes/investigacion/proyectos/oficios) otorgados
        # por el administrador. Un admin tiene acceso implícito a todos.
        self.modules = list(modules or [])

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def owner_filter(self) -> Optional[str]:
        """None → ve todo (admin/maestra). Si no, restringe a su user_id."""
        return None if self.is_admin else self.user_id

    def has_module(self, module: str) -> bool:
        return self.is_admin or module in self.modules


def _load_modules(role: str, user_id: Optional[str]) -> list[str]:
    if role == "admin" or not user_id:
        return list(permissions_repo.MODULES)
    try:
        return permissions_repo.list_for_user(user_id)
    except Exception as e:  # noqa: BLE001
        # Fail-closed (0 módulos) para no bloquear el login si aún falta aplicar
        # la migración 010 — pero SIEMPRE logueado: sin esto, un fallo real de
        # Supabase (timeout, RLS mal aplicada, credenciales) se confunde
        # silenciosamente con "usuario sin módulos otorgados".
        print(f"[_load_modules] no se pudieron leer los módulos de user_id={user_id}: "
              f"{type(e).__name__}: {e}")
        return []


# Rutas que un usuario con clave temporal SÍ puede usar (si no, quedaría
# encerrado sin poder ni cambiarla ni consultar su propio estado).
_RUTAS_LIBRES_CLAVE = frozenset({"/auth/password", "/auth/me"})


def get_principal(request: Request = None,
                  authorization: Optional[str] = Header(None),
                  x_api_key: Optional[str] = Header(None)) -> Principal:
    master = os.getenv(API_KEY_ENV, "")
    if x_api_key and master and hmac.compare_digest(x_api_key, master):
        return Principal(None, "admin", name="Administrador", master=True,
                         modules=list(permissions_repo.MODULES))
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        payload = auth_lib.parse_token(token)
        if payload:
            uid = payload.get("uid")
            # Revalida contra el estado ACTUAL del usuario en la BD en vez de confiar
            # ciegamente en el payload firmado: sin esto, rechazar/banear a un usuario
            # no revocaba los tokens que ya tenía emitidos (hasta AUTH_TOKEN_TTL_HOURS
            # de acceso residual — 168h por defecto), ni un cambio de rol surtía efecto
            # hasta que el token expirara por sí solo.
            if users_repo.is_enabled():
                u = users_repo.get_by_id(uid)
                if not u or u.get("status") != "approved":
                    raise HTTPException(
                        401, "Sesión inválida: tu cuenta ya no está activa. Inicia sesión de nuevo.")
                # Clave temporal puesta por el administrador: el usuario no puede
                # operar hasta definir la suya. Se dejan pasar solo las rutas que
                # le permiten hacerlo (si no, quedaría encerrado).
                if u.get("must_change_password") and \
                        (request is None or request.url.path not in _RUTAS_LIBRES_CLAVE):
                    raise HTTPException(
                        403, "Tu clave fue restablecida por el administrador. "
                             "Define una nueva en POST /auth/password para continuar.")
                role = u.get("role", "user")
                return Principal(uid, role, email=u.get("email"), name=u.get("name"),
                                 modules=_load_modules(role, uid))
            # Sin BD de usuarios configurada no hay estado que revalidar: confía en el token.
            role = payload.get("role", "user")
            return Principal(uid, role, modules=list(permissions_repo.MODULES) if role == "admin" else [])
    raise HTTPException(401, "No autenticado. Inicia sesión.")


def require_admin(p: Principal) -> None:
    if not p.is_admin:
        raise HTTPException(403, "Requiere rol administrador.")


def require_module(p: Principal, module: str) -> None:
    if not p.has_module(module):
        raise HTTPException(
            403, f"No tienes el rol '{module}' asignado. Pídele al administrador que te lo otorgue.")


def _resolve_doc_module(doc_type_key: str) -> str:
    key = (doc_type_key or "auto").strip().lower()
    if key == "auto":
        return "proyectos"
    try:
        return get_doc_type(key).module
    except Exception:
        return "proyectos"


def require_doc_module(p: Principal, doc_type_key: str) -> None:
    mod = _resolve_doc_module(doc_type_key)
    if mod == "ambos":
        if p.is_admin or "proyectos" in p.modules or "investigacion" in p.modules:
            return
        raise HTTPException(
            403, "No tienes el rol 'proyectos' ni 'investigacion' asignado. "
                "Pídele al administrador que te otorgue alguno.")
    require_module(p, mod)


def _require_supabase() -> None:
    if not users_repo.is_enabled():
        raise HTTPException(500, "Base de datos (Supabase) no configurada en el servidor.")


def _db(fn, *args, **kwargs):
    """Ejecuta una operación de usuarios traduciendo errores de BD a un mensaje
    claro (típicamente: falta aplicar la migración 004)."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # Corte de red / DNS / origen caído: NO es un error de la aplicación ni un
        # esquema mal aplicado. Antes se filtraba crudo al usuario como
        # "Error de base de datos: ConnectError: [Errno -2] Name or service not
        # known". Se responde 503 (temporal, reintentable) con un mensaje accionable.
        if isinstance(e, httpx.TransportError) or type(e).__name__ in (
                "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError"):
            raise HTTPException(
                503, "La base de datos no está disponible en este momento (fallo de "
                     "conexión con Supabase). Reintenta en unos segundos; si persiste, "
                     "revisa el estado del proyecto en el panel de Supabase.")
        if "user_module_roles" in msg and ("does not exist" in msg or "schema cache" in msg or "relation" in msg):
            raise HTTPException(500, "La tabla de roles por módulo no existe. Aplica la migración "
                                     "db/010_module_roles.sql en Supabase (SQL Editor).")
        if "users" in msg and ("does not exist" in msg or "schema cache" in msg or "relation" in msg):
            raise HTTPException(500, "La tabla de usuarios no existe. Aplica la migración "
                                     "db/004_auth_and_owner.sql en Supabase (SQL Editor).")
        raise HTTPException(500, f"Error de base de datos: {type(e).__name__}: {e}")


# ── Schemas ───────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    name: str = Field("", max_length=120)
    password: str = Field(..., min_length=6, max_length=200)


class LoginRequest(BaseModel):
    email: str
    password: str


# ── Módulo de cuenta ─────────────────────────────────────────────────────────
# Longitud mínima al DEFINIR una clave nueva. El login sigue aceptando las
# claves cortas ya existentes; solo se exige al cambiarlas.
MIN_PASSWORD = 8


class UpdateProfileRequest(BaseModel):
    name: str = Field("", max_length=120)
    phone: str = Field("", max_length=40)
    position: str = Field("", max_length=120)
    # Cambiar el email exige confirmar la clave actual (es la credencial de acceso).
    email: Optional[str] = Field(None, max_length=200)
    current_password: str = Field("", max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., min_length=MIN_PASSWORD, max_length=200)


class CreateProposalRequest(BaseModel):
    # Sin min_length aquí: Pydantic lo cuenta SIN strip, así que "          " (espacios)
    # pasaba y el pipeline arrancaba con un tema vacío ("Documento sin especificar
    # contenido"). La longitud real (tras strip) y la regla "tema O documentos" se
    # validan en el model_validator de abajo, que sí ve todos los campos.
    user_input: str = Field("", description="Idea, tema o propuesta a procesar.")
    mode: str = Field("text", pattern="^(search|text|file|url)$")
    doc_type_key: Optional[str] = Field("auto", description="'auto' o una clave de DOC_TYPES.")
    template_text: str = ""
    support_docs: list[tuple[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tema_o_documentos(self):
        tema = (self.user_input or "").strip()
        hay_docs = bool(self.support_docs) or bool((self.template_text or "").strip())
        if len(tema) < 10 and not hay_docs:
            raise ValueError(
                "Especifica un tema real (al menos 10 caracteres no vacíos) "
                "o adjunta al menos un documento/plantilla.")
        self.user_input = tema  # normaliza: guarda el tema ya sin espacios sobrantes
        return self


class CreateProposalResponse(BaseModel):
    session_id: str
    status: str


class ScoutRequest(BaseModel):
    user_input: str = Field(..., min_length=5, description="Tema o sector a buscar.")


class GenerateRequest(BaseModel):
    selected: list[int] = Field(..., min_length=1,
                                description="Índices de las oportunidades elegidas (0 = la mejor).")


class GenerateResponse(BaseModel):
    sessions: list[CreateProposalResponse]


class SessionSummary(BaseModel):
    session_id: str
    status: str
    approved: bool
    current_cycle: int
    doc_type_key: str
    input_mode: Optional[str] = None
    user_input: Optional[str] = None
    title: Optional[str] = None
    funder: Optional[str] = None
    funder_url: Optional[str] = None
    deadline: Optional[str] = None
    deadline_label: Optional[str] = None
    deadline_status: Optional[str] = None
    overall_score: Optional[float] = None
    viability_score: Optional[float] = None
    winning_probability: Optional[float] = None
    weighted_score: Optional[float] = None
    is_scouting: bool = False
    opportunities_count: int = 0
    go_no_go: Optional[str] = None
    go_no_go_reasons: list = []
    risks: list = []
    strengths: list = []
    recommendations: Optional[str] = None
    evidence_sources: list = []
    feasibility_breakdown: dict = {}
    owner_user_id: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    current_phase: Optional[str] = None
    progress_steps: list = []


# ── Helpers ───────────────────────────────────────────────────────────────
def _project_session_from_db(row: dict) -> ProjectSession:
    from dataclasses import fields
    from models.schemas import AnalysisResult, EcuadorAlignment, FunderInfo, ReviewResult

    def _rebuild_dc(cls, d: dict | None):
        if d is None:
            return None
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in valid})

    analysis = None
    if row.get("analysis"):
        a = dict(row["analysis"])
        if a.get("funder"):
            a["funder"] = FunderInfo(**a["funder"])
        if a.get("ecuador_alignment"):
            a["ecuador_alignment"] = EcuadorAlignment(**a["ecuador_alignment"])
        analysis = _rebuild_dc(AnalysisResult, a)

    brief = _rebuild_dc(DocumentBrief, row.get("brief"))
    financial = _rebuild_dc(FinancialPackage, row.get("financial"))

    session = ProjectSession(
        session_id=row["session_id"],
        user_input=row["user_input"],
        input_mode=row["input_mode"],
        doc_type_key=row["doc_type_key"],
        analysis=analysis,
        brief=brief,
        financial=financial,
        approved=row.get("approved", False),
        current_cycle=row.get("current_cycle", 0),
    )
    session.proposal_versions = [v["content"] for v in row.get("proposal_versions", [])]
    session.review_results = [_rebuild_dc(ReviewResult, r) for r in row.get("reviews", [])]
    if session.proposal_versions:
        session.final_proposal = session.proposal_versions[-1]
    return session


def _auto_title(row: dict, is_scouting: bool = False) -> str:
    """Genera un título descriptivo cuando el pipeline aún no tiene uno.

    Compartida con db/queries.py vía utils/titles.py — antes cada módulo tenía
    su propia copia, y la de aquí usaba `strftime("%-d %b %Y")`, que no es
    portable a Windows (%-d es una extensión de glibc/macOS, no del CRT de
    Windows) y ya divergía en formato de fecha frente a la copia de queries.py.
    """
    from utils.titles import auto_title
    return auto_title(row, is_scouting)


def _row_to_summary(row: dict) -> SessionSummary:
    brief = row.get("brief") or {}
    analysis = row.get("analysis") or {}
    funder_dict = analysis.get("funder") or {}
    last_review = (row.get("reviews") or [])[-1] if row.get("reviews") else None
    score = float(last_review["overall_score"]) if last_review else None
    alts = analysis.get("alternatives") or []
    is_scouting = bool(alts)
    real_title = brief.get("title") or analysis.get("project_title")
    title = real_title or _auto_title(row, is_scouting=is_scouting)
    return SessionSummary(
        session_id=row["session_id"],
        status=row.get("status", "pending"),
        approved=row.get("approved", False),
        current_cycle=row.get("current_cycle", 0),
        doc_type_key=row["doc_type_key"],
        input_mode=row.get("input_mode"),
        user_input=row.get("user_input"),
        title=title,
        funder=funder_dict.get("name"),
        funder_url=funder_dict.get("url"),
        deadline=funder_dict.get("deadline"),
        deadline_label=funder_dict.get("deadline_label"),
        deadline_status=funder_dict.get("deadline_status"),
        overall_score=score,
        viability_score=analysis.get("viability_score"),
        winning_probability=analysis.get("winning_probability"),
        weighted_score=analysis.get("weighted_score"),
        is_scouting=bool(alts),
        opportunities_count=len(alts),
        go_no_go=analysis.get("go_no_go"),
        go_no_go_reasons=analysis.get("go_no_go_reasons") or [],
        risks=analysis.get("risks") or [],
        strengths=analysis.get("strengths") or [],
        recommendations=analysis.get("recommendations"),
        evidence_sources=analysis.get("evidence_sources") or [],
        feasibility_breakdown=analysis.get("feasibility_breakdown") or {},
        owner_user_id=row.get("owner_user_id"),
        created_at=str(row.get("created_at")) if row.get("created_at") else None,
        completed_at=str(row.get("completed_at")) if row.get("completed_at") else None,
        error_message=row.get("error_message"),
        current_phase=row.get("current_phase"),
        progress_steps=row.get("progress_steps") or [],
    )


def _owned_row(session_id: str, p: Principal) -> dict:
    """Recupera la sesión y verifica que el principal pueda verla."""
    row = queries.get_session(session_id)
    if not row:
        raise HTTPException(404, "session_id no encontrado")
    if not p.is_admin and row.get("owner_user_id") != p.user_id:
        raise HTTPException(403, "No tienes acceso a este entregable.")
    return row


# ── Static / health ─────────────────────────────────────────────────────────
STATIC_DIR = Path(__file__).parent / "static"

# ── WebAuthn helpers ─────────────────────────────────────────────────────────
# In-memory challenge store (single-instance).
_wa_challenges: dict[str, tuple[bytes, float]] = {}


def _wa_store(key: str, challenge: bytes) -> None:
    _wa_challenges[key] = (challenge, time.time() + 300)
    # Prune old entries to avoid memory growth
    now = time.time()
    for k in list(_wa_challenges):
        if _wa_challenges[k][1] < now:
            _wa_challenges.pop(k, None)


def _wa_pop(key: str) -> Optional[bytes]:
    entry = _wa_challenges.pop(key, None)
    return entry[0] if entry and entry[1] > time.time() else None


def _wa_rp_id(request: Request) -> str:
    rp = os.getenv("WEBAUTHN_RP_ID", "")
    if rp:
        return rp
    host = request.headers.get("host", "localhost")
    return host.split(":")[0]


def _wa_origin(request: Request) -> str:
    env = os.getenv("WEBAUTHN_ORIGIN", "")
    if env:
        return env
    host = request.headers.get("host", "localhost")
    fwd = request.headers.get("x-forwarded-proto", "")
    if fwd in ("http", "https"):
        scheme = fwd
    elif "." in host and "localhost" not in host:
        scheme = "https"
    else:
        scheme = "http"
    return f"{scheme}://{host}"


def _wa_import():
    """Lazy import with clear error if webauthn not installed."""
    try:
        import webauthn
        from webauthn.helpers import options_to_json, bytes_to_base64url
        from webauthn.helpers.structs import (
            AuthenticatorSelectionCriteria,
            AuthenticatorAttachment,
            UserVerificationRequirement,
            ResidentKeyRequirement,
            PublicKeyCredentialDescriptor,
            AuthenticatorAttestationResponse,
            AuthenticatorAssertionResponse,
            RegistrationCredential,
            AuthenticationCredential,
        )
        return (webauthn, options_to_json, bytes_to_base64url,
                AuthenticatorSelectionCriteria, AuthenticatorAttachment,
                UserVerificationRequirement, ResidentKeyRequirement,
                PublicKeyCredentialDescriptor, AuthenticatorAttestationResponse,
                AuthenticatorAssertionResponse, RegistrationCredential,
                AuthenticationCredential)
    except ImportError as e:
        raise HTTPException(501, f"WebAuthn no disponible. Instala 'webauthn' en el servidor: {e}")


# ── Static / health ─────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js",
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(STATIC_DIR / "manifest.json",
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/icon.svg", include_in_schema=False)
def icon_svg():
    return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=604800"})


@app.get("/logo.png", include_in_schema=False)
def logo_png():
    return FileResponse(STATIC_DIR / "logo.png", media_type="image/png",
                        headers={"Cache-Control": "max-age=604800"})


def _deployed_commit() -> str:
    """SHA del commit desplegado, para verificar QUÉ código está corriendo en vivo
    (antes /healthz solo daba una versión estática y no permitía confirmar deploys).
    Coolify/Render exponen el commit como variable de entorno; se prueban los
    nombres habituales y, si no, se lee git localmente como último recurso."""
    for var in ("SOURCE_COMMIT", "GIT_COMMIT", "GIT_SHA", "COMMIT_SHA",
                "RENDER_GIT_COMMIT", "COOLIFY_GIT_COMMIT", "COOLIFY_GIT_SHA"):
        val = os.environ.get(var, "")
        if val:
            return val[:12]
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=3,
                             cwd=os.path.dirname(os.path.dirname(__file__)))
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "agente_map", "version": app.version,
            "commit": _deployed_commit()}


# ── WebAuthn endpoints ───────────────────────────────────────────────────────

class WaRegisterVerifyReq(BaseModel):
    credential: dict
    label: str = "biometric"


class WaLoginOptionsReq(BaseModel):
    email: Optional[str] = None


class WaLoginVerifyReq(BaseModel):
    credential: dict
    email: Optional[str] = None


@app.post("/auth/webauthn/register-options")
async def wa_register_options(request: Request, p: Principal = Depends(get_principal)):
    """Genera las opciones de registro WebAuthn para el usuario autenticado."""
    _require_supabase()
    if not p.user_id:
        raise HTTPException(400, "Se requiere cuenta de usuario (no clave maestra) para registrar biometría.")
    wa = _wa_import()
    webauthn, options_to_json, _, AuthenticatorSelectionCriteria, AuthenticatorAttachment, \
        UserVerificationRequirement, ResidentKeyRequirement, *_ = wa

    u = _db(users_repo.get_by_id, p.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")

    rp_id = _wa_rp_id(request)
    user_id_bytes = p.user_id.encode()[:64]

    from db import webauthn as wa_db
    existing = wa_db.get_credentials_for_user(p.user_id)
    exclude = []
    if existing:
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor
        from webauthn.helpers import base64url_to_bytes
        for c in existing:
            try:
                exclude.append(PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["credential_id"])))
            except Exception:
                pass

    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="Proyectos MAP",
        user_id=user_id_bytes,
        user_name=u["email"],
        user_display_name=u.get("name") or u["email"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
        timeout=60000,
    )
    _wa_store(f"reg:{p.user_id}", options.challenge)
    return json.loads(options_to_json(options))


@app.post("/auth/webauthn/register-verify")
async def wa_register_verify(request: Request, req: WaRegisterVerifyReq,
                              p: Principal = Depends(get_principal)):
    """Verifica la respuesta del dispositivo y almacena la credencial biométrica."""
    _require_supabase()
    if not p.user_id:
        raise HTTPException(400, "Se requiere cuenta de usuario.")
    (webauthn, options_to_json, bytes_to_base64url,
     AuthenticatorSelectionCriteria, AuthenticatorAttachment,
     UserVerificationRequirement, ResidentKeyRequirement,
     PublicKeyCredentialDescriptor, AuthenticatorAttestationResponse,
     AuthenticatorAssertionResponse, RegistrationCredential,
     AuthenticationCredential) = _wa_import()

    challenge = _wa_pop(f"reg:{p.user_id}")
    if not challenge:
        raise HTTPException(400, "Challenge expirado. Solicita nuevas opciones de registro.")

    d = req.credential
    resp = d.get("response", {})
    try:
        cred = RegistrationCredential(
            id=d["id"],
            raw_id=d.get("rawId", d["id"]),
            response=AuthenticatorAttestationResponse(
                client_data_json=resp["clientDataJSON"],
                attestation_object=resp["attestationObject"],
                transports=resp.get("transports"),
            ),
            type=d.get("type", "public-key"),
        )
        verified = webauthn.verify_registration_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=_wa_rp_id(request),
            expected_origin=_wa_origin(request),
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(400, f"Verificación biométrica fallida: {e}")

    from db import webauthn as wa_db
    cred_id_b64 = bytes_to_base64url(verified.credential_id)
    if wa_db.get_credential(cred_id_b64):
        raise HTTPException(409, "Esta credencial ya está registrada.")

    label = (req.label or "biometric").strip() or "biometric"
    wa_db.save_credential(
        user_id=p.user_id,
        credential_id=cred_id_b64,
        public_key_bytes=verified.credential_public_key,
        sign_count=verified.sign_count,
        label=label,
    )
    return {"ok": True, "credential_id": cred_id_b64, "label": label}


@app.post("/auth/webauthn/login-options")
async def wa_login_options(request: Request, req: WaLoginOptionsReq):
    """Genera las opciones de autenticación. Email opcional (para allowCredentials)."""
    (webauthn, options_to_json, bytes_to_base64url,
     _AuthSel, _AuthAtt, UserVerificationRequirement, _ResKey, PublicKeyCredentialDescriptor,
     *_rest) = _wa_import()

    allow = []
    if req.email and users_repo.is_enabled():
        u = users_repo.get_by_email(req.email)
        if u:
            from db import webauthn as wa_db
            from webauthn.helpers import base64url_to_bytes
            for c in wa_db.get_credentials_for_user(u["id"]):
                try:
                    allow.append(PublicKeyCredentialDescriptor(
                        id=base64url_to_bytes(c["credential_id"])
                    ))
                except Exception:
                    pass

    rp_id = _wa_rp_id(request)
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=60000,
    )
    challenge_key = f"auth:{req.email or 'anon'}:{id(options)}"
    _wa_store(challenge_key, options.challenge)
    result = json.loads(options_to_json(options))
    result["_challenge_key"] = challenge_key
    return result


@app.post("/auth/webauthn/login-verify")
async def wa_login_verify(request: Request, req: WaLoginVerifyReq):
    """Verifica la respuesta biométrica y devuelve un token JWT."""
    _require_supabase()
    (webauthn, options_to_json, bytes_to_base64url,
     AuthenticatorSelectionCriteria, AuthenticatorAttachment,
     UserVerificationRequirement, ResidentKeyRequirement,
     PublicKeyCredentialDescriptor, AuthenticatorAttestationResponse,
     AuthenticatorAssertionResponse, RegistrationCredential,
     AuthenticationCredential) = _wa_import()

    d = req.credential
    challenge_key = d.get("_challenge_key", "")
    challenge = _wa_pop(challenge_key) if challenge_key else None
    if not challenge:
        raise HTTPException(400, "Challenge expirado. Vuelve a intentarlo.")

    from db import webauthn as wa_db
    cred_id = d.get("id", "")
    stored = wa_db.get_credential(cred_id)
    if not stored:
        raise HTTPException(404, "Credencial biométrica no reconocida. Regístrala de nuevo.")

    resp = d.get("response", {})
    try:
        cred = AuthenticationCredential(
            id=d["id"],
            raw_id=d.get("rawId", d["id"]),
            response=AuthenticatorAssertionResponse(
                client_data_json=resp["clientDataJSON"],
                authenticator_data=resp["authenticatorData"],
                signature=resp["signature"],
                user_handle=resp.get("userHandle"),
            ),
            type=d.get("type", "public-key"),
        )
        webauthn.verify_authentication_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=_wa_rp_id(request),
            expected_origin=_wa_origin(request),
            credential_public_key=stored["_public_key_bytes"],
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(401, f"Verificación biométrica fallida: {e}")

    wa_db.update_sign_count(cred_id, stored["sign_count"] + 1)

    u = _db(users_repo.get_by_id, stored["user_id"])
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")
    if u.get("status") != "approved":
        raise HTTPException(403, "Tu cuenta no está activa.")

    users_repo.touch_login(u["id"])
    token = auth_lib.make_token(u["id"], u.get("role", "user"))
    return {"token": token, "user": users_repo.public_view(u)}


@app.get("/auth/webauthn/credentials")
def wa_list_credentials(p: Principal = Depends(get_principal)):
    """Lista las credenciales biométricas del usuario autenticado."""
    _require_supabase()
    if not p.user_id:
        return []
    from db import webauthn as wa_db
    return wa_db.list_for_user(p.user_id)


@app.delete("/auth/webauthn/credentials/{credential_id}")
def wa_delete_credential(credential_id: str, p: Principal = Depends(get_principal)):
    """Elimina una credencial biométrica del usuario."""
    _require_supabase()
    if not p.user_id:
        raise HTTPException(400, "Se requiere cuenta de usuario.")
    from db import webauthn as wa_db
    ok = wa_db.delete_credential(credential_id=credential_id, user_id=p.user_id)
    if not ok:
        raise HTTPException(404, "Credencial no encontrada.")
    return {"ok": True}


# ── Auth endpoints ───────────────────────────────────────────────────────────
@app.post("/auth/register")
def register(req: RegisterRequest):
    _require_supabase()
    email = req.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Email inválido.")
    if _db(users_repo.get_by_email, email):
        raise HTTPException(409, "Ya existe una cuenta con ese email.")
    first = _db(users_repo.count_users) == 0
    h, salt = auth_lib.hash_password(req.password)
    role = "admin" if first else "user"
    status = "approved" if first else "pending"
    _db(users_repo.create_user, email=email, name=req.name, password_hash=h,
        password_salt=salt, role=role, status=status)
    return {
        "status": status, "role": role,
        "message": ("Eres el administrador; ya puedes iniciar sesión."
                    if first else
                    "Cuenta creada. Queda pendiente de aprobación por el administrador."),
    }


@app.post("/auth/login")
def login(req: LoginRequest):
    _require_supabase()
    u = _db(users_repo.get_by_email, req.email)
    if not u or not auth_lib.verify_password(req.password, u.get("password_hash", ""),
                                             u.get("password_salt", "")):
        raise HTTPException(401, "Email o contraseña incorrectos.")
    if u.get("status") == "pending":
        raise HTTPException(403, "Tu cuenta está pendiente de aprobación por el administrador.")
    if u.get("status") != "approved":
        raise HTTPException(403, "Tu cuenta no está activa. Contacta al administrador.")
    # Una clave temporal caducada no sirve: hay que pedir al administrador que la
    # restablezca de nuevo.
    if _clave_temporal_vencida(u):
        raise HTTPException(
            403, "La clave temporal que te entregó el administrador ya caducó. "
                 "Pídele que la restablezca nuevamente.")
    users_repo.touch_login(u["id"])
    token = auth_lib.make_token(u["id"], u.get("role", "user"))
    view = users_repo.public_view(u)
    view["modules"] = _load_modules(u.get("role", "user"), u["id"])
    # El cliente usa este flag para llevar al usuario directo al cambio de clave.
    return {"token": token, "user": view,
            "must_change_password": bool(u.get("must_change_password"))}


@app.get("/auth/me")
def me(p: Principal = Depends(get_principal)):
    if p.master:
        return {"id": None, "name": "Administrador (clave maestra)", "email": None,
                "role": "admin", "status": "approved", "modules": list(permissions_repo.MODULES)}
    u = _db(users_repo.get_by_id, p.user_id) if p.user_id else None
    if not u:
        raise HTTPException(401, "Sesión inválida.")
    view = users_repo.public_view(u)
    view["modules"] = _load_modules(u.get("role", "user"), u["id"])
    return view


# ── Mi cuenta: autoservicio del propio usuario ───────────────────────────────
def _client_ip(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def _generar_clave_temporal(largo: int = 12) -> str:
    """Clave temporal legible: sin caracteres ambiguos (0/O, 1/l/I) para poder
    dictarla por teléfono sin errores. Aleatoriedad criptográfica."""
    import secrets
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(largo))


def _clave_temporal_vencida(u: dict) -> bool:
    """True si el usuario arrastra una clave temporal ya caducada."""
    if not u.get("must_change_password"):
        return False
    exp = u.get("temp_password_expires")
    if not exp:
        return False
    try:
        from datetime import datetime, timezone
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


@app.put("/auth/profile")
def update_profile(req: UpdateProfileRequest, p: Principal = Depends(get_principal)):
    """El propio usuario actualiza sus datos. Cambiar el email —que es la
    credencial de acceso— exige confirmar la clave actual."""
    _require_supabase()
    if p.master or not p.user_id:
        raise HTTPException(400, "La clave maestra no tiene perfil editable.")
    u = _db(users_repo.get_by_id, p.user_id)
    if not u:
        raise HTTPException(401, "Sesión inválida.")

    email_nuevo = None
    if req.email:
        candidato = req.email.strip().lower()
        if candidato != (u.get("email") or "").lower():
            if "@" not in candidato or "." not in candidato.split("@")[-1]:
                raise HTTPException(400, "Email inválido.")
            if not auth_lib.verify_password(req.current_password,
                                            u.get("password_hash", ""),
                                            u.get("password_salt", "")):
                raise HTTPException(403, "Para cambiar el email debes confirmar tu clave actual.")
            if _db(users_repo.get_by_email, candidato):
                raise HTTPException(409, "Ya existe una cuenta con ese email.")
            email_nuevo = candidato

    actualizado = _db(users_repo.update_profile, p.user_id,
                      name=req.name or None, email=email_nuevo,
                      phone=req.phone, position=req.position)
    view = users_repo.public_view(actualizado)
    view["modules"] = _load_modules(actualizado.get("role", "user"), actualizado["id"])
    return view


@app.post("/auth/password")
def change_password(req: ChangePasswordRequest, request: Request,
                    p: Principal = Depends(get_principal)):
    """Cambio de clave propia: exige la anterior (o la temporal que entregó el
    administrador). Al guardarla se levanta el bloqueo de clave temporal."""
    _require_supabase()
    if p.master or not p.user_id:
        raise HTTPException(400, "La clave maestra no se cambia desde aquí.")
    u = _db(users_repo.get_by_id, p.user_id)
    if not u:
        raise HTTPException(401, "Sesión inválida.")
    if not auth_lib.verify_password(req.current_password, u.get("password_hash", ""),
                                    u.get("password_salt", "")):
        raise HTTPException(403, "La clave actual no es correcta.")
    if auth_lib.verify_password(req.new_password, u.get("password_hash", ""),
                                u.get("password_salt", "")):
        raise HTTPException(400, "La nueva clave debe ser distinta de la anterior.")
    h, salt = auth_lib.hash_password(req.new_password)
    _db(users_repo.set_password, p.user_id, password_hash=h, password_salt=salt,
        must_change=False, temp_expires=None)
    users_repo.log_password(p.user_id, "self_change", p.user_id, _client_ip(request))
    return {"ok": True, "message": "Clave actualizada correctamente."}


# ── Gestión de usuarios (solo admin) ─────────────────────────────────────────
@app.get("/users")
def list_users_ep(status: Optional[str] = Query(None), p: Principal = Depends(get_principal)):
    require_admin(p)
    _require_supabase()
    return _db(users_repo.list_users, status)


def _count_approved_admins() -> int:
    # Cuenta solo admins que realmente pueden autenticarse (status='approved'):
    # un admin 'rejected'/'pending' ya no puede iniciar sesión (get_principal lo
    # exige), pero seguía sumando aquí y neutralizaba el guard anti-último-admin.
    return sum(1 for u in _db(users_repo.list_users, None)
               if u.get("role") == "admin" and u.get("status") == "approved")


@app.post("/users/{user_id}/approve")
def approve_user(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    users_repo.set_status(user_id, "approved")
    return {"ok": True, "status": "approved"}


@app.post("/users/{user_id}/reject")
def reject_user(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    u = _db(users_repo.get_by_id, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")
    # Rechazar a un admin ya aprobado lo deja sin poder autenticarse — es una
    # vía alterna para desactivar al único admin funcional, igual que
    # make-trabajador/make-user; debe llevar la misma protección.
    if u.get("role") == "admin" and u.get("status") == "approved" and _count_approved_admins() <= 1:
        raise HTTPException(409, "No puedes rechazar al único administrador activo.")
    users_repo.set_status(user_id, "rejected")
    return {"ok": True, "status": "rejected"}


@app.post("/users/{user_id}/make-admin")
def make_admin(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    u = _db(users_repo.get_by_id, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")
    users_repo.set_role(user_id, "admin")
    return {"ok": True, "role": "admin"}


@app.post("/users/{user_id}/make-trabajador")
def make_trabajador(user_id: str, p: Principal = Depends(get_principal)):
    """Convierte una cuenta existente en 'trabajador'. Solo admin."""
    require_admin(p)
    u = _db(users_repo.get_by_id, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")
    was_last_admin_target = u.get("role") == "admin"
    if was_last_admin_target and _count_approved_admins() <= 1:
        raise HTTPException(409, "No puedes quitarle el rol admin al único administrador.")
    users_repo.set_role(user_id, "trabajador")
    # Cierra la ventana entre el chequeo y la escritura (dos requests concurrentes
    # degradando a los últimos 2 admins a la vez): si tras escribir ya no queda
    # ningún admin aprobado, revierte esta escritura en vez de dejar el sistema
    # sin nadie que pueda administrar.
    if was_last_admin_target and _count_approved_admins() == 0:
        users_repo.set_role(user_id, "admin")
        raise HTTPException(409, "No puedes quitarle el rol admin al único administrador.")
    return {"ok": True, "role": "trabajador"}


@app.post("/users/{user_id}/make-user")
def make_user(user_id: str, p: Principal = Depends(get_principal)):
    """Devuelve una cuenta (admin o trabajador) al rol base 'user'. Solo admin."""
    require_admin(p)
    u = _db(users_repo.get_by_id, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")
    was_last_admin_target = u.get("role") == "admin"
    if was_last_admin_target and _count_approved_admins() <= 1:
        raise HTTPException(409, "No puedes quitarle el rol admin al único administrador.")
    users_repo.set_role(user_id, "user")
    if was_last_admin_target and _count_approved_admins() == 0:
        users_repo.set_role(user_id, "admin")
        raise HTTPException(409, "No puedes quitarle el rol admin al único administrador.")
    return {"ok": True, "role": "user"}


class CreateWorkerRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=200)
    name: str = Field("", max_length=120)
    password: str = Field(..., min_length=6, max_length=200)


@app.post("/admin/trabajadores", status_code=201)
def create_trabajador(req: CreateWorkerRequest, p: Principal = Depends(get_principal)):
    """Crea directamente una cuenta 'trabajador' (aprobada de inmediato, sin pasar
    por la cola de auto-registro): el administrador la crea y luego le otorga los
    módulos pertinentes desde el panel de usuarios. Solo admin."""
    require_admin(p)
    _require_supabase()
    email = req.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Email inválido.")
    if _db(users_repo.get_by_email, email):
        raise HTTPException(409, "Ya existe una cuenta con ese email.")
    h, salt = auth_lib.hash_password(req.password)
    u = _db(users_repo.create_user, email=email, name=req.name, password_hash=h,
            password_salt=salt, role="trabajador", status="approved")
    return {"ok": True, "user": users_repo.public_view(u)}


@app.post("/users/{user_id}/reset-password")
def reset_user_password(user_id: str, request: Request,
                        p: Principal = Depends(get_principal)):
    """Recuperación de clave olvidada: el administrador genera una clave temporal
    de un solo uso. Se devuelve UNA vez (no queda almacenada en claro), caduca a
    las 72 h y el usuario está obligado a cambiarla al entrar. Solo admin."""
    require_admin(p)
    _require_supabase()
    u = _db(users_repo.get_by_id, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado.")

    from datetime import datetime, timedelta, timezone
    temporal = _generar_clave_temporal()
    h, salt = auth_lib.hash_password(temporal)
    expira = datetime.now(timezone.utc) + timedelta(hours=72)
    _db(users_repo.set_password, user_id, password_hash=h, password_salt=salt,
        must_change=True, temp_expires=expira.isoformat(), reset_by=p.user_id)
    users_repo.log_password(user_id, "reset_admin", p.user_id, _client_ip(request))
    return {
        "ok": True,
        "temporary_password": temporal,
        "expires_at": expira.isoformat(),
        "message": (f"Entrega esta clave temporal a {u.get('name') or u.get('email')} "
                    f"en persona. Caduca en 72 horas y deberá cambiarla al entrar. "
                    f"No se volverá a mostrar."),
    }


class GrantModuleRequest(BaseModel):
    module: str


@app.get("/admin/roles")
def list_module_roles(p: Principal = Depends(get_principal)):
    """Todos los grants de módulo agrupados por usuario. Solo admin."""
    require_admin(p)
    _require_supabase()
    return _db(permissions_repo.list_all)


@app.post("/users/{user_id}/roles")
def grant_module_role(user_id: str, req: GrantModuleRequest, p: Principal = Depends(get_principal)):
    """Otorga a `user_id` acceso a un módulo. Solo admin."""
    require_admin(p)
    _require_supabase()
    if req.module not in permissions_repo.MODULES:
        raise HTTPException(400, f"Módulo desconocido: {req.module}")
    _db(permissions_repo.grant, user_id, req.module, p.user_id)
    return {"ok": True}


@app.delete("/users/{user_id}/roles/{module}")
def revoke_module_role(user_id: str, module: str, p: Principal = Depends(get_principal)):
    """Revoca a `user_id` el acceso a un módulo. Solo admin."""
    require_admin(p)
    _require_supabase()
    _db(permissions_repo.revoke, user_id, module)
    return {"ok": True}


# ── Catálogo ─────────────────────────────────────────────────────────────────
@app.get("/proveedores")
def proveedores(p: Principal = Depends(get_principal)):
    """Estado y saldo (cuando el proveedor lo expone) de las IAs. Solo admin."""
    require_admin(p)
    from utils.providers import provider_status
    return provider_status()


@app.get("/modules")
def modules(p: Principal = Depends(get_principal)):
    return list_modules()


@app.get("/doc_types")
def doc_types(p: Principal = Depends(get_principal)):
    return [{"key": d.key, "name": d.name, "description": d.description, "module": d.module}
            for d in list_doc_types()]


# ── Extracción de archivos ───────────────────────────────────────────────────
MAX_EXTRACT_FILES = 12
MAX_EXTRACT_BYTES = 20 * 1024 * 1024
MAX_EXTRACT_CHARS = 120_000


@app.post("/extract")
async def extract(files: list[UploadFile] = File(...), p: Principal = Depends(get_principal)):
    from tools import file_reader
    if len(files) > MAX_EXTRACT_FILES:
        raise HTTPException(413, f"Máximo {MAX_EXTRACT_FILES} archivos por carga.")
    out = []
    total = 0
    for f in files:
        # Validar el tamaño ANTES de materializar el archivo en RAM: UploadFile.size
        # ya viene calculado por el parser multipart. Sin esto, `await f.read()`
        # copiaba a memoria un archivo de cualquier tamaño antes del chequeo (vector
        # de DoS por RAM con peticiones concurrentes). También se acota el total.
        size = f.size or 0
        if size > MAX_EXTRACT_BYTES:
            raise HTTPException(413, f"'{f.filename}' supera 20 MB.")
        total += size
        if total > MAX_EXTRACT_BYTES * MAX_EXTRACT_FILES:
            raise HTTPException(413, "La carga total supera el límite permitido.")
        data = await f.read()
        if not data:
            raise HTTPException(400, f"'{f.filename}' está vacío.")
        if len(data) > MAX_EXTRACT_BYTES:  # defensa en profundidad si f.size es None
            raise HTTPException(413, f"'{f.filename}' supera 20 MB.")
        try:
            text = file_reader.read_upload(f.filename, data)
        except (ValueError, ImportError) as e:
            raise HTTPException(415, f"'{f.filename}': {e}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"No se pudo procesar '{f.filename}': {e}")
        text = (text or "").strip()
        if not text:
            raise HTTPException(422, f"'{f.filename}' no contiene texto legible "
                                     "(¿PDF escaneado sin OCR?).")
        out.append({"name": f.filename, "text": text[:MAX_EXTRACT_CHARS],
                    "chars": len(text), "truncated": len(text) > MAX_EXTRACT_CHARS})
    return out


# ── Entregables ──────────────────────────────────────────────────────────────
@app.post("/propuestas", response_model=CreateProposalResponse, status_code=202)
def create_proposal(req: CreateProposalRequest, background: BackgroundTasks,
                    p: Principal = Depends(get_principal)):
    require_doc_module(p, req.doc_type_key or "auto")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    session_id = uuid.uuid4().hex[:8]
    owner = p.user_id  # None si es la clave maestra
    is_admin, allowed_modules = p.is_admin, list(p.modules)

    jobs.submit_pipeline(
        user_input=req.user_input, mode=req.mode,
        doc_type_key=(req.doc_type_key or "auto"),
        template_text=req.template_text, support_docs=req.support_docs,
        session_id=session_id, owner_user_id=owner,
        is_admin=is_admin, allowed_modules=allowed_modules,
    )
    return CreateProposalResponse(session_id=session_id, status="pending")


# ── Scouting: buscar → reporte con calificación → elegir → generar ───────────
@app.post("/buscar", response_model=CreateProposalResponse, status_code=202)
def buscar_oportunidades(req: ScoutRequest, background: BackgroundTasks,
                         p: Principal = Depends(get_principal)):
    """Detecta el TOP-N de oportunidades y arma un reporte con calificación
    ponderada (NO genera propuestas). Luego se eligen con POST /generar."""
    require_module(p, "proyectos")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    session_id = uuid.uuid4().hex[:8]
    owner = p.user_id

    jobs.submit_scouting(user_input=req.user_input, session_id=session_id, owner_user_id=owner)
    return CreateProposalResponse(session_id=session_id, status="pending")


@app.get("/propuestas/{session_id}/oportunidades")
def listar_oportunidades(session_id: str, p: Principal = Depends(get_principal)):
    """Lista las oportunidades detectadas (para mostrarlas en el programa)."""
    row = _owned_row(session_id, p)
    analysis = row.get("analysis") or {}
    opps = analysis.get("alternatives") or []
    out = []
    for i, o in enumerate(opps):
        funder = o.get("funder") or {}
        out.append({
            "index": i,
            "title": o.get("title"),
            "summary": o.get("summary"),
            "funder": funder.get("name"),
            "funder_type": funder.get("type"),
            "url": funder.get("url"),
            "deadline": funder.get("deadline"),
            "amount": o.get("total_amount") or funder.get("amount_range"),
            "sector": o.get("sector"),
            "viability_score": o.get("viability_score"),
            "winning_probability": o.get("winning_probability"),
            "weighted_score": o.get("weighted_score"),
            "feasibility_breakdown": o.get("feasibility_breakdown") or {},
        })
    return {"session_id": session_id, "count": len(out), "opportunities": out}


@app.get("/propuestas/{session_id}/reporte")
def descargar_reporte(session_id: str, ids: Optional[str] = Query(None,
                      description="Índices separados por coma (ej. '0,2'); vacío = todas."),
                      fmt: str = Query("word", pattern="^(word|pdf)$"),
                      p: Principal = Depends(get_principal)):
    """Descarga el reporte de detección en Word o PDF (una, varias o todas)."""
    from tools import document_builder
    row = _owned_row(session_id, p)
    analysis = row.get("analysis") or {}
    opps = analysis.get("alternatives") or []
    if not opps:
        raise HTTPException(409, "Esta sesión no tiene oportunidades detectadas.")
    if ids:
        try:
            wanted = [int(x) for x in ids.split(",") if x.strip() != ""]
        except ValueError:
            raise HTTPException(400, "Parámetro 'ids' inválido (usa enteros separados por coma).")
        selected = [opps[i] for i in wanted if 0 <= i < len(opps)]
        if not selected:
            raise HTTPException(404, "Ningún índice válido en 'ids'.")
    else:
        selected = opps
    topic = row.get("user_input") or "Oportunidades"
    with tempfile.TemporaryDirectory() as tmp:
        if fmt == "pdf":
            out = Path(tmp) / f"REPORTE_{session_id}.pdf"
            md = document_builder.scouting_report_markdown(selected, topic)
            built = document_builder.build_pdf(md, "Reporte de oportunidades", out, subtitle=topic)
            mime, ext = "application/pdf", "pdf"
        else:
            out = Path(tmp) / f"REPORTE_{session_id}.docx"
            built = document_builder.build_scouting_report(selected, topic, out)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ext = "docx"
        if not built or not out.exists():
            raise HTTPException(500, "No se pudo generar el reporte (¿falta fpdf2 para PDF?).")
        data = out.read_bytes()
    return StreamingResponse(
        io.BytesIO(data), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="reporte_{session_id}.{ext}"'},
    )


@app.post("/propuestas/{session_id}/generar", response_model=GenerateResponse, status_code=202)
def generar_desde_seleccion(session_id: str, req: GenerateRequest, background: BackgroundTasks,
                            p: Principal = Depends(get_principal)):
    """Con el visto bueno del usuario: por cada oportunidad elegida lanza la
    generación de la propuesta completa (enfocada en esa entidad y sus requisitos)."""
    require_module(p, "proyectos")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    row = _owned_row(session_id, p)
    analysis = row.get("analysis") or {}
    opps = analysis.get("alternatives") or []
    if not opps:
        raise HTTPException(409, "Esta sesión no tiene oportunidades para generar.")
    owner = row.get("owner_user_id")
    is_admin, allowed_modules = p.is_admin, list(p.modules)

    created: list[CreateProposalResponse] = []
    for idx in req.selected:
        if not (0 <= idx < len(opps)):
            continue
        opp = opps[idx]
        funder = opp.get("funder") or {}
        title = opp.get("title") or funder.get("name") or row.get("user_input")
        url = funder.get("url") or ""
        user_input = f"{title} — {funder.get('name', '')}".strip(" —")
        # Si título y financiador venían vacíos, el f-string degenera en "None"/"" tras
        # el strip → se saltaría esta oportunidad en vez de lanzar un pipeline sobre nada.
        _ui = (user_input or "").strip()
        if len(_ui) < 5 or _ui.lower() == "none":
            continue
        user_input = _ui
        mode = "url" if url.startswith("http") else "search"
        if mode == "url":
            user_input = f"{user_input}\n{url}"
        new_id = uuid.uuid4().hex[:8]

        jobs.submit_pipeline(user_input=user_input, mode=mode, doc_type_key="propuesta",
                             session_id=new_id, owner_user_id=owner, seed_opportunity=opp,
                             is_admin=is_admin, allowed_modules=allowed_modules)
        created.append(CreateProposalResponse(session_id=new_id, status="pending"))

    if not created:
        raise HTTPException(400, "Ningún índice válido en 'selected'.")
    return GenerateResponse(sessions=created)


@app.get("/propuestas", response_model=list[SessionSummary])
def list_proposals(limit: int = Query(20, ge=1, le=100), approved_only: bool = False,
                   p: Principal = Depends(get_principal)):
    rows = queries.list_sessions(limit=limit, approved_only=approved_only,
                                 owner_user_id=p.owner_filter())
    return [_row_to_summary(r) for r in rows]


@app.get("/propuestas/{session_id}", response_model=SessionSummary)
def get_proposal(session_id: str, p: Principal = Depends(get_principal)):
    return _row_to_summary(_owned_row(session_id, p))


@app.get("/propuestas/{session_id}/reviews")
def get_proposal_reviews(session_id: str, p: Principal = Depends(get_principal)):
    """Historial completo de verificación 90/90 (por ciclo) + versiones, para
    auditar/verificar lo producido en cualquier momento."""
    row = _owned_row(session_id, p)
    brief = row.get("brief") or {}
    return {
        "session_id": session_id,
        "approved": row.get("approved", False),
        "doc_type_key": row.get("doc_type_key"),
        "evaluation_criteria": brief.get("evaluation_criteria") or [],
        "reviews": row.get("reviews") or [],
        "versions": [{"cycle": v.get("cycle"), "char_count": v.get("char_count"),
                      "created_at": str(v.get("created_at"))}
                     for v in (row.get("proposal_versions") or [])],
    }


@app.get("/propuestas/{session_id}/markdown")
def get_proposal_markdown(session_id: str, p: Principal = Depends(get_principal)):
    row = _owned_row(session_id, p)
    versions = row.get("proposal_versions") or []
    if not versions:
        raise HTTPException(409, "El entregable aún no tiene borradores; revisa el status.")
    return StreamingResponse(
        io.BytesIO(versions[-1]["content"].encode("utf-8")),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="entregable_{session_id}.md"'},
    )


def _build_and_stream(row: dict, kind: str):
    from tools import document_builder
    session_id = row["session_id"]
    if not (row.get("proposal_versions") or []):
        raise HTTPException(409, "El entregable aún no tiene contenido.")
    sess = _project_session_from_db(row)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if kind == "word":
            excel_name = f"CALCULOS_{session_id}.xlsx"
            out = tmp_path / f"ENTREGABLE_{session_id}.docx"
            built = document_builder.build_word(sess.final_proposal, sess.brief,
                                                sess.financial, excel_name, out)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ext = "docx"
        elif kind == "excel":
            has_budget = bool(sess.financial and sess.financial.budget_items)
            has_stats = bool(sess.brief and (sess.brief.statistics or {}).get("datasets"))
            if not (has_budget or has_stats):
                raise HTTPException(409, "Este entregable no tiene presupuesto ni datos estadísticos.")
            out = tmp_path / f"CALCULOS_{session_id}.xlsx"
            if has_budget:
                built = document_builder.build_excel(sess.brief, sess.financial, out)
            else:
                built = document_builder.build_excel_stats(sess.brief, out)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = "xlsx"
        elif kind == "pdf":
            out = tmp_path / f"ENTREGABLE_{session_id}.pdf"
            title = sess.brief.title if sess.brief else "Entregable"
            built = document_builder.build_pdf(sess.final_proposal, title, out)
            mime, ext = "application/pdf", "pdf"
        else:
            raise HTTPException(400, f"kind inválido: {kind}")
        if not built or not out.exists():
            raise HTTPException(500, f"No se pudo generar el {kind} (revisa logs).")
        data = out.read_bytes()
    return StreamingResponse(
        io.BytesIO(data), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="entregable_{session_id}.{ext}"'},
    )


@app.get("/propuestas/{session_id}/word")
def get_proposal_word(session_id: str, p: Principal = Depends(get_principal)):
    return _build_and_stream(_owned_row(session_id, p), "word")


@app.get("/propuestas/{session_id}/excel")
def get_proposal_excel(session_id: str, p: Principal = Depends(get_principal)):
    return _build_and_stream(_owned_row(session_id, p), "excel")


@app.get("/propuestas/{session_id}/pdf")
def get_proposal_pdf(session_id: str, p: Principal = Depends(get_principal)):
    return _build_and_stream(_owned_row(session_id, p), "pdf")


@app.post("/propuestas/{session_id}/retry", response_model=CreateProposalResponse, status_code=202)
def retry_proposal(session_id: str, background: BackgroundTasks,
                   p: Principal = Depends(get_principal)):
    row = _owned_row(session_id, p)
    require_doc_module(p, row.get("doc_type_key") or "auto")
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    # No relanzar un trabajo ya en curso (evita pipelines concurrentes sobre el mismo
    # registro, que se pisan el progreso y el estado entre sí).
    if row.get("status") in ("running", "pending"):
        raise HTTPException(409, "Este entregable ya está en curso; espera a que "
                                 "termine o cancélalo antes de reintentar.")
    # No reintentar una sesión "envenenada": si su tema quedó vacío o como el placeholder
    # "(initializing)" (caso histórico), reintentar volvería a investigar la nada y a
    # fallar. Se fuerza al usuario a reescribir el tema con «Mejorar tema».
    mode = row.get("input_mode") or "text"
    _ui = (row.get("user_input") or "").strip()
    _has_docs = bool(row.get("support_docs")) or bool((row.get("template_text") or "").strip())
    if (len(_ui) < 10 or _ui.lower() == "(initializing)") and not _has_docs:
        raise HTTPException(409, "Esta sesión no tiene un tema válido; usa «Mejorar tema» "
                                 "para reescribirlo antes de reintentar.")
    user_input = _ui
    # Si el intento previo NO llegó a aprobarse, vuelve a CLASIFICAR desde cero
    # ("auto") en vez de congelar el tipo detectado antes: una mala clasificación
    # previa (p. ej. una cotización marcada como "artículo científico") hacía que
    # el reintento repitiera el mismo error de origen y nunca produjera algo útil.
    # Para una sesión ya aprobada se respeta el tipo con el que se generó.
    doc_type_key = (row.get("doc_type_key") or "auto") if row.get("approved") else "auto"
    template_text = row.get("template_text") or ""
    support_docs = [(d.get("name"), d.get("text")) for d in (row.get("support_docs") or [])]
    owner = row.get("owner_user_id")
    is_admin, allowed_modules = p.is_admin, list(p.modules)

    jobs.submit_pipeline(user_input=user_input, mode=mode, doc_type_key=doc_type_key,
                         template_text=template_text, support_docs=support_docs,
                         session_id=session_id, owner_user_id=owner,
                         is_admin=is_admin, allowed_modules=allowed_modules)
    return CreateProposalResponse(session_id=session_id, status="pending")


@app.post("/propuestas/{session_id}/cancel", status_code=200)
def cancel_proposal(session_id: str, p: Principal = Depends(get_principal)):
    """Cancela un trabajo en cola/en curso (lo marca como cancelado; conserva el registro)."""
    from db import repository
    _owned_row(session_id, p)
    try:
        ok = repository.cancel_session(session_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo cancelar: {type(e).__name__}: {e}")
    if not ok:
        raise HTTPException(404, "session_id no encontrado")
    return {"ok": True, "status": "failed"}


@app.post("/propuestas/{session_id}/pause", status_code=200)
def pause_proposal(session_id: str, p: Principal = Depends(get_principal)):
    """Señaliza al pipeline que se detenga limpiamente al final de la fase actual."""
    from db import repository
    row = _owned_row(session_id, p)
    if row.get("status") not in ("running", "pending"):
        raise HTTPException(409, "Solo se puede pausar un trabajo en curso o en cola.")
    try:
        repository.request_pause(session_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo pausar: {type(e).__name__}: {e}")
    return {"ok": True, "status": "pausing"}


@app.delete("/propuestas/{session_id}", status_code=200)
def delete_proposal(session_id: str, p: Principal = Depends(get_principal)):
    """Borra el entregable del usuario (inconcluso o no): sesión + borradores + revisiones."""
    from db import repository
    _owned_row(session_id, p)  # verifica existencia y propiedad
    try:
        ok = repository.delete_session(session_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"No se pudo borrar: {type(e).__name__}: {e}")
    if not ok:
        raise HTTPException(404, "session_id no encontrado")
    return {"ok": True, "deleted": session_id}


# ── Oficios y peticiones ──────────────────────────────────────────────────────
class OficioRequest(BaseModel):
    entity: str = Field(..., min_length=2, description="Entidad destinataria (SRI, IESS, etc.)")
    entity_authority: str = Field("", description="Cargo y nombre de la autoridad")
    entity_city: str = Field("", description="Ciudad de la entidad")
    doc_type: str = Field("peticion", pattern="^(oficio|peticion|recurso_reposicion|recurso_apelacion|queja|memorando)$")
    subject: str = Field(..., min_length=5, description="Asunto del documento")
    requester_name: str = Field(..., min_length=3, description="Nombre del solicitante")
    requester_id: str = Field("", description="Cédula o RUC del solicitante")
    requester_role: str = Field("", description="Calidad o cargo del solicitante")
    requester_address: str = Field("", description="Dirección del solicitante")
    requester_phone: str = Field("", description="Teléfono o email del solicitante")
    request_detail: str = Field(..., min_length=10, description="Detalle de la petición o solicitud")
    extra_legal: str = Field("", description="Fundamento legal adicional")
    extra_facts: str = Field("", description="Antecedentes o hechos relevantes")
    doc_number: str = Field("", description="Número de oficio")
    city: str = Field("Cuenca", description="Ciudad de emisión")
    date_str: str = Field("", description="Fecha (auto si vacío)")


@app.get("/oficios/entidades")
def oficios_entidades(p: Principal = Depends(get_principal)):
    """Lista de entidades disponibles con indicador de base legal integrada."""
    from agents.oficio import list_entities
    return list_entities()


@app.get("/oficios/tipos")
def oficios_tipos(p: Principal = Depends(get_principal)):
    """Tipos de documentos disponibles."""
    from agents.oficio import list_doc_types_oficio
    return list_doc_types_oficio()


@app.post("/oficios", status_code=201)
def crear_oficio(req: OficioRequest, p: Principal = Depends(get_principal)):
    """Genera un oficio/petición y lo guarda en la base de datos."""
    require_module(p, "oficios")
    _require_supabase()
    from agents.oficio import generate
    from db import oficio_repo

    try:
        content = generate(
            entity=req.entity,
            entity_authority=req.entity_authority,
            entity_city=req.entity_city,
            doc_type=req.doc_type,
            subject=req.subject,
            requester_name=req.requester_name,
            requester_id=req.requester_id,
            requester_role=req.requester_role,
            requester_address=req.requester_address,
            requester_phone=req.requester_phone,
            request_detail=req.request_detail,
            extra_legal=req.extra_legal,
            extra_facts=req.extra_facts,
            doc_number=req.doc_number,
            city=req.city,
            date_str=req.date_str,
        )
    except Exception as e:
        raise HTTPException(500, f"Error al generar el documento: {e}")

    try:
        oficio_id = oficio_repo.save(
            # p.user_id es None con la clave maestra — la columna es uuid NULLABLE
            # (ON DELETE SET NULL); "" no es un UUID válido y hacía fallar el INSERT
            # en silencio, dejando el oficio generado pero nunca guardado.
            owner_user_id=p.user_id,
            entity=req.entity,
            doc_type=req.doc_type,
            subject=req.subject,
            requester_name=req.requester_name,
            requester_id=req.requester_id,
            requester_role=req.requester_role,
            requester_address=req.requester_address,
            requester_phone=req.requester_phone,
            request_detail=req.request_detail,
            extra_legal=req.extra_legal,
            extra_facts=req.extra_facts,
            doc_number=req.doc_number,
            city=req.city,
            content=content,
        )
    except Exception:
        oficio_id = None

    return {"oficio_id": oficio_id, "content": content}


@app.get("/oficios")
def listar_oficios(limit: int = Query(50, ge=1, le=200), p: Principal = Depends(get_principal)):
    """Lista los oficios del usuario."""
    require_module(p, "oficios")  # gate de módulo coherente con el resto de /oficios/*
    _require_supabase()
    from db import oficio_repo
    owner = p.owner_filter()
    return oficio_repo.list_oficios(owner_user_id=owner, limit=limit)


@app.get("/oficios/{oficio_id}")
def get_oficio(oficio_id: str, p: Principal = Depends(get_principal)):
    """Devuelve un oficio completo."""
    _require_supabase()
    from db import oficio_repo
    row = oficio_repo.get_oficio(oficio_id)
    if not row:
        raise HTTPException(404, "Oficio no encontrado.")
    if not p.is_admin and row.get("owner_user_id") != p.user_id:
        raise HTTPException(403, "Sin acceso.")
    return row


@app.get("/oficios/{oficio_id}/word")
def descargar_oficio_word(oficio_id: str, p: Principal = Depends(get_principal)):
    """Descarga el oficio en formato Word (.docx)."""
    _require_supabase()
    from db import oficio_repo
    row = oficio_repo.get_oficio(oficio_id)
    if not row:
        raise HTTPException(404, "Oficio no encontrado.")
    if not p.is_admin and row.get("owner_user_id") != p.user_id:
        raise HTTPException(403, "Sin acceso.")

    from exporters.word_exporter import oficio_to_docx
    buf = oficio_to_docx(row)
    filename = f"oficio_{row.get('doc_number') or oficio_id[:8]}.docx"
    return StreamingResponse(
        io.BytesIO(buf),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/oficios/{oficio_id}/pdf")
def descargar_oficio_pdf(oficio_id: str, p: Principal = Depends(get_principal)):
    """Descarga el oficio en PDF."""
    _require_supabase()
    from db import oficio_repo
    row = oficio_repo.get_oficio(oficio_id)
    if not row:
        raise HTTPException(404, "Oficio no encontrado.")
    if not p.is_admin and row.get("owner_user_id") != p.user_id:
        raise HTTPException(403, "Sin acceso.")

    from exporters.pdf_exporter import oficio_to_pdf
    buf = oficio_to_pdf(row)
    filename = f"oficio_{row.get('doc_number') or oficio_id[:8]}.pdf"
    return StreamingResponse(
        io.BytesIO(buf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/oficios/{oficio_id}", status_code=200)
def eliminar_oficio(oficio_id: str, p: Principal = Depends(get_principal)):
    """Elimina un oficio. Un admin puede borrar el de cualquier usuario."""
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import oficio_repo
    # Admin: no filtra por dueño (antes filtraba por el ID del propio admin, que
    # nunca coincide con el oficio ajeno — 0 filas borradas pero respondía ok:true).
    ok = oficio_repo.delete_oficio(oficio_id=oficio_id,
                                    owner_user_id=None if p.is_admin else p.user_id)
    if not ok:
        raise HTTPException(404, "Oficio no encontrado.")
    return {"ok": True}


# ── Migración administrativa ─────────────────────────────────────────────────
def _load_migration_sql() -> str:
    """Concatena las migraciones numeradas reales de db/*.sql (002-009...), en vez
    de mantener una copia a mano — la copia anterior quedó desactualizada (omitía
    006_webauthn/008_captacion/009_resume_state), dejando /admin/migrate
    respondiendo "ok" sin crear esas tablas/columnas en un proyecto nuevo.
    Excluye 000_full_setup.sql (alternativa de "esquema completo desde cero para
    un proyecto vacío", no una migración incremental) y cualquier archivo no
    numerado (schema.sql/setup.sql, variantes de referencia)."""
    db_dir = Path(__file__).parent.parent / "db"
    files = sorted(
        f for f in db_dir.glob("*.sql")
        if f.stem[:3].isdigit() and f.stem != "000_full_setup"
    )
    parts = []
    for f in files:
        parts.append(f"-- === {f.name} ===\n" + f.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


class RunMigrationsRequest(BaseModel):
    db_password: str = Field(..., description="Contraseña de BD de Supabase")


@app.post("/admin/migrate")
def run_migrations(body: RunMigrationsRequest,
                   p: Principal = Depends(get_principal)):
    """Aplica migraciones pendientes (solo admin). Requiere db_password en el body (no en la URL:
    los parámetros de query quedan en logs de acceso de proxies/hosting en texto plano)."""
    if not p.is_admin:
        raise HTTPException(403, "Solo administradores.")
    db_password = body.db_password
    try:
        import psycopg2
    except ImportError:
        raise HTTPException(500, "psycopg2 no disponible en este entorno.")

    ref = "rzdpfhflkzwylaaplgml"
    errors = []
    conn = None
    for host, port, user in [
        (f"db.{ref}.supabase.co", 5432, "postgres"),
        (f"aws-1-us-west-2.pooler.supabase.com", 6543, f"postgres.{ref}"),
    ]:
        try:
            conn = psycopg2.connect(host=host, port=port, dbname="postgres",
                                    user=user, password=db_password,
                                    sslmode="require", connect_timeout=10)
            break
        except Exception as e:
            errors.append(f"{host}:{port} → {e}")

    if not conn:
        raise HTTPException(502, f"No se pudo conectar a la BD: {' | '.join(errors)}")

    try:
        conn.autocommit = True
        cur = conn.cursor()
        migration_sql = _load_migration_sql()
        for stmt in [s.strip() for s in migration_sql.split(";") if s.strip() and not s.strip().startswith("--")]:
            try:
                cur.execute(stmt)
            except Exception as e:
                errors.append(f"SQL error: {e}")
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"Error aplicando migración: {e}")

    return {"ok": True, "applied": True, "errors": errors or None}


# ── Captación activa / Prospección / Clientes ────────────────────────────────
class BuscarMercadoRequest(BaseModel):
    producto: str = Field(..., min_length=3, description="Producto o servicio a promocionar")
    zonas_niveles: list[int] = Field(
        default=[1, 2],
        description="Niveles geográficos a buscar: 1=Cuenca, 2=Azuay, 3=Cañar/Loja/ElOro, "
                    "4=Morona/Zamora, 5=Chimborazo/Tungurahua, 6=Guayas/Manabí, "
                    "7=Pichincha, 8=Nacional"
    )
    guardar: bool = Field(True, description="Guardar los prospectos en la base de datos")


class UpdateProspectoRequest(BaseModel):
    estado: Optional[str] = Field(None, pattern="^(nuevo|contactado|interesado|no_interesado|cliente|descartado)$")
    notas: Optional[str] = None
    contacto_email: Optional[str] = None
    contacto_telefono: Optional[str] = None
    contacto_web: Optional[str] = None


class SaveClienteRequest(BaseModel):
    nombre: str = Field(..., min_length=2)
    sector: str = ""
    zona: str = ""
    contacto_web: str = ""
    contacto_email: str = ""
    contacto_telefono: str = ""
    contacto_direccion: str = ""
    notas: str = ""


class UpdateClienteRequest(BaseModel):
    nombre: Optional[str] = None
    sector: Optional[str] = None
    zona: Optional[str] = None
    contacto_web: Optional[str] = None
    contacto_email: Optional[str] = None
    contacto_telefono: Optional[str] = None
    contacto_direccion: Optional[str] = None
    notas: Optional[str] = None
    estado: Optional[str] = Field(None, pattern="^(activo|inactivo|bloqueado)$")


@app.get("/captacion/zonas")
def captacion_zonas(p: Principal = Depends(get_principal)):
    """Lista las zonas geográficas disponibles con su nivel de expansión."""
    from agents.captacion import ZONAS_ECUADOR
    return ZONAS_ECUADOR


@app.post("/captacion/buscar", status_code=202)
def buscar_mercado(req: BuscarMercadoRequest, background: BackgroundTasks,
                   p: Principal = Depends(get_principal)):
    """Lanza búsqueda de mercado en background. Devuelve job_id para polling."""
    require_module(p, "captacion")
    import threading
    _prune_captacion_jobs()
    job_id = str(uuid.uuid4())
    _captacion_jobs[job_id] = {"status": "running", "result": None, "error": None,
                               "owner_user_id": p.user_id, "is_admin_job": p.is_admin,
                               "created_at": time.time()}

    def _run():
        try:
            from agents.captacion import buscar_mercado as _buscar
            result = _buscar(
                producto=req.producto,
                zonas_solicitadas=req.zonas_niveles,
                api_key=None,
            )
            if req.guardar and result.get("prospectos") and p.user_id:
                _require_supabase()
                from db import captacion_repo
                captacion_repo.save_prospectos(
                    result["prospectos"],
                    owner_user_id=p.user_id,
                    producto=req.producto,
                )
            _captacion_jobs[job_id].update({"status": "done", "result": result, "error": None})
        except Exception as e:
            _captacion_jobs[job_id].update({"status": "failed", "result": None, "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id, "status": "running"}


_captacion_jobs: dict[str, dict] = {}
_CAPTACION_JOB_TTL_SEC = 3600  # 1h: tiempo de sobra para hacer polling del resultado


def _prune_captacion_jobs() -> None:
    """Sin esto, cada búsqueda de mercado (con docenas de prospectos de texto
    largo) quedaba en memoria del proceso para siempre — igual al problema que
    _wa_store ya resuelve para los challenges de WebAuthn, patrón reutilizado aquí."""
    now = time.time()
    for k in list(_captacion_jobs):
        if now - _captacion_jobs[k].get("created_at", now) > _CAPTACION_JOB_TTL_SEC:
            _captacion_jobs.pop(k, None)


@app.get("/captacion/buscar/{job_id}")
def captacion_job_status(job_id: str, p: Principal = Depends(get_principal)):
    """Consulta el estado de una búsqueda de mercado en curso."""
    job = _captacion_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado.")
    # Sin esto, cualquier usuario autenticado que conozca/adivine un job_id ajeno
    # podía leer los resultados de la búsqueda de mercado de otro usuario.
    if not p.is_admin and job.get("owner_user_id") != p.user_id:
        raise HTTPException(404, "Job no encontrado.")
    return {k: v for k, v in job.items()
            if k not in ("owner_user_id", "is_admin_job", "created_at")}


# ── Prospectos ────────────────────────────────────────────────────────────────
class ImportarProspectosRequest(BaseModel):
    producto: str = Field(..., min_length=2)
    prospectos: list[dict]


@app.post("/prospectos/importar", status_code=201)
def importar_prospectos(req: ImportarProspectosRequest, p: Principal = Depends(get_principal)):
    """Importa una lista de prospectos directamente (sin búsqueda web)."""
    require_module(p, "captacion")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    n = captacion_repo.save_prospectos(req.prospectos, owner_user_id=p.user_id, producto=req.producto)
    return {"ok": True, "importados": n}


@app.get("/prospectos")
def listar_prospectos(
    producto: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    zona_nivel_max: Optional[int] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    p: Principal = Depends(get_principal),
):
    require_module(p, "captacion")  # gate de módulo coherente con el resto de captación
    _require_supabase()
    from db import captacion_repo
    return captacion_repo.list_prospectos(
        owner_user_id=p.owner_filter(),
        producto=producto,
        estado=estado,
        zona_nivel_max=zona_nivel_max,
        limit=limit,
    )


@app.patch("/prospectos/{prospecto_id}")
def actualizar_prospecto(prospecto_id: str, req: UpdateProspectoRequest,
                         p: Principal = Depends(get_principal)):
    require_module(p, "captacion")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    ok = captacion_repo.update_prospecto(prospecto_id, owner_user_id=p.user_id, **fields)
    if not ok:
        raise HTTPException(404, "Prospecto no encontrado.")
    return {"ok": True}


@app.post("/prospectos/{prospecto_id}/convertir", status_code=201)
def convertir_a_cliente(prospecto_id: str, p: Principal = Depends(get_principal)):
    """Convierte un prospecto en cliente: crea una fila real en clientes, así que
    exige ambos módulos — quien solo prospecta (captacion) no debe poder crear
    clientes de facto por esta vía sin tener también el módulo clientes."""
    require_module(p, "captacion")
    require_module(p, "clientes")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    cliente_id = captacion_repo.prospecto_to_cliente(
        prospecto_id=prospecto_id, owner_user_id=p.user_id)
    if not cliente_id:
        raise HTTPException(404, "Prospecto no encontrado.")
    return {"ok": True, "cliente_id": cliente_id}


@app.delete("/prospectos/{prospecto_id}")
def eliminar_prospecto(prospecto_id: str, p: Principal = Depends(get_principal)):
    require_module(p, "captacion")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    # delete_prospecto() ya devuelve si realmente borró algo — el endpoint lo
    # ignoraba y siempre respondía ok:true, incluso con un id inexistente/ajeno.
    ok = captacion_repo.delete_prospecto(prospecto_id=prospecto_id, owner_user_id=p.user_id)
    if not ok:
        raise HTTPException(404, "Prospecto no encontrado.")
    return {"ok": True}


# ── Clientes ──────────────────────────────────────────────────────────────────
@app.get("/clientes")
def listar_clientes(
    estado: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    p: Principal = Depends(get_principal),
):
    require_module(p, "clientes")
    _require_supabase()
    from db import captacion_repo
    return captacion_repo.list_clientes(owner_user_id=p.owner_filter(), estado=estado, limit=limit)


@app.post("/clientes", status_code=201)
def crear_cliente(req: SaveClienteRequest, p: Principal = Depends(get_principal)):
    require_module(p, "clientes")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    cliente_id = captacion_repo.save_cliente(
        owner_user_id=p.user_id, **req.model_dump())
    return {"ok": True, "cliente_id": cliente_id}


@app.patch("/clientes/{cliente_id}")
def actualizar_cliente(cliente_id: str, req: UpdateClienteRequest,
                       p: Principal = Depends(get_principal)):
    require_module(p, "clientes")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    ok = captacion_repo.update_cliente(cliente_id, owner_user_id=p.user_id, **fields)
    if not ok:
        raise HTTPException(404, "Cliente no encontrado.")
    return {"ok": True}


@app.delete("/clientes/{cliente_id}")
def eliminar_cliente(cliente_id: str, p: Principal = Depends(get_principal)):
    require_module(p, "clientes")
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    ok = captacion_repo.delete_cliente(cliente_id=cliente_id, owner_user_id=p.user_id)
    if not ok:
        raise HTTPException(404, "Cliente no encontrado.")
    return {"ok": True}
