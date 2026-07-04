"""API HTTP de agente_map.

Auth multiusuario:
- `POST /auth/register` (auto-registro; 1er usuario = admin aprobado, resto pending)
- `POST /auth/login` → token firmado (Bearer)
- La X-API-Key maestra (AGENTE_MAP_API_KEY) entra como ADMIN bootstrap.
- Cada usuario ve SUS entregables; el admin ve todos.

Endpoints de datos: /modules, /doc_types, /extract, /propuestas[...],
/propuestas/{id}/(markdown|word|excel|retry|reviews).

Trabajos largos: el pipeline corre en BackgroundTasks; el estado se persiste en
sessions.status (pending → running → approved/failed).
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import config
import api.auth as auth_lib
from core.pipeline import run_pipeline, run_scouting
from db import queries
from db import users as users_repo
from models.doc_types import list_doc_types, list_modules
from models.schemas import DocumentBrief, FinancialPackage, ProjectSession


app = FastAPI(
    title="agente_map API",
    description="Pipeline multiagente multiusuario de entregables de alto nivel.",
    version="0.3.0",
)

API_KEY_ENV = "AGENTE_MAP_API_KEY"


# ── Auth / principal ────────────────────────────────────────────────────────
class Principal:
    def __init__(self, user_id: Optional[str], role: str,
                 email: Optional[str] = None, name: Optional[str] = None,
                 master: bool = False):
        self.user_id = user_id
        self.role = role
        self.email = email
        self.name = name
        self.master = master

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def owner_filter(self) -> Optional[str]:
        """None → ve todo (admin/maestra). Si no, restringe a su user_id."""
        return None if self.is_admin else self.user_id


def get_principal(authorization: Optional[str] = Header(None),
                  x_api_key: Optional[str] = Header(None)) -> Principal:
    master = os.getenv(API_KEY_ENV, "")
    if x_api_key and master and x_api_key == master:
        return Principal(None, "admin", name="Administrador", master=True)
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
                return Principal(uid, u.get("role", "user"), email=u.get("email"), name=u.get("name"))
            # Sin BD de usuarios configurada no hay estado que revalidar: confía en el token.
            return Principal(uid, payload.get("role", "user"))
    raise HTTPException(401, "No autenticado. Inicia sesión.")


def require_admin(p: Principal) -> None:
    if not p.is_admin:
        raise HTTPException(403, "Requiere rol administrador.")


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


class CreateProposalRequest(BaseModel):
    user_input: str = Field(..., min_length=10, description="Idea, tema o propuesta a procesar.")
    mode: str = Field("text", pattern="^(search|text|file|url)$")
    doc_type_key: Optional[str] = Field("auto", description="'auto' o una clave de DOC_TYPES.")
    template_text: str = ""
    support_docs: list[tuple[str, str]] = Field(default_factory=list)


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


_DOC_TYPE_LABELS: dict[str, str] = {
    "propuesta":          "Propuesta",
    "articulo_cientifico":"Artículo científico",
    "tesis":              "Tesis",
    "tdr":                "TDR",
    "informe":            "Informe",
    "peer_review":        "Revisión de pares",
    "legal_tecnico":      "Doc. legal/técnico",
    "auto":               "Documento",
}


def _auto_title(row: dict, is_scouting: bool = False) -> str:
    """Genera un título descriptivo cuando el pipeline aún no tiene uno."""
    import datetime

    created_raw = row.get("created_at")
    try:
        if created_raw:
            dt = datetime.datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            fecha = dt.strftime("%-d %b %Y") if hasattr(dt, "strftime") else str(dt)[:10]
        else:
            fecha = datetime.date.today().strftime("%-d %b %Y")
    except Exception:
        fecha = str(created_raw or "")[:10]

    # Tipo de documento
    key = (row.get("doc_type_key") or "auto").lower()
    if is_scouting:
        tipo = "Búsqueda de oportunidades"
    else:
        tipo = _DOC_TYPE_LABELS.get(key, key.replace("_", " ").title())

    # Extracto del input
    inp = (row.get("user_input") or "").strip()
    if inp:
        # Tomar las primeras palabras hasta ~55 chars, cortar en espacio
        if len(inp) > 55:
            cut = inp[:55]
            space = cut.rfind(" ")
            inp = (cut[:space] if space > 20 else cut) + "…"
        # Limpiar saltos de línea
        inp = inp.replace("\n", " ").replace("\r", "")
        return f"{tipo} — {inp} ({fecha})"
    return f"{tipo} ({fecha})"


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


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "agente_map", "version": app.version}


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
    users_repo.touch_login(u["id"])
    token = auth_lib.make_token(u["id"], u.get("role", "user"))
    return {"token": token, "user": users_repo.public_view(u)}


@app.get("/auth/me")
def me(p: Principal = Depends(get_principal)):
    if p.master:
        return {"id": None, "name": "Administrador (clave maestra)", "email": None,
                "role": "admin", "status": "approved"}
    u = _db(users_repo.get_by_id, p.user_id) if p.user_id else None
    if not u:
        raise HTTPException(401, "Sesión inválida.")
    return users_repo.public_view(u)


# ── Gestión de usuarios (solo admin) ─────────────────────────────────────────
@app.get("/users")
def list_users_ep(status: Optional[str] = Query(None), p: Principal = Depends(get_principal)):
    require_admin(p)
    _require_supabase()
    return _db(users_repo.list_users, status)


@app.post("/users/{user_id}/approve")
def approve_user(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    users_repo.set_status(user_id, "approved")
    return {"ok": True, "status": "approved"}


@app.post("/users/{user_id}/reject")
def reject_user(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    users_repo.set_status(user_id, "rejected")
    return {"ok": True, "status": "rejected"}


@app.post("/users/{user_id}/make-admin")
def make_admin(user_id: str, p: Principal = Depends(get_principal)):
    require_admin(p)
    users_repo.set_role(user_id, "admin")
    return {"ok": True, "role": "admin"}


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
    for f in files:
        data = await f.read()
        if not data:
            raise HTTPException(400, f"'{f.filename}' está vacío.")
        if len(data) > MAX_EXTRACT_BYTES:
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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    session_id = uuid.uuid4().hex[:8]
    owner = p.user_id  # None si es la clave maestra

    def _run():
        try:
            run_pipeline(
                user_input=req.user_input, mode=req.mode,
                doc_type_key=(req.doc_type_key or "auto"),
                template_text=req.template_text, support_docs=req.support_docs,
                session_id=session_id, owner_user_id=owner,
            )
        except Exception:
            pass

    background.add_task(_run)
    return CreateProposalResponse(session_id=session_id, status="pending")


# ── Scouting: buscar → reporte con calificación → elegir → generar ───────────
@app.post("/buscar", response_model=CreateProposalResponse, status_code=202)
def buscar_oportunidades(req: ScoutRequest, background: BackgroundTasks,
                         p: Principal = Depends(get_principal)):
    """Detecta el TOP-N de oportunidades y arma un reporte con calificación
    ponderada (NO genera propuestas). Luego se eligen con POST /generar."""
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    session_id = uuid.uuid4().hex[:8]
    owner = p.user_id

    def _run():
        try:
            run_scouting(user_input=req.user_input, session_id=session_id, owner_user_id=owner)
        except Exception:
            pass

    background.add_task(_run)
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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    row = _owned_row(session_id, p)
    analysis = row.get("analysis") or {}
    opps = analysis.get("alternatives") or []
    if not opps:
        raise HTTPException(409, "Esta sesión no tiene oportunidades para generar.")
    owner = row.get("owner_user_id")

    created: list[CreateProposalResponse] = []
    for idx in req.selected:
        if not (0 <= idx < len(opps)):
            continue
        opp = opps[idx]
        funder = opp.get("funder") or {}
        title = opp.get("title") or funder.get("name") or row.get("user_input")
        url = funder.get("url") or ""
        user_input = f"{title} — {funder.get('name', '')}".strip(" —")
        mode = "url" if url.startswith("http") else "search"
        if mode == "url":
            user_input = f"{user_input}\n{url}"
        new_id = uuid.uuid4().hex[:8]

        def _run(new_id=new_id, user_input=user_input, mode=mode, opp=opp):
            try:
                run_pipeline(user_input=user_input, mode=mode, doc_type_key="propuesta",
                             session_id=new_id, owner_user_id=owner, seed_opportunity=opp)
            except Exception:
                pass

        background.add_task(_run)
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
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY no configurada en el servidor")
    user_input = row["user_input"]
    mode = row.get("input_mode") or "text"
    doc_type_key = row.get("doc_type_key") or "auto"
    template_text = row.get("template_text") or ""
    support_docs = [(d.get("name"), d.get("text")) for d in (row.get("support_docs") or [])]
    owner = row.get("owner_user_id")

    def _run():
        try:
            run_pipeline(user_input=user_input, mode=mode, doc_type_key=doc_type_key,
                         template_text=template_text, support_docs=support_docs,
                         session_id=session_id, owner_user_id=owner)
        except Exception:
            pass

    background.add_task(_run)
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
    import threading
    job_id = str(uuid.uuid4())
    _captacion_jobs[job_id] = {"status": "running", "result": None, "error": None,
                               "owner_user_id": p.user_id, "is_admin_job": p.is_admin}

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
    return {k: v for k, v in job.items() if k not in ("owner_user_id", "is_admin_job")}


# ── Prospectos ────────────────────────────────────────────────────────────────
class ImportarProspectosRequest(BaseModel):
    producto: str = Field(..., min_length=2)
    prospectos: list[dict]


@app.post("/prospectos/importar", status_code=201)
def importar_prospectos(req: ImportarProspectosRequest, p: Principal = Depends(get_principal)):
    """Importa una lista de prospectos directamente (sin búsqueda web)."""
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
    """Convierte un prospecto en cliente."""
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
    _require_supabase()
    from db import captacion_repo
    return captacion_repo.list_clientes(owner_user_id=p.owner_filter(), estado=estado, limit=limit)


@app.post("/clientes", status_code=201)
def crear_cliente(req: SaveClienteRequest, p: Principal = Depends(get_principal)):
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
    _require_supabase()
    if not p.user_id:
        raise HTTPException(403, "Requiere cuenta de usuario.")
    from db import captacion_repo
    ok = captacion_repo.delete_cliente(cliente_id=cliente_id, owner_user_id=p.user_id)
    if not ok:
        raise HTTPException(404, "Cliente no encontrado.")
    return {"ok": True}
