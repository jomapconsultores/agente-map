# ------------------------------------------------------------
# Desarrollado por Marco Antonio Posligua San Martín
# ------------------------------------------------------------
"""Envío de correo por SMTP para las entregas automáticas (vigía de convocatorias).

Se usa SMTP y no una API de terceros a propósito: el envío tiene que funcionar hoy,
con una cuenta que ya existe, sin verificar un dominio ni esperar la aprobación de
ningún proveedor. Con Gmail basta una contraseña de aplicación.

Variables (todas con prefijo VIGIA_ para no chocar con el correo transaccional):
    VIGIA_SMTP_HOST   por defecto smtp.gmail.com
    VIGIA_SMTP_PORT   465 (SSL) o 587 (STARTTLS); por defecto 465
    VIGIA_SMTP_USER   cuenta remitente
    VIGIA_SMTP_PASS   contraseña de aplicación (NO la contraseña normal de la cuenta)
    VIGIA_MAIL_FROM   remitente visible; por defecto VIGIA_SMTP_USER
    VIGIA_MAIL_TO     destinatarios separados por coma; por defecto VIGIA_SMTP_USER
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate


class MailNotConfigured(RuntimeError):
    """Falta configuración SMTP. El llamador decide si eso es fatal o no."""


def _cfg() -> dict:
    user = os.getenv("VIGIA_SMTP_USER", "").strip()
    password = os.getenv("VIGIA_SMTP_PASS", "").strip()
    to_raw = os.getenv("VIGIA_MAIL_TO", "").strip() or user
    return {
        "host": os.getenv("VIGIA_SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.getenv("VIGIA_SMTP_PORT", "465") or 465),
        "user": user,
        "password": password,
        "from": os.getenv("VIGIA_MAIL_FROM", "").strip() or user,
        "to": [a.strip() for a in to_raw.split(",") if a.strip()],
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["user"] and c["password"] and c["to"])


def describe() -> str:
    """Estado de la configuración, sin revelar la contraseña."""
    c = _cfg()
    if not c["user"]:
        return "SMTP sin configurar: falta VIGIA_SMTP_USER"
    if not c["password"]:
        return f"SMTP incompleto: {c['user']} sin VIGIA_SMTP_PASS"
    return f"SMTP listo: {c['user']} → {', '.join(c['to'])} vía {c['host']}:{c['port']}"


def send_html(subject: str, html: str, text: str = "", *, sender_name: str = "Vigía MAP") -> None:
    """Envía un correo HTML con alternativa en texto plano.

    Lanza MailNotConfigured si faltan credenciales, y las excepciones de smtplib
    tal cual si el envío falla: quien llama debe poder distinguir "no configurado"
    de "configurado pero rechazado".
    """
    c = _cfg()
    if not (c["user"] and c["password"] and c["to"]):
        raise MailNotConfigured(describe())

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, c["from"]))
    msg["To"] = ", ".join(c["to"])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text or "Este mensaje requiere un lector con HTML.")
    msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if c["port"] == 465:
        with smtplib.SMTP_SSL(c["host"], c["port"], context=context, timeout=30) as s:
            s.login(c["user"], c["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(c["host"], c["port"], timeout=30) as s:
            s.starttls(context=context)
            s.login(c["user"], c["password"])
            s.send_message(msg)


if __name__ == "__main__":  # python -m utils.mailer  → prueba de envío
    print(describe())
    if is_configured():
        send_html(
            "Prueba del vigía MAP",
            "<p>Si lees esto, el canal de correo del vigía funciona.</p>",
            "Si lees esto, el canal de correo del vigía funciona.",
        )
        print("correo de prueba enviado")
