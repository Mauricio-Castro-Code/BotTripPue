"""
Capa de servicios — lógica del bot conversacional.

Responsabilidades:
  - Sesiones con máquina de estados (menu / chat_*)
  - Respuestas con OpenAI (chat completion + function calling)
  - Envío de mensajes a WhatsApp (Meta API)
  - Registro de leads
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI
from urllib.parse import quote
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import settings
from .models import Lead, SesionIA
from data.paquetes import (
    METODOS_PAGO,
    REQUISITOS,
    get_contexto_paquetes,
    get_resumen_nacionales,
    get_resumen_internacionales,
)

logger = logging.getLogger(__name__)
_TZ_MX = ZoneInfo("America/Mexico_City")
_openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)

META_API_BASE = "https://graph.facebook.com/v25.0"
_MAX_HISTORIAL = 20

NUMERO_ASESOR_NACIONAL       = "522228126321"   # Puebla Travel Trips
NUMERO_ASESOR_INTERNACIONAL  = "522225207938"   # LibertYa
 
# ─── Menú ─────────────────────────────────────────────────────────────────────

_MENU_OPCIONES = (
    "¿En qué te podemos ayudar hoy?\n\n"
    "1️⃣ Viajes nacionales *(Puebla Travel Trips)*\n"
    "2️⃣ Viajes internacionales *(LibertYa)*\n"
    "3️⃣ Ya aparté un viaje / soy cliente\n"
    "4️⃣ Pagos, requisitos y más información\n\n"
    "Responde con el número, la categoría o escribe directamente tu duda 👇🏼"
)

_MENSAJE_BIENVENIDA = (
    "¡Hola, viajero! 🌎✈️ Bienvenido a *LibertYa*, "
    "la nueva línea de viajes internacionales de la familia "
    "*Puebla Travel Trips*.\n\n"
    "Seguimos siendo el mismo equipo y la misma confianza que ya conoces, "
    "ahora listos para acompañarte a descubrir el mundo. 🧳🌍\n\n"
    "Soy tu asistente virtual. ¿Qué destino internacional te interesa conocer?\n\n"
    + _MENU_OPCIONES
)

_MENSAJE_DESPEDIDA = (
    "¡Hasta luego, viajero! 👋✈️\n\n"
    "Fue un placer atenderte. Cuando quieras planear tu próxima aventura, "
    "aquí estaremos. ¡Que tengas un excelente día! 🌟\n\n"
    "*LibertYa* — Tu agencia de confianza "
)

_RESPUESTAS_FIJAS: dict[str, str] = {
    "3": (
        "¡Claro, con gusto te ayudo! 😊\n\n"
        "¿Sobre qué viaje tienes una pregunta? Dime el destino o nombre del viaje que ya apartaste. ✈️"
    ),
    "4": (
        METODOS_PAGO
        + "\n\n"
        + REQUISITOS
        + "\n\n"
        "📌 *¿Viaje grupal?* (empresa, boda, XV años, amigos)\n"
        "Cuéntanos destino, número de personas y fechas tentativas y te preparamos una propuesta personalizada. 🎉"
    ),
}

# Estados que continúan con OpenAI después de la respuesta inicial
_ESTADOS_CON_IA = {
    "chat_nacional", "chat_internacional",
    "chat_cliente", "chat_cliente_nacional", "chat_cliente_internacional",
    "chat_grupo",
}

# ─── Sistema prompt OpenAI ────────────────────────────────────────────────────

_SISTEMA_BASE = """
Eres un asistente de WhatsApp de la familia de agencias *Puebla Travel Trips* (viajes nacionales) y *LibertYa* (viajes internacionales), con base en Puebla, México.
Responde siempre en español, de forma amable y concisa (máximo 3 párrafos cortos).

REGLA ABSOLUTA — INFORMACIÓN DE PAQUETES:
Cuando un cliente pregunte por un destino o paquete, busca en la sección PAQUETES DISPONIBLES la entrada que más se parezca a lo que pide.
- Si la encuentras: copia EXACTAMENTE los datos del catálogo (precio, fechas, duración, transporte, incluye, reserva). No parafrasees ni inventes datos distintos.
- Si NO la encuentras: responde amablemente que ese tour no está disponible aún, y ofrece que un asesor puede cotizárselo.
NUNCA inventes precios, fechas, transportes ni contenidos de un paquete que no estén literalmente en PAQUETES DISPONIBLES.

IMPORTANTE: Nunca incluyas links de contacto ni menciones al asesor en tu respuesta. Solo proporciona información del destino o paquete. Los botones de acción los maneja el sistema automáticamente.
"""

_CONTEXTO_POR_ESTADO: dict[str, str] = {
    "chat_nacional": (
        "El cliente está interesado en viajes NACIONALES (Puebla Travel Trips). "
        "Ya se le preguntó qué tipo de viaje le interesa. Sigue estas reglas:\n"
        "1. Si menciona una categoría (playa, pueblo mágico, aventura, montaña, etc.) → muéstrale ÚNICAMENTE los destinos del catálogo que encajen, en lista breve con destino, fecha y precio.\n"
        "2. Si dice 'ver todos' o similar → muestra todos los destinos nacionales disponibles.\n"
        "3. Si menciona un destino específico que SÍ está en el catálogo → da todos los detalles: fechas, precio, duración, qué incluye, anticipo requerido.\n"
        "4. Si menciona un destino que NO está en el catálogo → dile amablemente que por el momento no tenemos ese paquete, pero que un asesor puede cotizárselo.\n"
        "Responde SOLO con información de destinos. No incluyas links ni menciones al asesor."
    ),
    "chat_internacional": (
        "El cliente está interesado en viajes INTERNACIONALES (LibertYa). "
        "Ya se le preguntó qué continente o región le interesa. Sigue estas reglas estrictamente:\n"
        "1. Si menciona un continente o región (Europa, América, Asia, Caribe, etc.) → muéstrale ÚNICAMENTE los destinos de esa región del catálogo, en formato de lista breve con destino y precio.\n"
        "2. Si dice 'ver todos', 'todos', 'los más populares', 'los más económicos' o similar → muéstrale una selección de máximo 10 destinos variados del catálogo ordenados de menor a mayor precio.\n"
        "3. Si menciona un país o destino específico que SÍ está en el catálogo → da todos los detalles: fechas, precio, duración, qué incluye, anticipo requerido. Recuérdales verificar vigencia del pasaporte si aplica.\n"
        "4. Si menciona un país que NO está en el catálogo → dile amablemente que por el momento no tenemos ese tour disponible, pero que un asesor puede cotizárselo con gusto.\n"
        "Responde SOLO con información de destinos. No incluyas links ni menciones al asesor."
    ),
    "chat_cliente": (
        "El cliente ya tiene una reserva y mencionó un viaje. Tu tarea en este turno es identificar "
        "si el viaje es NACIONAL (dentro de México) o INTERNACIONAL (fuera de México) y responder su duda. "
        "Usa el campo tipo_viaje para indicar 'nacional' o 'internacional' según corresponda. "
        "Si la duda es básica y el paquete está en el catálogo, respóndela. "
        "No incluyas links ni menciones al asesor."
    ),
    "chat_cliente_nacional": (
        "El cliente tiene una reserva en un viaje NACIONAL (Puebla Travel Trips). "
        "Responde sus dudas usando únicamente los datos del catálogo: fechas, precio, duración, qué incluye, anticipo. "
        "Si la pregunta es sobre procesos internos, cambios de reserva o pagos específicos que no están en el catálogo, "
        "indícale amablemente que un asesor lo ayudará mejor. No incluyas links ni menciones al asesor."
    ),
    "chat_cliente_internacional": (
        "El cliente tiene una reserva en un viaje INTERNACIONAL (LibertYa). "
        "Responde sus dudas usando únicamente los datos del catálogo: fechas, precio, duración, qué incluye, anticipo. "
        "Si la pregunta es sobre procesos internos, cambios de reserva o pagos específicos que no están en el catálogo, "
        "indícale amablemente que un asesor lo ayudará mejor. No incluyas links ni menciones al asesor."
    ),
}

# ─── Clasificador de intención (OpenAI) ──────────────────────────────────────

_PROMPT_INTENT = """\
Eres el clasificador de intención del bot de WhatsApp de LibertYa, una agencia de viajes.

El cliente puede escribir de cualquier forma: con errores, informal, emojis, oraciones largas o muy cortas.

Clasifica el mensaje en UNA de estas intenciones:

saludo        → El cliente saluda o quiere reiniciar (hola, buenos días, inicio, start…)
despedida     → Se despide o ya terminó (adiós, bye, hasta luego, es todo, gracias fue todo…)
no_interesado → Indica que no le interesa o quiere cancelar (no me interesa, no gracias, ya no quiero, cancelar…)
menu_1        → Pregunta por viajes NACIONALES (dentro de México: playas, pueblos mágicos, Cancún, CDMX…)
menu_2        → Pregunta por viajes INTERNACIONALES (fuera de México: Europa, Caribe, Asia, cruceros…)
menu_3        → Ya apartó o compró un viaje, tiene una reserva, es cliente activo
menu_4        → Pregunta sobre precios, pagos, requisitos, documentos, grupos, bodas, XV años
continuar     → Cualquier otra cosa: pregunta específica de un destino, comentario, duda, algo que vio en redes

Estado actual de la conversación: {estado}

REGLA CRÍTICA: Si el estado es chat_nacional, chat_internacional, chat_cliente, chat_cliente_nacional, chat_cliente_internacional o chat_grupo,
y el mensaje es una pregunta, comentario o duda sobre un destino o viaje (aunque contenga
palabras cortas como "hi" o "hey" dentro de una oración en español), devuelve "continuar".

Devuelve ÚNICAMENTE una de estas palabras exactas: saludo, despedida, no_interesado, menu_1, menu_2, menu_3, menu_4, continuar\
"""

_VALIDAS_INTENT = frozenset({
    "saludo", "despedida", "no_interesado",
    "menu_1", "menu_2", "menu_3", "menu_4",
    "continuar",
})


_MENU_DIRECTO = {"1": "menu_1", "2": "menu_2", "3": "menu_3", "4": "menu_4"}


def clasificar_intencion(texto: str, estado: str) -> str:
    if estado == "menu" and texto.strip() in _MENU_DIRECTO:
        return _MENU_DIRECTO[texto.strip()]

    try:
        response = _openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _PROMPT_INTENT.format(estado=estado)},
                {"role": "user", "content": texto},
            ],
            max_tokens=10,
            temperature=0,
        )
        resultado = response.choices[0].message.content.strip().lower()
        return resultado if resultado in _VALIDAS_INTENT else "continuar"
    except Exception as exc:
        logger.error("Error clasificando intención: %s", exc)
        return "continuar"


_ESTADOS_NACIONALES = {"chat_nacional", "chat_cliente_nacional"}
_ESTADOS_INTERNACIONALES = {"chat_internacional", "chat_cliente_internacional", "chat_cliente"}


def _numero_asesor(estado: str) -> str:
    return NUMERO_ASESOR_NACIONAL if estado in _ESTADOS_NACIONALES else NUMERO_ASESOR_INTERNACIONAL


def get_ultima_duda(historial: list) -> str | None:
    for msg in reversed(historial):
        if msg.get("role") == "user":
            return msg.get("content")
    return None


def _mensaje_derivar(
    estado: str,
    viajes_interes: list[str] | None = None,
    duda_cliente: str | None = None,
) -> str:
    if estado in _ESTADOS_NACIONALES:
        nombre = "Puebla Travel Trips"
        numero = NUMERO_ASESOR_NACIONAL
    else:
        nombre = "LibertYa"
        numero = NUMERO_ASESOR_INTERNACIONAL

    if estado in ("chat_cliente_nacional", "chat_cliente_internacional") and duda_cliente:
        texto_precar = f"Hola, tengo una reserva y tengo la siguiente duda: {duda_cliente}"
        link = f"https://wa.me/{numero}?text={quote(texto_precar)}"
    elif viajes_interes:
        if len(viajes_interes) == 1:
            detalle = viajes_interes[0]
        else:
            detalle = ", ".join(viajes_interes[:-1]) + " y " + viajes_interes[-1]
        texto_precar = f"Hola, estoy interesado en reservar: {detalle}"
        link = f"https://wa.me/{numero}?text={quote(texto_precar)}"
    else:
        link = f"https://wa.me/{numero}"

    return (
        f"¡Con gusto! 😊 Escríbele directamente al asesor de *{nombre}*:\n\n"
        f"👉 {link}\n\n"
        "Te atenderá personalmente a la brevedad. ✈️"
    )


_ESTADO_POR_INTENCION: dict[str, str] = {
    "menu_1": "chat_nacional",
    "menu_2": "chat_internacional",
    "menu_3": "chat_cliente",
    "menu_4": "chat_cliente",
}

_OPCION_POR_INTENCION: dict[str, str] = {
    "menu_1": "1",
    "menu_2": "2",
    "menu_3": "3",
    "menu_4": "4",
}


# ─── Estado de sesión ─────────────────────────────────────────────────────────

def get_estado(historial: list) -> str:
    for msg in historial:
        if msg.get("role") == "meta":
            return msg.get("estado", "menu")
    return "menu"


def get_viajes_interes(historial: list) -> list[str]:
    for msg in historial:
        if msg.get("role") == "meta":
            return list(msg.get("viajes_interes", []))
    return []


def set_estado(mensajes: list, estado: str, viajes_interes: list[str] | None = None) -> list:
    sin_meta = [m for m in mensajes if m.get("role") != "meta"]
    meta: dict = {"role": "meta", "estado": estado}
    if viajes_interes:
        meta["viajes_interes"] = viajes_interes
    return [meta] + sin_meta


def mensajes_openai(historial: list) -> list:
    return [m for m in historial if m.get("role") in ("user", "assistant")]


# ─── Sesión ───────────────────────────────────────────────────────────────────

def obtener_o_crear_sesion(db: Session, telefono: str) -> SesionIA:
    sesion = db.query(SesionIA).filter(SesionIA.telefono_cliente == telefono).first()
    if not sesion:
        sesion = SesionIA(telefono_cliente=telefono, historial=[])
        db.add(sesion)
        db.commit()
        db.refresh(sesion)
    return sesion


def guardar_historial(db: Session, sesion: SesionIA, historial: list) -> None:
    sesion.historial = historial
    sesion.ultimo_mensaje = datetime.now(tz=_TZ_MX)
    db.commit()


def preparar_historial(mensajes: list, mensaje_usuario: str) -> list:
    mensajes = list(mensajes) + [{"role": "user", "content": mensaje_usuario}]
    if len(mensajes) > _MAX_HISTORIAL:
        mensajes = mensajes[-_MAX_HISTORIAL:]
    return mensajes


# ─── Lead ─────────────────────────────────────────────────────────────────────

def guardar_o_actualizar_lead(
    db: Session,
    telefono: str,
    nombre: str | None = None,
    destino: str | None = None,
    estatus: str | None = None,
) -> None:
    lead = db.query(Lead).filter(Lead.telefono == telefono).first()
    if not lead:
        lead = Lead(telefono=telefono, estatus="nuevo")
        db.add(lead)
    if nombre and not lead.nombre:
        lead.nombre = nombre
    if destino:
        lead.destino_interes = destino
    if estatus:
        lead.estatus = estatus
    db.commit()


# ─── OpenAI ───────────────────────────────────────────────────────────────────

_FUNCION_RESPONDER = {
    "name": "responder_cliente",
    "description": "Genera la respuesta para el cliente y extrae información relevante.",
    "parameters": {
        "type": "object",
        "properties": {
            "respuesta": {
                "type": "string",
                "description": "Mensaje de respuesta para enviar al cliente por WhatsApp.",
            },
            "nombre_detectado": {
                "type": "string",
                "description": "Nombre del cliente si lo mencionó. Null si no.",
            },
            "destino_detectado": {
                "type": "string",
                "description": "Destino de viaje que le interesa si lo mencionó. Null si no.",
            },
            "viaje_interes": {
                "type": "string",
                "description": (
                    "Resumen conciso del viaje ESPECÍFICO que le interesa al cliente, "
                    "solo si preguntó por un destino concreto del catálogo. "
                    "Formato: 'Destino (salida DD MMM, $precio)'. "
                    "Null si solo está explorando o preguntando de forma general."
                ),
            },
            "tipo_viaje": {
                "type": "string",
                "enum": ["nacional", "internacional"],
                "description": (
                    "Tipo del viaje que menciona el cliente. "
                    "'nacional' si el destino es dentro de México. "
                    "'internacional' si el destino es fuera de México. "
                    "Obligatorio cuando el estado sea 'chat_cliente' y el cliente mencione un viaje. "
                    "Null si no se puede determinar."
                ),
            },
        },
        "required": ["respuesta"],
    },
}


_PREGUNTA_NACIONAL = (
    "¡Perfecto! 🇲🇽✈️ Tenemos salidas a distintos destinos dentro de México.\n\n"
    "¿Qué tipo de viaje te interesa?\n\n"
    "🏖️ *Playa* — Huatulco, Riviera Maya, Puerto Vallarta...\n"
    "🏘️ *Pueblo Mágico* — Destinos coloniales y con encanto\n"
    "🏔️ *Aventura / Naturaleza* — Ecoturismo, senderismo, cascadas...\n"
    "⭐ *Ver todos los disponibles*\n\n"
    "¡O dime directamente el destino que tienes en mente! 😊"
)

_PREGUNTA_CONTINENTE = (
    "¡Excelente elección! 🌎✈️ Tenemos destinos en varios continentes y regiones.\n\n"
    "¿Qué región te llama más la atención?\n\n"
    "🌍 *Europa* — España, Francia, Italia, Alemania, Marruecos...\n"
    "🌎 *América* — Colombia, Costa Rica, Panamá, Patagonia, Perú...\n"
    "🌏 *Asia / Oceanía* — Japón, Tailandia, India, Australia...\n"
    "🏝️ *Caribe* — Jamaica, Punta Cana, Cruceros...\n"
    "⭐ *Ver los más populares* — Te muestro una selección de los más solicitados\n\n"
    "¡O dime directamente el destino o país que tienes en mente! 😊"
)


def get_respuesta_opcion(opcion: str) -> str:
    if opcion == "1":
        return get_resumen_nacionales()
    if opcion == "2":
        return get_resumen_internacionales()
    return _RESPUESTAS_FIJAS.get(opcion, "")


def generar_respuesta_ia(mensajes: list, estado: str = "menu") -> tuple[str, str | None, str | None, str | None, str | None]:
    """Llama a OpenAI. Devuelve (respuesta, nombre_detectado, destino_detectado, viaje_interes, tipo_viaje)."""
    contexto = _CONTEXTO_POR_ESTADO.get(estado, "")
    paquetes_actuales = get_contexto_paquetes()
    sistema = (
        _SISTEMA_BASE
        + f"\nPAQUETES DISPONIBLES:\n{paquetes_actuales}"
        + (f"\n\nCONTEXTO ACTUAL: {contexto}" if contexto else "")
    )

    messages = [{"role": "system", "content": sistema}] + mensajes
    response = _openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=[{"type": "function", "function": _FUNCION_RESPONDER}],
        tool_choice={"type": "function", "function": {"name": "responder_cliente"}},
    )
    args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
    return (
        args["respuesta"],
        args.get("nombre_detectado"),
        args.get("destino_detectado"),
        args.get("viaje_interes"),
        args.get("tipo_viaje"),
    )


# ─── WhatsApp ─────────────────────────────────────────────────────────────────

def _post_whatsapp(payload: dict) -> None:
    url = f"{META_API_BASE}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    telefono = payload.get("to", "?")
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if not r.ok:
            logger.error("Error enviando a %s: %s — %s", telefono, r.status_code, r.text)
        r.raise_for_status()
    except Exception as exc:
        logger.error("Error enviando a %s: %s", telefono, exc)


def enviar_mensaje_texto(telefono: str, mensaje: str) -> None:
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "text",
        "text": {"body": mensaje},
    })


_ESTADOS_CLIENTE = {"chat_cliente", "chat_cliente_nacional", "chat_cliente_internacional"}


def enviar_botones_reserva(telefono: str, estado: str = "") -> None:
    if estado in _ESTADOS_CLIENTE:
        titulo_accion = "Hablar con asesor"
        cuerpo = "¿Necesitas hablar directamente con un asesor? 😊"
    else:
        titulo_accion = "Quiero reservar"
        cuerpo = "¿Te gustaría dar el siguiente paso? 😊"

    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar", "title": titulo_accion}},
                    {"type": "reply", "reply": {"id": "btn_terminar", "title": "Terminar chat"}},
                ]
            },
        },
    })


# ─── Follow-up automático ─────────────────────────────────────────────────────

def _enviar_seguimiento_8h(telefono: str) -> None:
    """Primer aviso a las 8h de inactividad — invita a reservar o a resolver dudas."""
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "¡Hola! 👋 Vimos que estuviste explorando nuestros destinos de viaje.\n\n"
                    "¿Pudiste encontrar lo que buscabas? Si tienes alguna duda o ya estás listo "
                    "para apartar tu lugar, aquí estamos. ✈️"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar",   "title": "Quiero reservar"}},
                    {"type": "reply", "reply": {"id": "btn_seguir",     "title": "Tengo una duda"}},
                    {"type": "reply", "reply": {"id": "btn_no_interes", "title": "No me interesa"}},
                ]
            },
        },
    })


def _enviar_recordatorio_cierre(telefono: str, estado: str) -> None:
    """Segundo aviso a las 24h — advierte que el chat se cerrará en 2h."""
    nombre = "Puebla Travel Trips" if estado == "chat_nacional" else "LibertYa"
    _post_whatsapp({
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    f"¡Hola de nuevo! 😊 Desde *{nombre}* queremos recordarte "
                    "que seguimos aquí para ayudarte a planear tu próximo viaje. ✈️\n\n"
                    "Nuestros asesores están listos para reservar tu lugar y acompañarte "
                    "en cada paso, ya sea un destino nacional o internacional. 🌎\n\n"
                    "⚠️ Si no recibimos respuesta en las próximas *2 horas*, "
                    "cerraremos esta conversación automáticamente.\n"
                    "¡Pero siempre estaremos aquí para cuando quieras viajar!"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "btn_reservar",   "title": "Quiero reservar"}},
                    {"type": "reply", "reply": {"id": "btn_no_interes", "title": "No me interesa"}},
                ]
            },
        },
    })


def manejar_boton(db: Session, telefono: str, boton_id: str) -> None:
    sesion = obtener_o_crear_sesion(db, telefono)
    estado = get_estado(list(sesion.historial or []))

    if boton_id in ("btn_no_interes", "btn_terminar"):
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        guardar_o_actualizar_lead(db, telefono, estatus="no_interesado")
        enviar_mensaje_texto(telefono, _MENSAJE_DESPEDIDA)

    elif boton_id in ("btn_asesor", "btn_reservar"):
        historial = list(sesion.historial or [])
        viajes_interes = get_viajes_interes(historial)
        estatus_derivado = "derivado_nacional" if estado in _ESTADOS_NACIONALES else "derivado_internacional"
        guardar_o_actualizar_lead(db, telefono, estatus=estatus_derivado)
        duda = get_ultima_duda(historial) if estado in ("chat_cliente_nacional", "chat_cliente_internacional") else None
        enviar_mensaje_texto(telefono, _mensaje_derivar(estado, viajes_interes, duda))

    elif boton_id == "btn_seguir":
        guardar_historial(db, sesion, set_estado(mensajes_openai(list(sesion.historial or [])), "menu"))
        enviar_mensaje_texto(telefono, _MENU_OPCIONES)


def procesar_seguimientos(db: Session) -> None:
    ahora = datetime.now(tz=_TZ_MX)

    # ── 1. Seguimiento 8h: primer aviso tras 8h de inactividad ───────────────
    sesiones_8h = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(hours=8),
            or_(
                SesionIA.seguimiento_1h.is_(None),
                SesionIA.seguimiento_1h < SesionIA.ultimo_mensaje,
            ),
        )
        .all()
    )
    for sesion in sesiones_8h:
        try:
            _enviar_seguimiento_8h(sesion.telefono_cliente)
            sesion.seguimiento_1h = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error seguimiento 8h a %s: %s", sesion.telefono_cliente, exc)

    # ── 2. Recordatorio 24h: segundo aviso con advertencia de cierre en 2h ───
    sesiones_24h = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.ultimo_mensaje < ahora - timedelta(hours=24),
            SesionIA.seguimiento_1h.isnot(None),
            SesionIA.seguimiento_3d.is_(None),
        )
        .all()
    )
    for sesion in sesiones_24h:
        try:
            estado_sesion = get_estado(list(sesion.historial or []))
            _enviar_recordatorio_cierre(sesion.telefono_cliente, estado_sesion)
            sesion.seguimiento_3d = ahora
            db.commit()
        except Exception as exc:
            logger.error("Error recordatorio 24h a %s: %s", sesion.telefono_cliente, exc)

    # ── 3. Auto-cierre: 2h después del recordatorio sin respuesta ────────────
    sesiones_cierre = (
        db.query(SesionIA)
        .filter(
            SesionIA.sesion_cerrada == False,  # noqa: E712
            SesionIA.seguimiento_3d.isnot(None),
            SesionIA.seguimiento_3d < ahora - timedelta(hours=2),
            SesionIA.ultimo_mensaje < SesionIA.seguimiento_3d,
        )
        .all()
    )
    for sesion in sesiones_cierre:
        try:
            sesion.sesion_cerrada = True
            db.commit()
            logger.info("Chat cerrado automáticamente: %s", sesion.telefono_cliente)
        except Exception as exc:
            logger.error("Error auto-cierre a %s: %s", sesion.telefono_cliente, exc)


# ─── Estadísticas para el dashboard ──────────────────────────────────────────

def get_estadisticas(db: Session) -> dict:
    por_estatus_raw = (
        db.query(Lead.estatus, func.count(Lead.id))
        .group_by(Lead.estatus)
        .all()
    )
    por_estatus = {e: c for e, c in por_estatus_raw}
    total = sum(por_estatus.values())
    derivados = (
        por_estatus.get("derivado_nacional", 0)
        + por_estatus.get("derivado_internacional", 0)
    )
    tasa = round(derivados / total * 100, 1) if total else 0.0

    top_destinos = (
        db.query(Lead.destino_interes, func.count(Lead.id).label("total"))
        .filter(Lead.destino_interes.isnot(None))
        .group_by(Lead.destino_interes)
        .order_by(func.count(Lead.id).desc())
        .limit(10)
        .all()
    )

    sesiones_activas = (
        db.query(func.count(SesionIA.id))
        .filter(SesionIA.sesion_cerrada == False)  # noqa: E712
        .scalar()
    ) or 0

    fecha = datetime.now(tz=_TZ_MX).strftime("%d/%m/%Y %H:%M")

    return {
        "total": total,
        "por_estatus": por_estatus,
        "derivados": derivados,
        "tasa_conversion": tasa,
        "top_destinos": list(top_destinos),
        "sesiones_activas": sesiones_activas,
        "fecha_actualizacion": fecha,
    }
