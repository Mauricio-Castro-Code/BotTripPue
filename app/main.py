"""
Punto de entrada — FastAPI.

Endpoints:
  GET  /webhook  — verificación del webhook por Meta
  POST /webhook  — recibe mensajes y ejecuta el flujo conversacional
  GET  /health   — health check
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from .config import settings
from .crm_service import aplicar_score, guardar_mensaje_entrante, guardar_mensaje_saliente, sincronizar_estado_comercial
from .database import SessionLocal, get_db
from . import crm as _crm_module
from .schemas import BroadcastRequest, WhatsAppWebhookPayload
from .services import (
    broadcast_mensaje,
    _MENSAJE_BIENVENIDA,
    _MENSAJE_DESPEDIDA,
    _MENU_OPCIONES,
    _ESTADO_POR_INTENCION,
    _ESTADOS_CON_IA,
    _ESTADOS_AMBIGUOS_ASESOR,
    _OPCION_POR_INTENCION,
    _PREGUNTA_TIPO_ASESOR,
    clasificar_intencion,
    derivar_a_asesor,
    enviar_botones,
    enviar_texto,
    generar_respuesta_ia,
    get_estadisticas,
    get_estado,
    get_viajes_interes,
    get_respuesta_opcion,
    guardar_historial,
    guardar_o_actualizar_lead,
    manejar_boton,
    mensajes_openai,
    notificar_cotizacion,
    obtener_o_crear_sesion,
    preparar_historial,
    procesar_seguimientos,
    registrar_echo_asesor,
    _reactivar_bot,
    set_estado,
    get_ultima_duda,
)
from .models import SesionIA as _SesionIA

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
app.include_router(_crm_module.router)


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
        objeto = raw.get("object", "")
        if objeto == "page":
            _procesar_payload_messenger(raw, db)
        else:
            payload = WhatsAppWebhookPayload.model_validate(raw)
            _procesar_payload_whatsapp(payload, db)
    except Exception as exc:
        logger.exception("Error procesando webhook: %s", exc)
    return {"status": "ok"}


def _procesar_payload_whatsapp(payload: WhatsAppWebhookPayload, db: Session) -> None:
    if not payload.entry:
        return
    for entrada in payload.entry:
        for cambio in (entrada.changes or []):
            valor = cambio.value
            if not valor or not valor.messages:
                continue
            for mensaje in valor.messages:
                if mensaje.type == "text" and mensaje.text:
                    _procesar_mensaje(db, telefono=mensaje.from_, texto=mensaje.text.body, canal="whatsapp")
                elif mensaje.type == "interactive" and mensaje.interactive:
                    btn = mensaje.interactive.button_reply
                    if btn and btn.id and mensaje.from_:
                        manejar_boton(db, telefono=mensaje.from_, boton_id=btn.id, canal="whatsapp")


def _procesar_payload_messenger(raw: dict, db: Session) -> None:
    for entrada in raw.get("entry", []):
        for evento in entrada.get("messaging", []):
            psid = evento.get("sender", {}).get("id")
            if not psid:
                continue
            msg = evento.get("message", {})

            # Echo: mensaje enviado desde la página
            if msg.get("is_echo"):
                # Solo procesar el comando /bot para reactivar el bot manualmente
                recipient_psid = evento.get("recipient", {}).get("id")
                if recipient_psid and msg.get("text", "").strip().lower() == "/bot":
                    sesion = db.query(_SesionIA).filter(
                        _SesionIA.telefono_cliente == recipient_psid
                    ).first()
                    if sesion:
                        _reactivar_bot(db, sesion, "messenger")
                continue

            # Quick reply (clic en botón de Messenger)
            quick_reply = msg.get("quick_reply")
            if quick_reply:
                manejar_boton(db, telefono=psid, boton_id=quick_reply.get("payload", ""), canal="messenger")
                continue

            # Mensaje de texto normal
            texto = msg.get("text")
            if texto:
                _procesar_mensaje(db, telefono=psid, texto=texto, canal="messenger")


def _enviar_y_guardar(db: Session, sesion: _SesionIA, canal: str, telefono: str, texto: str) -> None:
    enviar_texto(canal, telefono, texto)
    guardar_mensaje_saliente(db, sesion, texto, sender_type="bot")


def _procesar_mensaje(db: Session, telefono: str, texto: str, canal: str = "whatsapp") -> None:
    logger.info("[%s] Mensaje de %s: %s", canal, telefono, texto[:80])

    if telefono in settings.blocked_phones_set:
        return

    guardar_o_actualizar_lead(db, telefono)

    sesion = obtener_o_crear_sesion(db, telefono, canal)

    # Guardar mensaje entrante, actualizar score y estado comercial
    guardar_mensaje_entrante(db, sesion, texto)
    aplicar_score(db, sesion, texto)
    sincronizar_estado_comercial(db, sesion)

    if sesion.sesion_cerrada:
        sesion.sesion_cerrada = False
        sesion.seguimiento_1h = None
        sesion.seguimiento_3d = None
        sesion.historial = []
        sesion.estado_comercial = "nuevo"
        sesion.asesor_activo = False
        sesion.asesor_desde = None
        sesion.asesor_nombre = None
        db.commit()

    # ── Human handoff ─────────────────────────────────────────────────────────
    if sesion.asesor_activo:
        if canal == "messenger" and sesion.asesor_desde:
            from .services import _TZ_MX
            ahora = datetime.now(tz=_TZ_MX)
            asesor_desde = sesion.asesor_desde
            if asesor_desde.tzinfo is None:
                asesor_desde = asesor_desde.replace(tzinfo=_TZ_MX)
            if (ahora - asesor_desde) < timedelta(hours=24):
                logger.info("[%s] Bot silenciado — asesor activo en sesión %s", canal, telefono)
                return
            else:
                sesion.asesor_activo = False
                sesion.asesor_desde = None
                db.commit()
                logger.info("[%s] Bot reactivado (24h sin asesor) para %s", canal, telefono)
        else:
            # WhatsApp CRM handoff — silenciado hasta reactivar manualmente desde el panel
            logger.info("[%s] Bot silenciado (CRM) para %s", canal, telefono)
            return

    historial = list(sesion.historial or [])
    estado = get_estado(historial)

    if estado == "esperando_tipo_asesor":
        texto_lower = texto.lower().strip()
        if texto_lower == "2" or "internacional" in texto_lower:
            nuevo_estado = "chat_internacional"
        elif texto_lower == "1" or "nacional" in texto_lower:
            nuevo_estado = "chat_nacional"
        else:
            _enviar_y_guardar(db, sesion, canal, telefono, _PREGUNTA_TIPO_ASESOR)
            return
        guardar_historial(db, sesion, set_estado(mensajes_openai(historial), nuevo_estado))
        derivar_a_asesor(db, sesion, canal, telefono, nuevo_estado)
        return

    intencion = clasificar_intencion(texto, estado)
    logger.info("[%s] Intención de %s (estado=%s): %s", canal, telefono, estado, intencion)

    if intencion == "quiere_humano":
        if estado in _ESTADOS_AMBIGUOS_ASESOR:
            guardar_historial(db, sesion, set_estado(mensajes_openai(historial), "esperando_tipo_asesor"))
            _enviar_y_guardar(db, sesion, canal, telefono, _PREGUNTA_TIPO_ASESOR)
        else:
            derivar_a_asesor(db, sesion, canal, telefono, estado)
        return

    if intencion == "no_interesado":
        guardar_historial(db, sesion, set_estado([], "cerrada"))
        sesion.sesion_cerrada = True
        db.commit()
        guardar_o_actualizar_lead(db, telefono, estatus="no_interesado")
        _enviar_y_guardar(db, sesion, canal, telefono, _MENSAJE_DESPEDIDA)
        return

    if intencion == "despedida":
        guardar_historial(db, sesion, set_estado([], "menu"))
        _enviar_y_guardar(db, sesion, canal, telefono, _MENSAJE_DESPEDIDA)
        return

    if intencion == "saludo":
        guardar_historial(db, sesion, set_estado([], "menu"))
        _enviar_y_guardar(db, sesion, canal, telefono, _MENSAJE_BIENVENIDA)
        return

    if intencion == "continuar" and estado in _ESTADOS_CON_IA:
        viajes_actuales = get_viajes_interes(historial)
        msgs = preparar_historial(mensajes_openai(historial), texto)
        respuesta, nombre, destino, viaje_nuevo, tipo_viaje, requiere_asesor, resumen_cotizacion = generar_respuesta_ia(msgs, estado)
        msgs.append({"role": "assistant", "content": respuesta})
        if viaje_nuevo and viaje_nuevo not in viajes_actuales:
            viajes_actuales = (viajes_actuales + [viaje_nuevo])[:3]
        if estado == "chat_cliente" and tipo_viaje in ("nacional", "internacional"):
            nuevo_estado = f"chat_cliente_{tipo_viaje}"
        else:
            nuevo_estado = estado
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado, viajes_actuales))
        guardar_o_actualizar_lead(db, telefono, nombre=nombre, destino=destino)
        _enviar_y_guardar(db, sesion, canal, telefono, respuesta)
        if resumen_cotizacion:
            notificar_cotizacion(resumen_cotizacion, telefono)
        if requiere_asesor:
            derivar_a_asesor(db, sesion, canal, telefono, nuevo_estado)
        else:
            enviar_botones(canal, telefono, nuevo_estado)
        return

    if intencion in _ESTADO_POR_INTENCION:
        opcion = _OPCION_POR_INTENCION[intencion]
        respuesta = get_respuesta_opcion(opcion)
        nuevo_estado = _ESTADO_POR_INTENCION[intencion]
        msgs = mensajes_openai(historial) + [{"role": "assistant", "content": respuesta}]
        viajes_actuales = get_viajes_interes(historial) if intencion == "menu_grupo" else None
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado, viajes_actuales))
        guardar_o_actualizar_lead(db, telefono, estatus="informado")
        _enviar_y_guardar(db, sesion, canal, telefono, respuesta)
        return

    if intencion == "continuar" and estado == "menu":
        msgs = preparar_historial([], texto)
        respuesta, nombre, destino, viaje_nuevo, tipo_viaje, requiere_asesor, _resumen = generar_respuesta_ia(msgs, "menu")
        nuevo_estado = (
            "chat_nacional" if tipo_viaje == "nacional"
            else "chat_internacional" if tipo_viaje == "internacional"
            else "menu"
        )
        msgs.append({"role": "assistant", "content": respuesta})
        viajes_actuales = [viaje_nuevo] if viaje_nuevo else []
        guardar_historial(db, sesion, set_estado(msgs, nuevo_estado, viajes_actuales or None))
        guardar_o_actualizar_lead(db, telefono, nombre=nombre, destino=destino, estatus="informado")
        _enviar_y_guardar(db, sesion, canal, telefono, respuesta)
        if requiere_asesor and nuevo_estado != "menu":
            derivar_a_asesor(db, sesion, canal, telefono, nuevo_estado)
        elif requiere_asesor:
            guardar_historial(db, sesion, set_estado(mensajes_openai(list(sesion.historial or [])), "esperando_tipo_asesor"))
            _enviar_y_guardar(db, sesion, canal, telefono, _PREGUNTA_TIPO_ASESOR)
        elif nuevo_estado in _ESTADOS_CON_IA:
            enviar_botones(canal, telefono, nuevo_estado)
        return

    _enviar_y_guardar(
        db, sesion, canal, telefono,
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
    resultado = broadcast_mensaje(db, body.mensaje, body.boton_id, body.boton_titulo)
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


_ESTATUS_LABEL = {
    "nuevo": ("Nuevo", "#1565c0", "#e3f2fd"),
    "informado": ("Informado", "#f57f17", "#fff8e1"),
    "derivado_nacional": ("Derivado ✓", "#2e7d32", "#e8f5e9"),
    "derivado_internacional": ("Derivado ✓", "#1b5e20", "#c8e6c9"),
    "no_interesado": ("No interesó", "#b71c1c", "#fce4ec"),
}

_ESTADO_BOT_LABEL = {
    "menu": "Menú",
    "chat_nacional": "Viajes nacionales",
    "chat_internacional": "Viajes internacionales",
    "chat_cliente": "Cliente existente",
    "chat_cliente_nacional": "Cliente nacional",
    "chat_cliente_internacional": "Cliente internacional",
    "chat_grupo": "Grupo",
    "esperando_tipo_asesor": "Esperando tipo (asesor)",
    "cerrada": "Cerrada",
}


def _badge(estatus: str) -> str:
    label, color, bg = _ESTATUS_LABEL.get(estatus, (estatus, "#888", "#f0f2f5"))
    return f'<span style="background:{bg};color:{color};padding:2px 8px;border-radius:20px;font-size:.72rem;font-weight:600">{label}</span>'


def _render_dashboard(stats: dict) -> str:
    por_estatus = stats["por_estatus"]
    total = stats["total"]

    def pct(n: int) -> float:
        return round(n / total * 100, 1) if total else 0.0

    # ── Barras de estatus ─────────────────────────────────────────────────────
    estatuses = [
        ("nuevo",                  "Nuevos",                  "#1565c0"),
        ("informado",              "Informados",               "#f57f17"),
        ("derivado_nacional",      "Derivados Nacional",       "#2e7d32"),
        ("derivado_internacional", "Derivados Internacional",  "#1b5e20"),
        ("no_interesado",          "No interesados",           "#b71c1c"),
    ]
    status_rows = ""
    for key, label, color in estatuses:
        count = por_estatus.get(key, 0)
        p = pct(count)
        status_rows += (
            f'<div class="status-row">'
            f'<span class="label-est">{label}</span>'
            f'<div class="bar-wrap"><div class="bar" style="width:{p}%;background:{color}"></div></div>'
            f'<span class="count-est" style="color:{color}">{count} <small>({p}%)</small></span>'
            f"</div>"
        )

    # ── Top destinos ──────────────────────────────────────────────────────────
    destino_rows = ""
    for i, (destino, count) in enumerate(stats["top_destinos"], 1):
        destino_rows += f"<tr><td style='color:#888;font-weight:600'>{i}</td><td>{destino or '—'}</td><td>{count}</td></tr>"
    if not destino_rows:
        destino_rows = '<tr><td colspan="3" style="color:#aaa;text-align:center;padding:16px">Sin datos aún</td></tr>'

    # ── Actividad 7 días ──────────────────────────────────────────────────────
    max_dia = max((d["total"] for d in stats["leads_por_dia"]), default=1) or 1
    dia_bars = ""
    for d in stats["leads_por_dia"]:
        h = max(4, int(d["total"] / max_dia * 60))
        dia_bars += (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">'
            f'<span style="font-size:.7rem;color:#888">{d["total"]}</span>'
            f'<div style="width:100%;height:{h}px;background:#25D366;border-radius:4px 4px 0 0;min-height:4px"></div>'
            f'<span style="font-size:.68rem;color:#aaa">{d["dia"]}</span>'
            f'</div>'
        )
    dia_table = ""
    for d in reversed(stats["leads_por_dia"]):
        if d["total"] > 0:
            dia_table += f"<tr><td>{d['dia']}</td><td>{d['total']}</td><td style='color:#25D366'>{d['derivados']}</td></tr>"
    if not dia_table:
        dia_table = '<tr><td colspan="3" style="color:#aaa;text-align:center;padding:12px">Sin actividad esta semana</td></tr>'

    # ── Chats activos ─────────────────────────────────────────────────────────
    activas_rows = ""
    for s in stats["detalle_activas"]:
        estado_label = _ESTADO_BOT_LABEL.get(s["estado_bot"], s["estado_bot"])
        canal_icon = "💬" if s.get("canal") == "messenger" else "📱"
        activas_rows += (
            f"<tr>"
            f"<td style='color:#888'>{s['tel']}</td>"
            f"<td>{s['nombre']}</td>"
            f"<td style='max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{s['destino']}</td>"
            f"<td><span style='font-size:.72rem;color:#555'>{estado_label}</span></td>"
            f"<td>{_badge(s['estatus'])}</td>"
            f"<td style='text-align:center'>{canal_icon}</td>"
            f"<td style='color:#aaa;font-size:.78rem'>{s['ultima']}</td>"
            f"</tr>"
        )
    if not activas_rows:
        activas_rows = '<tr><td colspan="7" style="color:#aaa;text-align:center;padding:16px">Sin sesiones activas</td></tr>'

    # ── Leads recientes ───────────────────────────────────────────────────────
    recientes_rows = ""
    for l in stats["leads_recientes"]:
        recientes_rows += (
            f"<tr>"
            f"<td style='color:#888'>{l['tel']}</td>"
            f"<td>{l['nombre']}</td>"
            f"<td style='max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{l['destino']}</td>"
            f"<td>{_badge(l['estatus'])}</td>"
            f"<td style='color:#aaa;font-size:.78rem'>{l['fecha']}</td>"
            f"</tr>"
        )
    if not recientes_rows:
        recientes_rows = '<tr><td colspan="5" style="color:#aaa;text-align:center;padding:16px">Sin datos aún</td></tr>'

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
.header{{background:linear-gradient(135deg,#25D366,#128C7E);color:white;padding:24px 32px;display:flex;justify-content:space-between;align-items:center}}
.header h1{{font-size:1.4rem;font-weight:700}}
.header p{{font-size:0.82rem;opacity:0.85;margin-top:4px}}
.reload{{background:rgba(255,255,255,.2);color:white;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:.82rem}}
.container{{max-width:920px;margin:28px auto;padding:0 16px}}
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
td{{padding:9px 0;border-bottom:1px solid #f8f8f8;font-size:.85rem;vertical-align:middle}}
td:last-child,th:last-child{{text-align:right}}
.two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
.footer{{text-align:center;font-size:.72rem;color:#bbb;padding:16px 0 32px}}
@media(max-width:600px){{.label-est{{width:120px}}.count-est{{width:80px}}.two-col{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>📊 Panel de Seguimiento</h1>
    <p>LibertYa &amp; Puebla Travel Trips &nbsp;·&nbsp; {stats['fecha_actualizacion']}</p>
  </div>
  <button class="reload" onclick="location.reload()">↻ Actualizar</button>
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
      <div class="clabel">Chats activos</div>
      <div class="cvalue" style="color:#1565c0">{stats['sesiones_activas']}</div>
      <div class="csub" style="color:#888">en este momento</div>
    </div>
    <div class="card">
      <div class="clabel">No interesados</div>
      <div class="cvalue" style="color:#b71c1c">{no_interesado}</div>
      <div class="csub" style="color:#888">{pct(no_interesado)}% del total</div>
    </div>
    <div class="card">
      <div class="clabel">Canales</div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
        <span style="font-size:1.3rem">📱</span>
        <div>
          <div style="font-size:1.2rem;font-weight:700;color:#25D366">{stats['por_canal'].get('whatsapp', 0)}</div>
          <div style="font-size:.7rem;color:#888">WhatsApp</div>
        </div>
        <span style="font-size:1.3rem;margin-left:8px">💬</span>
        <div>
          <div style="font-size:1.2rem;font-weight:700;color:#0084ff">{stats['por_canal'].get('messenger', 0)}</div>
          <div style="font-size:.7rem;color:#888">Messenger</div>
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <h2>📅 Actividad — Últimos 7 días</h2>
    <div style="display:flex;align-items:flex-end;gap:6px;height:80px;margin-bottom:16px">{dia_bars}</div>
    <table>
      <thead><tr><th>Fecha</th><th>Nuevos chats</th><th>Derivados</th></tr></thead>
      <tbody>{dia_table}</tbody>
    </table>
  </div>

  <div class="two-col">
    <div class="section">
      <h2>Distribución por estatus</h2>
      {status_rows}
    </div>
    <div class="section">
      <h2>🏆 Top destinos</h2>
      <table>
        <thead><tr><th>#</th><th>Destino</th><th>Total</th></tr></thead>
        <tbody>{destino_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <h2>🟢 Chats activos ahora</h2>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Teléfono</th><th>Nombre</th><th>Destino</th><th>Estado bot</th><th>Estatus</th><th style="text-align:center">Canal</th><th>Última act.</th></tr></thead>
      <tbody>{activas_rows}</tbody>
    </table>
    </div>
  </div>

  <div class="section">
    <h2>🕐 Leads recientes</h2>
    <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Teléfono</th><th>Nombre</th><th>Destino</th><th>Estatus</th><th>Fecha</th></tr></thead>
      <tbody>{recientes_rows}</tbody>
    </table>
    </div>
  </div>

</div>
<div class="footer">Datos en tiempo real · PueblTrips Bot</div>
</body>
</html>"""
