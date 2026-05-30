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
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .schemas import BroadcastRequest, WhatsAppWebhookPayload
from .services import (
    broadcast_mensaje,
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
    get_estadisticas,
    get_estado,
    get_viajes_interes,
    get_respuesta_opcion,
    guardar_historial,
    guardar_o_actualizar_lead,
    manejar_boton,
    mensajes_openai,
    obtener_o_crear_sesion,
    preparar_historial,
    procesar_seguimientos,
    set_estado,
    get_ultima_duda,
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
        sesion.historial = []
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
        viajes_actuales = get_viajes_interes(historial)
        msgs = preparar_historial(mensajes_openai(historial), texto)
        respuesta, nombre, destino, viaje_nuevo, tipo_viaje = generar_respuesta_ia(msgs, estado)
        msgs.append({"role": "assistant", "content": respuesta})
        if viaje_nuevo and viaje_nuevo not in viajes_actuales:
            viajes_actuales = (viajes_actuales + [viaje_nuevo])[:3]
        if estado == "chat_cliente" and tipo_viaje in ("nacional", "internacional"):
            nuevo_estado = f"chat_cliente_{tipo_viaje}"
        else:
            nuevo_estado = estado
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado, viajes_actuales))
        guardar_o_actualizar_lead(db, telefono, nombre=nombre, destino=destino)
        enviar_mensaje_texto(telefono, respuesta)
        enviar_botones_reserva(telefono, nuevo_estado)
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

    if intencion == "continuar" and estado == "menu":
        msgs = preparar_historial([], texto)
        respuesta, nombre, destino, viaje_nuevo, tipo_viaje = generar_respuesta_ia(msgs, "menu")
        nuevo_estado = (
            "chat_nacional" if tipo_viaje == "nacional"
            else "chat_internacional" if tipo_viaje == "internacional"
            else "menu"
        )
        msgs.append({"role": "assistant", "content": respuesta})
        viajes_actuales = [viaje_nuevo] if viaje_nuevo else []
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado, viajes_actuales or None))
        guardar_o_actualizar_lead(db, telefono, nombre=nombre, destino=destino, estatus="informado")
        enviar_mensaje_texto(telefono, respuesta)
        if nuevo_estado in _ESTADOS_CON_IA:
            enviar_botones_reserva(telefono, nuevo_estado)
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


@app.post("/admin/broadcast")
def enviar_broadcast(
    body: BroadcastRequest,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    resultado = broadcast_mensaje(db, body.mensaje)
    return resultado


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")
    stats = get_estadisticas(db)
    return HTMLResponse(content=_render_dashboard(stats))


def _render_dashboard(stats: dict) -> str:
    por_estatus = stats["por_estatus"]
    total = stats["total"]

    def pct(n: int) -> float:
        return round(n / total * 100, 1) if total else 0.0

    estatuses = [
        ("nuevo",                  "Nuevos",                  "#1565c0", "#e3f2fd"),
        ("informado",              "Informados",               "#f57f17", "#fff8e1"),
        ("derivado_nacional",      "Derivados Nacional",       "#2e7d32", "#e8f5e9"),
        ("derivado_internacional", "Derivados Internacional",  "#1b5e20", "#c8e6c9"),
        ("no_interesado",          "No interesados",           "#b71c1c", "#fce4ec"),
    ]

    status_rows = ""
    for key, label, color, _ in estatuses:
        count = por_estatus.get(key, 0)
        p = pct(count)
        status_rows += (
            f'<div class="status-row">'
            f'<span class="label-est">{label}</span>'
            f'<div class="bar-wrap"><div class="bar" style="width:{p}%;background:{color}"></div></div>'
            f'<span class="count-est" style="color:{color}">{count} <small>({p}%)</small></span>'
            f"</div>"
        )

    destino_rows = ""
    for i, (destino, count) in enumerate(stats["top_destinos"], 1):
        destino_rows += (
            f"<tr>"
            f'<td style="color:#888;font-weight:600">{i}</td>'
            f"<td>{destino or '—'}</td>"
            f"<td>{count}</td>"
            f"</tr>"
        )
    if not destino_rows:
        destino_rows = '<tr><td colspan="3" style="color:#aaa;text-align:center;padding:16px">Sin datos aún</td></tr>'

    no_interesado = por_estatus.get("no_interesado", 0)

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Panel LibertYa</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f0f2f5;color:#1a1a2e}}
.header{{background:linear-gradient(135deg,#25D366,#128C7E);color:white;padding:24px 32px}}
.header h1{{font-size:1.4rem;font-weight:700}}
.header p{{font-size:0.82rem;opacity:0.85;margin-top:4px}}
.container{{max-width:860px;margin:28px auto;padding:0 16px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:20px}}
.card{{background:white;border-radius:12px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.clabel{{font-size:.72rem;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}}
.cvalue{{font-size:2rem;font-weight:700}}
.csub{{font-size:.78rem;margin-top:3px;font-weight:600}}
.section{{background:white;border-radius:12px;padding:22px;box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:20px}}
.section h2{{font-size:.95rem;font-weight:700;color:#555;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid #f0f2f5}}
.status-row{{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #f8f8f8}}
.status-row:last-child{{border-bottom:none}}
.label-est{{font-size:.82rem;width:190px;flex-shrink:0;color:#444}}
.bar-wrap{{flex:1;background:#f0f2f5;border-radius:4px;height:8px;overflow:hidden}}
.bar{{height:100%;border-radius:4px}}
.count-est{{font-size:.82rem;font-weight:700;width:110px;text-align:right;flex-shrink:0}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:.72rem;color:#888;text-transform:uppercase;letter-spacing:.5px;padding:8px 0;border-bottom:2px solid #f0f2f5}}
td{{padding:10px 0;border-bottom:1px solid #f8f8f8;font-size:.88rem}}
td:last-child,th:last-child{{text-align:right}}
.footer{{text-align:center;font-size:.72rem;color:#bbb;padding:16px 0 32px}}
@media(max-width:500px){{.label-est{{width:120px}}.count-est{{width:80px}}}}
</style>
</head>
<body>
<div class="header">
  <h1>📊 Panel de Seguimiento</h1>
  <p>LibertYa &amp; Puebla Travel Trips &nbsp;·&nbsp; Actualizado el {stats['fecha_actualizacion']}</p>
</div>
<div class="container">
  <div class="cards">
    <div class="card">
      <div class="clabel">Total contactos</div>
      <div class="cvalue">{total}</div>
      <div class="csub" style="color:#888">desde el inicio</div>
    </div>
    <div class="card">
      <div class="clabel">Derivados al asesor</div>
      <div class="cvalue" style="color:#25D366">{stats['derivados']}</div>
      <div class="csub" style="color:#25D366">{stats['tasa_conversion']}% de conversión</div>
    </div>
    <div class="card">
      <div class="clabel">Sesiones activas</div>
      <div class="cvalue" style="color:#1565c0">{stats['sesiones_activas']}</div>
      <div class="csub" style="color:#888">en este momento</div>
    </div>
    <div class="card">
      <div class="clabel">No interesados</div>
      <div class="cvalue" style="color:#b71c1c">{no_interesado}</div>
      <div class="csub" style="color:#888">{pct(no_interesado)}% del total</div>
    </div>
  </div>

  <div class="section">
    <h2>Distribución por estatus</h2>
    {status_rows}
  </div>

  <div class="section">
    <h2>🏆 Top destinos de interés</h2>
    <table>
      <thead><tr><th>#</th><th>Destino</th><th>Interesados</th></tr></thead>
      <tbody>{destino_rows}</tbody>
    </table>
  </div>
</div>
<div class="footer">Datos en tiempo real · PueblTrips Bot</div>
</body>
</html>"""
