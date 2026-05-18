"""
Punto de entrada — FastAPI.

Endpoints:
  GET  /webhook  — verificación del webhook por Meta
  POST /webhook  — recibe mensajes y ejecuta el flujo conversacional
  GET  /health   — health check
"""
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .schemas import WhatsAppWebhookPayload
from .services import (
    _MENSAJE_BIENVENIDA,
    _MENSAJE_DESPEDIDA,
    _MENU_OPCIONES,
    _ESTADO_POR_INTENCION,
    _ESTADOS_CON_IA,
    _OPCION_POR_INTENCION,
    clasificar_intencion,
    enviar_botones_reserva,
    enviar_mensaje_texto,
    generar_respuesta_ia,
    get_estado,
    get_respuesta_opcion,
    guardar_historial,
    guardar_o_actualizar_lead,
    manejar_boton,
    mensajes_openai,
    obtener_o_crear_sesion,
    preparar_historial,
    procesar_seguimientos,
    set_estado,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler(timezone="America/Mexico_City")


def _job_seguimientos() -> None:
    db = SessionLocal()
    try:
        procesar_seguimientos(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _scheduler.add_job(_job_seguimientos, "interval", minutes=5, id="seguimientos")
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="LibertYa WhatsApp Bot", version="1.0.0", lifespan=lifespan)


@app.get("/webhook", response_class=PlainTextResponse)
def verificar_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if hub_mode != "subscribe" or hub_verify_token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Verify token inválido")
    return hub_challenge or ""


@app.post("/webhook", status_code=200)
async def recibir_mensaje(request: Request, db: Session = Depends(get_db)):
    try:
        raw = await request.json()
        payload = WhatsAppWebhookPayload.model_validate(raw)
        _procesar_payload(payload, db)
    except Exception as exc:
        logger.exception("Error procesando webhook: %s", exc)
    return {"status": "ok"}


def _procesar_payload(payload: WhatsAppWebhookPayload, db: Session) -> None:
    if not payload.entry:
        return
    for entrada in payload.entry:
        for cambio in (entrada.changes or []):
            valor = cambio.value
            if not valor or not valor.messages:
                continue
            for mensaje in valor.messages:
                if mensaje.type == "text" and mensaje.text:
                    _procesar_mensaje(db, telefono=mensaje.from_, texto=mensaje.text.body)
                elif mensaje.type == "interactive" and mensaje.interactive:
                    btn = mensaje.interactive.button_reply
                    if btn and btn.id and mensaje.from_:
                        manejar_boton(db, telefono=mensaje.from_, boton_id=btn.id)


def _procesar_mensaje(db: Session, telefono: str, texto: str) -> None:
    logger.info("Mensaje de %s: %s", telefono, texto[:80])

    if telefono in settings.blocked_phones_set:
        return

    guardar_o_actualizar_lead(db, telefono)

    sesion = obtener_o_crear_sesion(db, telefono)
    if sesion.sesion_cerrada:
        sesion.sesion_cerrada = False
        sesion.seguimiento_1h = None
        sesion.seguimiento_3d = None
        db.commit()
    historial = list(sesion.historial or [])
    estado = get_estado(historial)

    intencion = clasificar_intencion(texto, estado)
    logger.info("Intención de %s (estado=%s): %s", telefono, estado, intencion)

    if intencion == "no_interesado":
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        guardar_o_actualizar_lead(db, telefono, estatus="no_interesado")
        enviar_mensaje_texto(telefono, _MENSAJE_DESPEDIDA)
        return

    if intencion == "despedida":
        guardar_historial(db, sesion, set_estado([], "menu"))
        enviar_mensaje_texto(telefono, _MENSAJE_DESPEDIDA)
        return

    if intencion == "saludo":
        guardar_historial(db, sesion, set_estado([], "menu"))
        enviar_mensaje_texto(telefono, _MENSAJE_BIENVENIDA)
        return

    if intencion == "continuar" and estado in _ESTADOS_CON_IA:
        msgs = preparar_historial(mensajes_openai(historial), texto)
        respuesta, nombre, destino = generar_respuesta_ia(msgs, estado)
        msgs.append({"role": "assistant", "content": respuesta})
        guardar_historial(db, sesion, set_estado(msgs, estado))
        guardar_o_actualizar_lead(db, telefono, nombre=nombre, destino=destino)
        enviar_mensaje_texto(telefono, respuesta)
        enviar_botones_reserva(telefono)
        return

    if intencion in _ESTADO_POR_INTENCION:
        opcion = _OPCION_POR_INTENCION[intencion]
        respuesta = get_respuesta_opcion(opcion)
        nuevo_estado = _ESTADO_POR_INTENCION[intencion]
        msgs = mensajes_openai(historial) + [{"role": "assistant", "content": respuesta}]
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado))
        guardar_o_actualizar_lead(db, telefono, estatus="informado")
        enviar_mensaje_texto(telefono, respuesta)
        return

    enviar_mensaje_texto(
        telefono,
        "¡Hmm, no entendí bien! 😊 ¿Sobre qué te puedo ayudar?\n\n" + _MENU_OPCIONES,
    )


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "PueblTrips Bot"}


@app.post("/cron/recordatorio")
def cron_recordatorio(
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    procesar_seguimientos(db)
    return {"status": "ok"}
