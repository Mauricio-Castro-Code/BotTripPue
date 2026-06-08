"""
CRM service — lógica de negocio para el panel de asesores.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .models import Message, SesionIA

logger = logging.getLogger(__name__)
_TZ_MX = ZoneInfo("America/Mexico_City")

_ESTADOS_SIN_BOT = {
    "requiere_humano", "en_atencion_humana", "reservando",
    "apartado", "pagado", "perdido", "finalizado",
}

ESTADOS_COMERCIALES = [
    "nuevo", "automatizado", "pregunto_info", "interesado",
    "requiere_humano", "en_atencion_humana", "reservando",
    "apartado", "pagado", "perdido", "finalizado",
]


# ─── Lead scoring conversacional ─────────────────────────────────────────────
#
# Detecta señales de compra en el texto del cliente y ajusta el score.
# Cada grupo de patrones suma/resta UNA sola vez aunque haya varias coincidencias.

_SENALES_SCORE: list[tuple[int, list[str]]] = [
    (+60, [r"quiero reservar", r"quiero apartar", r"me apunto", r"an[oó]tenme",
           r"ya me anot[oó]", r"q(uiero)? reservar"]),
    (+50, [r"comprobante", r"ya pagu[eé]", r"ya deposit[eé]", r"ya mand[eé] el pago",
           r"te mand[eé]", r"te env[ií]", r"hice la transferencia"]),
    (+40, [r"cu[aá]nto se aparta", r"cu[aá]nto de anticipo", r"\banticipo\b",
           r"cu[aá]nto hay que dar", r"enganche", r"cu[aá]nto para apartar"]),
    (+35, [r"c[oó]mo reservo", r"c[oó]mo aparto", r"c[oó]mo se reserva",
           r"para reservar", r"qu[eé] necesito para reservar", r"pasos para reservar"]),
    (+25, [r"hay cupo", r"hay lugar(es)?", r"quedan (lugares|cupos)", r"hay disponibilidad",
           r"a[ún]n hay\b", r"todav[ií]a hay"]),
    (+20, [r"somos \d+", r"vamos \d+", r"\d+\s*personas", r"para \d+\s*personas",
           r"(\d+) adultos?", r"(\d+) ni[ñn]os?"]),
    (+10, [r"\bfecha(s)?\b", r"cu[aá]ndo sale", r"qu[eé] d[ií]a", r"pr[oó]xima salida",
           r"fecha de salida", r"cu[aá]ndo es"]),
    (+10, [r"\bprecio(s)?\b", r"cu[aá]nto cuesta", r"cu[aá]nto vale", r"cu[aá]nto es\b",
           r"cu[aá]nto cobran", r"tiene costo"]),
    (+5,  [r"^info$", r"^informaci[oó]n$", r"^informes$", r"quiero info",
           r"me das info", r"^más info$"]),
    (-10, [r"^(ok\s*)?gracias\.?$", r"^muchas gracias\.?$", r"^ya era todo\.?$",
           r"^ok,?\s*gracias\.?$"]),
    (-15, [r"muy caro", r"est[aá] caro", r"es caro", r"qu[eé] caro", r"costoso",
           r"no tengo tanto", r"se me hace caro", r"muy elevado"]),
]


def calcular_delta_score(texto: str) -> int:
    texto_norm = texto.lower().strip()
    delta = 0
    for puntos, patrones in _SENALES_SCORE:
        for patron in patrones:
            if re.search(patron, texto_norm):
                delta += puntos
                break  # una sola vez por señal
    return delta


def clasificacion_score(score: int) -> tuple[str, str, str]:
    """Devuelve (etiqueta, color_texto, color_fondo)."""
    if score >= 100:
        return "🔥 Listo", "#ffffff", "#e65100"
    if score >= 61:
        return "🌶️ Caliente", "#b71c1c", "#fce4ec"
    if score >= 26:
        return "🌡️ Tibio", "#f57f17", "#fff8e1"
    return "❄️ Frío", "#1565c0", "#e3f2fd"


def aplicar_score(db: Session, sesion: SesionIA, texto: str) -> None:
    delta = calcular_delta_score(texto)
    if delta == 0:
        return
    sesion.score = max(0, (sesion.score or 0) + delta)
    db.commit()


# ─── Estado comercial automático ─────────────────────────────────────────────
#
# Solo avanza estados automáticos (nuevo→automatizado→pregunto_info→interesado→requiere_humano).
# Nunca retrocede ni toca estados gestionados por el asesor.

_ESTADOS_AUTO = {"nuevo", "automatizado", "pregunto_info", "interesado", "requiere_humano"}

_ORDEN_ESTADO = {
    "nuevo":           0,
    "automatizado":    1,
    "pregunto_info":   2,
    "interesado":      3,
    "requiere_humano": 4,
}


def sincronizar_estado_comercial(db: Session, sesion: SesionIA) -> None:
    estado_actual = sesion.estado_comercial or "nuevo"

    # No interferir con estados que ya maneja el asesor manualmente
    if estado_actual not in _ESTADOS_AUTO:
        return

    score = sesion.score or 0

    if score >= 100:
        target = "requiere_humano"
    elif score >= 61:
        target = "interesado"
    elif score >= 26:
        target = "pregunto_info"
    else:
        target = "automatizado"  # primer mensaje activa el estado mínimo

    # Solo avanzar — nunca retroceder aunque el score baje
    if _ORDEN_ESTADO.get(target, 0) <= _ORDEN_ESTADO.get(estado_actual, 0):
        return

    sesion.estado_comercial = target
    if target == "requiere_humano":
        sesion.requiere_humano = True

    db.commit()


# ─── Guardar mensajes ─────────────────────────────────────────────────────────

def guardar_mensaje_entrante(
    db: Session,
    sesion: SesionIA,
    body: str,
    whatsapp_message_id: str | None = None,
) -> None:
    msg = Message(
        sesion_id=sesion.id,
        telefono=sesion.telefono_cliente,
        canal=sesion.canal or "whatsapp",
        direccion="inbound",
        sender_type="cliente",
        body=body,
        whatsapp_message_id=whatsapp_message_id,
        status="received",
    )
    db.add(msg)
    db.commit()


def guardar_mensaje_saliente(
    db: Session,
    sesion: SesionIA,
    body: str,
    sender_type: str = "bot",
    sender_nombre: str | None = None,
) -> None:
    msg = Message(
        sesion_id=sesion.id,
        telefono=sesion.telefono_cliente,
        canal=sesion.canal or "whatsapp",
        direccion="outbound",
        sender_type=sender_type,
        sender_nombre=sender_nombre,
        body=body,
        status="sent",
    )
    db.add(msg)
    db.commit()


# ─── Control de bot ───────────────────────────────────────────────────────────

def debe_bot_responder(sesion: SesionIA) -> bool:
    if sesion.asesor_activo:
        return False
    estado = sesion.estado_comercial or "nuevo"
    if estado in _ESTADOS_SIN_BOT:
        return False
    return True


def pausar_bot(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = True
    sesion.asesor_desde = datetime.now(tz=_TZ_MX)
    db.commit()


def reactivar_bot(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = False
    sesion.asesor_desde = None
    db.commit()


# ─── Gestión de asesores ──────────────────────────────────────────────────────

def tomar_chat(db: Session, sesion: SesionIA, asesor_nombre: str) -> None:
    if (
        sesion.asesor_activo
        and sesion.asesor_nombre
        and sesion.asesor_nombre != asesor_nombre
    ):
        raise ValueError(f"Chat ya tomado por {sesion.asesor_nombre}")
    sesion.asesor_activo = True
    sesion.asesor_desde = datetime.now(tz=_TZ_MX)
    sesion.asesor_nombre = asesor_nombre
    sesion.estado_comercial = "en_atencion_humana"
    sesion.requiere_humano = False
    db.commit()


def liberar_chat(db: Session, sesion: SesionIA) -> None:
    sesion.asesor_activo = False
    sesion.asesor_desde = None
    sesion.asesor_nombre = None
    if (sesion.estado_comercial or "nuevo") == "en_atencion_humana":
        sesion.estado_comercial = "automatizado"
    db.commit()


# ─── Estados y metadatos ──────────────────────────────────────────────────────

def actualizar_estado_comercial(db: Session, sesion: SesionIA, estado: str) -> None:
    if estado not in ESTADOS_COMERCIALES:
        raise ValueError(f"Estado inválido: {estado}")
    sesion.estado_comercial = estado
    if estado == "requiere_humano":
        sesion.requiere_humano = True
    if estado in ("perdido", "finalizado"):
        sesion.sesion_cerrada = True
    db.commit()


def actualizar_score(db: Session, sesion: SesionIA, score: int) -> None:
    sesion.score = max(0, score)  # sin techo — 100+ = Listo para asesor
    db.commit()


def agregar_nota(db: Session, sesion: SesionIA, nota: str) -> None:
    ahora = datetime.now(tz=_TZ_MX).strftime("%d/%m %H:%M")
    nueva_linea = f"[{ahora}] {nota.strip()}"
    sesion.notas_internas = (
        (sesion.notas_internas + "\n" + nueva_linea)
        if sesion.notas_internas
        else nueva_linea
    )
    db.commit()


# ─── Envío manual desde CRM ──────────────────────────────────────────────────

def enviar_mensaje_asesor(
    db: Session, sesion: SesionIA, asesor_nombre: str, body: str
) -> None:
    from .services import enviar_texto  # import local para evitar circular
    canal = sesion.canal or "whatsapp"
    enviar_texto(canal, sesion.telefono_cliente, body)
    guardar_mensaje_saliente(db, sesion, body, sender_type="asesor", sender_nombre=asesor_nombre)
    sesion.ultimo_mensaje = datetime.now(tz=_TZ_MX)
    db.commit()
