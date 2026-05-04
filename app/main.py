"""
Punto de entrada de la API — FastAPI.

Endpoints:
  GET  /webhook  — verificación del webhook por Meta (Verify Token)
  POST /webhook  — recibe mensajes de WhatsApp y ejecuta el flujo de cotización
  GET  /health   — health check
"""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Cotizacion, Negocio
from .schemas import DatosRenta, WhatsAppWebhookPayload
from .services import (
    _MENSAJE_BIENVENIDA,
    actualizar_contexto_sesion,
    analizar_equipo_y_clarificar,
    buscar_producto,
    cerrar_sesion,
    confirmar_pedido,
    detectar_consulta_inventario,
    detectar_consulta_precio,
    enviar_documento_pdf,
    enviar_imagen_whatsapp,
    enviar_mensaje_texto,
    es_autorizacion,
    es_saludo,
    extraer_datos_ia,
    generar_pdf_cotizacion,
    generar_pregunta_faltante,
    guardar_cotizacion,
    manteleria_declinada,
    marcar_sesion_esperando_autorizacion,
    obtener_o_crear_sesion,
    responder_consulta_inventario,
    responder_consulta_precio,
    sugerir_alternativas,
)

_PALETA_MANTELERIA = Path("assets/colores_mantel.png")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="BotCotizar — Alquiladora Crystal", version="1.0.0")


def _obtener_negocio(db: Session) -> Negocio:
    negocio = db.query(Negocio).filter(Negocio.id == settings.NEGOCIO_ID).first()
    if not negocio:
        raise RuntimeError(f"Negocio {settings.NEGOCIO_ID} no encontrado en BD")
    return negocio


# ─── Verificación del webhook (GET) ───────────────────────────────────────────

@app.get("/webhook", response_class=PlainTextResponse)
def verificar_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """
    Meta llama este endpoint al configurar el webhook.
    Valida el verify_token y devuelve el challenge para confirmar la suscripción.
    """
    if hub_mode != "subscribe" or hub_verify_token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Verify token inválido")
    return hub_challenge or ""


# ─── Recepción de mensajes (POST) ─────────────────────────────────────────────

@app.post("/webhook", status_code=200)
async def recibir_mensaje(request: Request, db: Session = Depends(get_db)):
    """
    Recibe el JSON de Meta, extrae el mensaje de texto y ejecuta el flujo:
      1. Extraer datos con IA
      2. Si faltan campos → preguntar al cliente
      3. Si está completo → guardar, generar PDF, enviar al cliente
    Siempre devuelve 200 para evitar reintentos de Meta.
    """
    try:
        raw = await request.json()
        payload = WhatsAppWebhookPayload.model_validate(raw)
        _procesar_payload(payload, db)
    except Exception as exc:
        # Log sin relanzar: Meta espera 200 siempre
        logger.exception("Error procesando webhook: %s", exc)
    return {"status": "ok"}


def _procesar_payload(payload: WhatsAppWebhookPayload, db: Session) -> None:
    if not payload.entry:
        return

    negocio = _obtener_negocio(db)

    for entrada in payload.entry:
        for cambio in (entrada.changes or []):
            valor = cambio.value
            if not valor or not valor.messages:
                continue
            for mensaje in valor.messages:
                if mensaje.type != "text" or not mensaje.text:
                    continue  # Solo procesamos mensajes de texto
                _procesar_mensaje_texto(
                    db=db,
                    negocio=negocio,
                    telefono=mensaje.from_,
                    texto=mensaje.text.body,
                )


def _procesar_mensaje_texto(
    db: Session,
    negocio: Negocio,
    telefono: str,
    texto: str,
) -> None:
    logger.info("Mensaje de %s: %s", telefono, texto[:80])

    # Telefonos bloqueados (familiares, internos, etc.) → el bot no responde
    if telefono in settings.blocked_phones_set:
        logger.info("Telefono bloqueado, omitiendo respuesta del bot: %s", telefono)
        return

    sesion = obtener_o_crear_sesion(db, telefono, str(negocio.id))

    # Esperando autorización: ya enviamos PDF y aguardamos "Sí autorizo"
    if sesion.cotizacion_id:
        cotizacion = db.query(Cotizacion).filter(Cotizacion.id == sesion.cotizacion_id).first()
        if cotizacion and not cotizacion.confirmada:
            _manejar_autorizacion(db, sesion, cotizacion, telefono, texto)
            return

    # Saludo puro ("hola", "buenas tardes", etc.) → siempre responder con saludo + invitación
    if es_saludo(texto):
        contexto_actual = DatosRenta.model_validate(sesion.contexto_actual)
        faltantes = contexto_actual.campos_faltantes
        if sesion.contexto_actual and faltantes:
            # Ya habia conversacion — saludamos y retomamos donde quedo
            pregunta = generar_pregunta_faltante(faltantes, contexto_actual.tipo_entrega)
            mensaje = (
                "¡Hola! 👋 Soy el asistente de *Alquiladora Crystal*. "
                "Retomemos tu cotización:\n\n" + pregunta
            )
        else:
            mensaje = _MENSAJE_BIENVENIDA
        enviar_mensaje_texto(telefono, mensaje)
        return

    # Consulta de inventario ("¿qué sillas manejan?") → responder y no cambiar contexto
    categoria = detectar_consulta_inventario(texto)
    if categoria:
        respuesta = responder_consulta_inventario(db, str(negocio.id), categoria)
        enviar_mensaje_texto(telefono, respuesta)
        return

    # Consulta de precio ("¿cuanto cuesta la silla acojinada?") → responder precio + invitar
    # Solo si no hay conversacion en curso (sesion.contexto_actual vacio) para no derailar mid-flow
    if not sesion.contexto_actual:
        producto = detectar_consulta_precio(texto, db, str(negocio.id))
        if producto:
            enviar_mensaje_texto(telefono, responder_consulta_precio(producto))
            return

    # Campo que el bot acaba de preguntar (para desambiguar respuestas cortas)
    contexto_anterior = DatosRenta.model_validate(sesion.contexto_actual)
    faltantes_previos = contexto_anterior.campos_faltantes
    campo_esperado = faltantes_previos[0] if faltantes_previos else None

    # Extraer nuevos datos del mensaje
    nuevos_datos = extraer_datos_ia(
        texto, sesion.contexto_actual, negocio.nombre, campo_esperado=campo_esperado
    )

    # Validar equipo contra inventario: si hay items sin match → sugerir alternativas
    if nuevos_datos.equipo:
        sin_match: list = []
        con_match: list = []
        for item in nuevos_datos.equipo:
            if buscar_producto(db, str(negocio.id), item.descripcion):
                con_match.append(item)
            else:
                sin_match.append(item)

        if sin_match:
            _responder_productos_sin_match(db, str(negocio.id), telefono, sin_match)
            # Solo conservamos los items que sí existen para no romper el flujo
            nuevos_datos.equipo = con_match or None

    # Si el cliente declina manteleria explicitamente, marcamos el flag
    if manteleria_declinada(texto):
        nuevos_datos.manteleria_consultada = True

    # Detectar items genericos (ej. "sillas") y sugerir complementos (manteleria)
    if nuevos_datos.equipo:
        clarificacion = analizar_equipo_y_clarificar(
            nuevos_datos.equipo, db, str(negocio.id),
            mensaje_usuario=texto,
            manteleria_consultada=contexto_anterior.manteleria_consultada,
        )
        if clarificacion:
            # Marcamos el flag y guardamos contexto para no preguntar dos veces
            nuevos_datos.manteleria_consultada = True
            contexto_actualizado = contexto_anterior.fusionar(nuevos_datos)
            actualizar_contexto_sesion(db, sesion, contexto_actualizado.model_dump())
            enviar_mensaje_texto(telefono, clarificacion)
            return

    # Fusionar con el contexto acumulado
    contexto_actualizado = contexto_anterior.fusionar(nuevos_datos)

    # Persistir el contexto enriquecido
    actualizar_contexto_sesion(db, sesion, contexto_actualizado.model_dump())

    if contexto_actualizado.completo:
        _finalizar_cotizacion(db, sesion, negocio, telefono, contexto_actualizado)
    else:
        faltantes = contexto_actualizado.campos_faltantes
        pregunta = generar_pregunta_faltante(faltantes, contexto_actualizado.tipo_entrega)
        # Si toca preguntar por color de mantel y tenemos paleta, mandamos imagen
        if faltantes and faltantes[0] == "mantel_color" and _PALETA_MANTELERIA.exists():
            enviado = enviar_imagen_whatsapp(telefono, str(_PALETA_MANTELERIA), caption=pregunta)
            if not enviado:
                enviar_mensaje_texto(telefono, pregunta)
        else:
            enviar_mensaje_texto(telefono, pregunta)


def _responder_productos_sin_match(db, negocio_id: str, telefono: str, items: list) -> None:
    """Envía un mensaje con disculpa y alternativas para cada producto no encontrado."""
    bloques = []
    for item in items:
        alternativas = sugerir_alternativas(db, negocio_id, item.descripcion)
        if alternativas:
            bloques.append(
                f"• No contamos con *{item.descripcion}*. Te ofrezco:\n   - "
                + "\n   - ".join(alternativas)
            )
        else:
            bloques.append(
                f"• No contamos con *{item.descripcion}* en nuestro catálogo."
            )
    mensaje = (
        "Te ofrezco una disculpa 🙏\n\n"
        + "\n\n".join(bloques)
        + "\n\nDéjame checar si te lo puedo conseguir. "
        "Mientras tanto, ¿quieres alguna de las opciones que te mencioné?"
    )
    enviar_mensaje_texto(telefono, mensaje)


_MENSAJE_ENVIO_NOTA = (
    "Hola, buen día 😊\n"
    "Te pedimos por favor leer con atención la información de la nota, "
    "ya que ahí vienen todos los detalles del servicio.\n"
    "Para dejar apartado tu equipo, solo necesitamos que nos confirmes respondiendo:\n"
    "*“Sí autorizo”*."
)

_MENSAJE_HUMANO = (
    "Gracias por tu mensaje 🙌\n"
    "En breve un asesor humano retomará el chat para atenderte personalmente."
)


def _finalizar_cotizacion(
    db: Session,
    sesion,
    negocio: Negocio,
    telefono: str,
    datos: DatosRenta,
) -> None:
    """Guarda la cotización, genera el PDF y la deja esperando autorización del cliente."""
    cotizacion = guardar_cotizacion(db, telefono, str(negocio.id), datos)

    # Recargar cliente para tener el objeto completo
    db.refresh(cotizacion.cliente)

    ruta_pdf = generar_pdf_cotizacion(cotizacion, cotizacion.cliente, negocio)

    # Actualizar la URL del PDF en la cotización
    cotizacion.pdf_url = ruta_pdf
    db.commit()

    # No cerramos la sesión todavía — quedamos esperando "Sí autorizo"
    marcar_sesion_esperando_autorizacion(db, sesion, str(cotizacion.id))

    enviado = enviar_documento_pdf(telefono, ruta_pdf, caption=_MENSAJE_ENVIO_NOTA)
    if not enviado:
        # Si falla el PDF, al menos mandamos el texto
        enviar_mensaje_texto(telefono, _MENSAJE_ENVIO_NOTA)


def _manejar_autorizacion(
    db: Session,
    sesion,
    cotizacion: Cotizacion,
    telefono: str,
    texto: str,
) -> None:
    """Procesa la respuesta del cliente tras enviarle la nota PDF."""
    if es_autorizacion(texto):
        folio_pedido = confirmar_pedido(db, cotizacion)
        cerrar_sesion(db, sesion, str(cotizacion.id))
        nombre = cotizacion.cliente.nombre or ""
        respuesta = (
            f"¡Muchas gracias{(' ' + nombre) if nombre else ''}! Estamos para servirte. 🙌\n"
            f"Tu folio de nota es: *{folio_pedido}*\n\n"
            "Nuestro equipo de logística se comunicará contigo para coordinar la entrega."
        )
        enviar_mensaje_texto(telefono, respuesta)
    else:
        cerrar_sesion(db, sesion, str(cotizacion.id))
        enviar_mensaje_texto(telefono, _MENSAJE_HUMANO)


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "BotCotizar"}
