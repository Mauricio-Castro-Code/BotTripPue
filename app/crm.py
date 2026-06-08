"""
Panel CRM para asesores — gestión de conversaciones de WhatsApp y Messenger.

Rutas:
  GET  /crm                          — lista de conversaciones
  GET  /crm/chat/{id}               — detalle del chat
  GET  /crm/chat/{id}/mensajes      — fragmento htmx con mensajes (polling)
  POST /crm/chat/{id}/tomar         — tomar chat
  POST /crm/chat/{id}/liberar       — liberar chat
  POST /crm/chat/{id}/bot/pausar    — pausar bot
  POST /crm/chat/{id}/bot/activar   — reactivar bot
  POST /crm/chat/{id}/estado        — cambiar estado comercial
  POST /crm/chat/{id}/score         — actualizar score
  POST /crm/chat/{id}/nota          — agregar nota interna
  POST /crm/chat/{id}/mensaje       — enviar mensaje manual

Todas requieren ?token=<WHATSAPP_VERIFY_TOKEN>.
"""
from __future__ import annotations

import html
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import settings
from .crm_service import (
    ESTADOS_COMERCIALES,
    actualizar_estado_comercial,
    actualizar_score,
    agregar_nota,
    clasificacion_score,
    enviar_mensaje_asesor,
    guardar_mensaje_saliente,
    liberar_chat,
    pausar_bot,
    reactivar_bot,
    tomar_chat,
)
from .database import get_db
from .models import Lead, Message, SesionIA

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/crm", tags=["crm"])


# ─── Auth helper ─────────────────────────────────────────────────────────────

def _check_token(token: str | None) -> None:
    if token != settings.WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")


# ─── Helpers de HTML ─────────────────────────────────────────────────────────

_ESTADO_STYLE = {
    "nuevo":              ("color:#1565c0", "background:#e3f2fd"),
    "automatizado":       ("color:#0288d1", "background:#e1f5fe"),
    "pregunto_info":      ("color:#f57f17", "background:#fff8e1"),
    "interesado":         ("color:#2e7d32", "background:#e8f5e9"),
    "requiere_humano":    ("color:#e65100", "background:#fff3e0"),
    "en_atencion_humana": ("color:#6a1b9a", "background:#f3e5f5"),
    "reservando":         ("color:#00695c", "background:#e0f2f1"),
    "apartado":           ("color:#1b5e20", "background:#c8e6c9"),
    "pagado":             ("color:#1a237e", "background:#e8eaf6"),
    "perdido":            ("color:#b71c1c", "background:#fce4ec"),
    "finalizado":         ("color:#424242", "background:#f5f5f5"),
}


def _badge_estado(estado: str) -> str:
    color, bg = _ESTADO_STYLE.get(estado or "nuevo", ("color:#888", "background:#f0f2f5"))
    label = (estado or "nuevo").replace("_", " ").title()
    return (
        f'<span style="{bg};{color};padding:2px 10px;border-radius:20px;'
        f'font-size:.72rem;font-weight:700;white-space:nowrap">{label}</span>'
    )


def _badge_score(score: int) -> str:
    label, color, bg = clasificacion_score(score)
    return (
        f'<span style="background:{bg};color:{color};padding:2px 8px;border-radius:20px;'
        f'font-size:.72rem;font-weight:700;white-space:nowrap">'
        f'{label} <b>{score}</b></span>'
    )


def _badge_bot(asesor_activo: bool) -> str:
    if asesor_activo:
        return (
            '<span style="background:#fce4ec;color:#b71c1c;padding:2px 8px;'
            'border-radius:20px;font-size:.72rem;font-weight:600">⏸ Pausado</span>'
        )
    return (
        '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 8px;'
        'border-radius:20px;font-size:.72rem;font-weight:600">▶ Activo</span>'
    )


def _e(text: str | None) -> str:
    return html.escape(text or "")


def _fmt_mins(mins: int) -> str:
    if mins < 60:
        return f"hace {mins}m"
    if mins < 1440:
        return f"hace {mins // 60}h"
    return f"hace {mins // 1440}d"


# ─── CSS compartido ───────────────────────────────────────────────────────────

_BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f2f5;color:#1a1a2e;font-size:14px}
.header{background:linear-gradient(135deg,#25D366,#128C7E);color:white;
        padding:16px 24px;display:flex;align-items:center;gap:16px}
.header h1{font-size:1.1rem;font-weight:700}
.header a{color:rgba(255,255,255,.8);text-decoration:none;font-size:.82rem;
          padding:6px 12px;background:rgba(255,255,255,.15);border-radius:6px}
.header a:hover{background:rgba(255,255,255,.25)}
.container{max-width:1100px;margin:24px auto;padding:0 16px}
.btn{display:inline-block;padding:6px 14px;border-radius:6px;border:none;
     cursor:pointer;font-size:.82rem;font-weight:600;text-decoration:none;
     transition:opacity .15s}
.btn:hover{opacity:.85}
.btn-green{background:#25D366;color:white}
.btn-red{background:#b71c1c;color:white}
.btn-blue{background:#1565c0;color:white}
.btn-orange{background:#e65100;color:white}
.btn-gray{background:#e0e0e0;color:#333}
.btn-purple{background:#6a1b9a;color:white}
.card{background:white;border-radius:12px;padding:20px;
      box-shadow:0 1px 3px rgba(0,0,0,.08);margin-bottom:16px}
.card h2{font-size:.9rem;font-weight:700;color:#555;margin-bottom:14px;
         padding-bottom:10px;border-bottom:1px solid #f0f2f5}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:.72rem;color:#888;text-transform:uppercase;
   letter-spacing:.5px;padding:8px 6px;border-bottom:2px solid #f0f2f5}
td{padding:10px 6px;border-bottom:1px solid #f8f8f8;font-size:.85rem;vertical-align:middle}
tr:hover td{background:#fafafa}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.filter-chip{padding:6px 14px;border-radius:20px;border:1px solid #ddd;
             background:white;color:#555;font-size:.8rem;cursor:pointer;
             text-decoration:none;font-weight:500}
.filter-chip.active{background:#25D366;color:white;border-color:#25D366}
.alerta{color:#e65100;font-weight:700}
input,textarea,select{width:100%;padding:8px 10px;border:1px solid #ddd;
                      border-radius:6px;font-size:.85rem;font-family:inherit}
textarea{resize:vertical;min-height:60px}
label{display:block;font-size:.78rem;color:#666;margin-bottom:4px;font-weight:500}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# LISTA DE CONVERSACIONES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("", response_class=HTMLResponse)
def crm_lista(
    token: str | None = Query(default=None),
    filtro: str | None = Query(default=None),
    orden: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)

    q = db.query(SesionIA, Lead).outerjoin(Lead, Lead.telefono == SesionIA.telefono_cliente)

    if filtro == "requiere_humano":
        q = q.filter(SesionIA.requiere_humano == True)  # noqa: E712
    elif filtro == "en_atencion":
        q = q.filter(SesionIA.estado_comercial == "en_atencion_humana")
    elif filtro == "calientes":
        q = q.filter(SesionIA.score >= 61, SesionIA.sesion_cerrada == False)  # noqa: E712
    elif filtro == "cerrados":
        q = q.filter(SesionIA.sesion_cerrada == True)  # noqa: E712
    else:
        q = q.filter(SesionIA.sesion_cerrada == False)  # noqa: E712

    if orden == "score":
        q = q.order_by(SesionIA.score.desc(), SesionIA.ultimo_mensaje.desc())
    else:
        q = q.order_by(
            SesionIA.requiere_humano.desc(),
            SesionIA.score.desc(),
            SesionIA.ultimo_mensaje.desc(),
        )

    sesiones = q.limit(200).all()

    # Contadores para tabs
    total_activos  = db.query(SesionIA).filter(SesionIA.sesion_cerrada == False).count()  # noqa
    total_requiere = db.query(SesionIA).filter(SesionIA.requiere_humano == True).count()   # noqa
    total_atencion = db.query(SesionIA).filter(SesionIA.estado_comercial == "en_atencion_humana").count()
    total_calientes = db.query(SesionIA).filter(SesionIA.score >= 61, SesionIA.sesion_cerrada == False).count()  # noqa

    def _chip(label: str, f: str, count: int | None = None) -> str:
        active = "active" if filtro == f or (f == "" and not filtro) else ""
        cnt = f' <b>({count})</b>' if count is not None else ""
        return (
            f'<a href="/crm?token={token}&filtro={f}&orden={orden or ""}" '
            f'class="filter-chip {active}">{label}{cnt}</a>'
        )

    def _sort_link(label: str, o: str) -> str:
        active_style = "font-weight:700;color:#25D366" if orden == o else "color:#888"
        arrow = " ↓" if orden == o else ""
        return (
            f'<a href="/crm?token={token}&filtro={filtro or ""}&orden={o}" '
            f'style="text-decoration:none;font-size:.78rem;{active_style}">{label}{arrow}</a>'
        )

    chips = (
        _chip("Activos", "", total_activos)
        + _chip("🔴 Requieren humano", "requiere_humano", total_requiere)
        + _chip("🌶️ Calientes", "calientes", total_calientes)
        + _chip("En atención humana", "en_atencion", total_atencion)
        + _chip("Cerrados", "cerrados")
    )

    sort_links = (
        "Ordenar: "
        + _sort_link("Más recientes", "reciente")
        + " &nbsp;|&nbsp; "
        + _sort_link("Mayor score", "score")
    )

    rows = ""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    tz = ZoneInfo("America/Mexico_City")
    ahora = datetime.now(tz=tz)

    for s, lead in sesiones:
        um = s.ultimo_mensaje
        if um and um.tzinfo is None:
            from zoneinfo import ZoneInfo as _ZI
            um = um.replace(tzinfo=_ZI("America/Mexico_City"))
        mins = int((ahora - um).total_seconds() / 60) if um else 0

        alerta = ' <span class="alerta" title="Requiere atención humana">🔴</span>' if s.requiere_humano else ""
        canal_icon = "💬" if (s.canal or "") == "messenger" else "📱"

        ultimo_msg = (
            db.query(Message)
            .filter(Message.sesion_id == s.id, Message.direccion == "inbound")
            .order_by(Message.created_at.desc())
            .first()
        )
        ultimo_body = _e(ultimo_msg.body[:60]) if ultimo_msg else "—"
        score_val = s.score or 0

        rows += f"""<tr onclick="location.href='/crm/chat/{s.id}?token={token}'" style="cursor:pointer">
            <td>{canal_icon} <code style="font-size:.78rem;color:#555">{_e(s.telefono_cliente[-10:])}</code>{alerta}</td>
            <td>{_e(lead.nombre if lead else None) or '<span style="color:#bbb">—</span>'}</td>
            <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {_e(lead.destino_interes if lead else None) or '<span style="color:#bbb">—</span>'}
            </td>
            <td>{_badge_score(score_val)}</td>
            <td>{_badge_estado(s.estado_comercial or "nuevo")}</td>
            <td>{_e(s.asesor_nombre) or '<span style="color:#bbb">Sin asignar</span>'}</td>
            <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555">
                {ultimo_body}
            </td>
            <td style="color:#aaa;font-size:.78rem">{_fmt_mins(mins)}</td>
            <td>{_badge_bot(s.asesor_activo)}</td>
            <td>
                <a href="/crm/chat/{s.id}?token={token}" class="btn btn-blue" onclick="event.stopPropagation()">
                    Ver →
                </a>
            </td>
        </tr>"""

    if not rows:
        rows = '<tr><td colspan="10" style="text-align:center;color:#aaa;padding:32px">Sin conversaciones</td></tr>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>CRM — LibertYa</title>
<style>
{_BASE_CSS}
tr{{cursor:pointer}}
</style>
</head>
<body>
<div class="header">
    <div style="flex:1">
        <h1>🎯 Panel CRM</h1>
        <div style="font-size:.78rem;opacity:.8;margin-top:2px">LibertYa &amp; Puebla Travel Trips</div>
    </div>
    <a href="/dashboard?token={token}">📊 Dashboard</a>
</div>

<div class="container">
    <div class="filters">{chips}</div>
    <div style="margin-bottom:12px;color:#666">{sort_links}</div>

    <div class="card" style="padding:0;overflow:hidden">
        <div style="overflow-x:auto">
        <table>
            <thead>
                <tr>
                    <th>Teléfono</th>
                    <th>Nombre</th>
                    <th>Destino</th>
                    <th>
                        <a href="/crm?token={token}&filtro={filtro or ''}&orden=score"
                           style="text-decoration:none;color:inherit">
                            Score {'↓' if orden == 'score' else ''}
                        </a>
                    </th>
                    <th>Estado</th>
                    <th>Asesor</th>
                    <th>Último mensaje</th>
                    <th>Tiempo</th>
                    <th>Bot</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        </div>
    </div>
</div>
</body>
</html>""")


# ═══════════════════════════════════════════════════════════════════════════════
# DETALLE DEL CHAT
# ═══════════════════════════════════════════════════════════════════════════════

def _render_mensajes(mensajes: list[Message]) -> str:
    if not mensajes:
        return '<div style="text-align:center;color:#aaa;padding:40px">Sin mensajes registrados aún</div>'

    rows = ""
    prev_date = None
    for m in mensajes:
        ts = m.created_at
        if ts and ts.tzinfo is None:
            from zoneinfo import ZoneInfo
            ts = ts.replace(tzinfo=ZoneInfo("America/Mexico_City"))

        date_str = ts.strftime("%d/%m/%Y") if ts else ""
        time_str = ts.strftime("%H:%M") if ts else ""

        if date_str != prev_date:
            rows += (
                f'<div style="text-align:center;margin:16px 0 8px">'
                f'<span style="background:#e0e0e0;color:#555;padding:2px 12px;'
                f'border-radius:20px;font-size:.72rem">{date_str}</span></div>'
            )
            prev_date = date_str

        is_inbound = m.direccion == "inbound"
        if is_inbound:
            bubble_bg = "#f0f2f5"
            bubble_color = "#1a1a2e"
            align = "flex-start"
            sender_label = "👤 Cliente"
        elif m.sender_type == "asesor":
            bubble_bg = "#d1e8ff"
            bubble_color = "#0d47a1"
            align = "flex-end"
            sender_label = f"🧑‍💼 {_e(m.sender_nombre or 'Asesor')}"
        else:
            bubble_bg = "#dcf8c6"
            bubble_color = "#1b5e20"
            align = "flex-end"
            sender_label = "🤖 Bot"

        body_escaped = _e(m.body).replace("\n", "<br>")
        rows += f"""<div style="display:flex;justify-content:{align};margin-bottom:8px;padding:0 12px">
            <div style="max-width:70%;background:{bubble_bg};color:{bubble_color};
                        padding:8px 12px;border-radius:12px;font-size:.85rem;line-height:1.5">
                <div style="font-size:.68rem;color:#888;margin-bottom:4px;font-weight:600">{sender_label}</div>
                {body_escaped}
                <div style="font-size:.68rem;color:#aaa;text-align:right;margin-top:4px">{time_str}</div>
            </div>
        </div>"""

    return rows


@router.get("/chat/{sesion_id}/mensajes", response_class=HTMLResponse)
def crm_mensajes_fragment(
    sesion_id: UUID,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = db.query(SesionIA).filter(SesionIA.id == sesion_id).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    mensajes = (
        db.query(Message)
        .filter(Message.sesion_id == sesion_id)
        .order_by(Message.created_at.asc())
        .all()
    )
    return HTMLResponse(_render_mensajes(mensajes))


@router.get("/chat/{sesion_id}", response_class=HTMLResponse)
def crm_chat_detalle(
    sesion_id: UUID,
    token: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = db.query(SesionIA).filter(SesionIA.id == sesion_id).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    lead = db.query(Lead).filter(Lead.telefono == sesion.telefono_cliente).first()
    mensajes = (
        db.query(Message)
        .filter(Message.sesion_id == sesion_id)
        .order_by(Message.created_at.asc())
        .all()
    )

    nombre = lead.nombre if lead else None
    destino = lead.destino_interes if lead else None
    estado_comercial = sesion.estado_comercial or "nuevo"
    score = sesion.score or 0
    canal_icon = "💬 Messenger" if (sesion.canal or "") == "messenger" else "📱 WhatsApp"
    sesion_id_str = str(sesion_id)

    # Opciones de estado para el selector
    opciones_estado = "".join(
        f'<option value="{e}" {"selected" if e == estado_comercial else ""}>'
        f'{e.replace("_", " ").title()}</option>'
        for e in ESTADOS_COMERCIALES
    )

    # Botones de acción
    if sesion.asesor_activo and sesion.asesor_nombre:
        btn_tomar = (
            f'<form method="post" action="/crm/chat/{sesion_id_str}/liberar?token={token}" style="display:inline">'
            f'<button class="btn btn-gray" type="submit">🔓 Liberar chat</button></form>'
        )
    elif sesion.asesor_activo:
        btn_tomar = (
            f'<form method="post" action="/crm/chat/{sesion_id_str}/liberar?token={token}" style="display:inline">'
            f'<button class="btn btn-gray" type="submit">🔓 Liberar</button></form>'
        )
    else:
        btn_tomar = (
            f'<form method="post" action="/crm/chat/{sesion_id_str}/tomar?token={token}" '
            f'style="display:inline" onsubmit="return confirm(\'¿Tomar este chat?\')">'
            f'<input type="hidden" name="asesor_nombre" id="asesor_nombre_input" value="">'
            f'<button class="btn btn-green" type="submit" onclick="'
            f'var n=prompt(\'Tu nombre:\',\'\');if(!n){{event.preventDefault();return false}};'
            f'document.getElementById(\'asesor_nombre_input\').value=n">🤝 Tomar chat</button></form>'
        )

    if sesion.asesor_activo:
        btn_bot = (
            f'<form method="post" action="/crm/chat/{sesion_id_str}/bot/activar?token={token}" style="display:inline">'
            f'<button class="btn btn-green" type="submit">▶ Reactivar bot</button></form>'
        )
    else:
        btn_bot = (
            f'<form method="post" action="/crm/chat/{sesion_id_str}/bot/pausar?token={token}" style="display:inline">'
            f'<button class="btn btn-orange" type="submit">⏸ Pausar bot</button></form>'
        )

    error_html = (
        f'<div style="background:#fce4ec;color:#b71c1c;padding:10px 14px;border-radius:8px;margin-bottom:12px">'
        f'⚠️ {_e(error)}</div>'
    ) if error else ""

    mensajes_html = _render_mensajes(mensajes)
    notas_val = _e(sesion.notas_internas or "")

    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Chat {_e(nombre or sesion.telefono_cliente[-8:])} — CRM</title>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
{_BASE_CSS}
.layout{{display:grid;grid-template-columns:1fr 320px;gap:16px;align-items:start}}
.chat-wrap{{background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);
           display:flex;flex-direction:column;height:calc(100vh - 170px)}}
.chat-body{{flex:1;overflow-y:auto;padding:12px 0;scroll-behavior:smooth}}
.chat-input{{padding:12px;border-top:1px solid #f0f2f5;display:flex;gap:8px}}
.chat-input textarea{{height:44px;resize:none;padding:10px 12px}}
.chat-input .btn{{padding:10px 18px;white-space:nowrap}}
.info-panel{{display:flex;flex-direction:column;gap:14px}}
.actions{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:4px}}
.field-row{{margin-bottom:12px}}
@media(max-width:768px){{.layout{{grid-template-columns:1fr}}
    .chat-wrap{{height:60vh}}.info-panel{{order:-1}}}}
</style>
</head>
<body>
<div class="header">
    <a href="/crm?token={token}">← CRM</a>
    <div style="flex:1;margin-left:8px">
        <h1>{_e(nombre) or _e(sesion.telefono_cliente)} &nbsp; {_badge_estado(estado_comercial)}</h1>
        <div style="font-size:.75rem;opacity:.8">{canal_icon} · {_e(sesion.telefono_cliente)}</div>
    </div>
    {_badge_bot(sesion.asesor_activo)}
</div>

<div class="container">
{error_html}

<div class="actions">
    {btn_tomar}
    {btn_bot}
</div>

<div class="layout">

    <!-- Chat -->
    <div class="chat-wrap">
        <div class="chat-body" id="chat-msgs"
             hx-get="/crm/chat/{sesion_id_str}/mensajes?token={token}"
             hx-trigger="every 8s"
             hx-swap="innerHTML"
             hx-on::after-request="var el=document.getElementById('chat-msgs');el.scrollTop=el.scrollHeight">
            {mensajes_html}
        </div>

        <div class="chat-input">
            <form method="post" action="/crm/chat/{sesion_id_str}/mensaje?token={token}"
                  style="display:flex;gap:8px;width:100%">
                <textarea name="body" placeholder="Escribe un mensaje para el cliente..."
                          required rows="1" style="flex:1"></textarea>
                <input type="hidden" name="asesor_nombre" id="sender_name"
                       value="{_e(sesion.asesor_nombre or '')}">
                <button class="btn btn-green" type="submit"
                        onclick="if(!document.getElementById('sender_name').value){{
                            var n=prompt('Tu nombre:','');
                            if(!n){{event.preventDefault();return}};
                            document.getElementById('sender_name').value=n
                        }}">
                    Enviar
                </button>
            </form>
        </div>
    </div>

    <!-- Panel de info -->
    <div class="info-panel">

        <div class="card">
            <h2>👤 Cliente</h2>
            <div class="field-row">
                <label>Teléfono</label>
                <div style="font-weight:600">{_e(sesion.telefono_cliente)}</div>
            </div>
            <div class="field-row">
                <label>Nombre</label>
                <div>{_e(nombre) or '<span style="color:#bbb">No registrado</span>'}</div>
            </div>
            <div class="field-row">
                <label>Destino de interés</label>
                <div>{_e(destino) or '<span style="color:#bbb">—</span>'}</div>
            </div>
            <div class="field-row">
                <label>Canal</label>
                <div>{canal_icon}</div>
            </div>
        </div>

        <div class="card">
            <h2>📊 Estado y Score</h2>
            <div class="field-row">
                <label>Estado comercial</label>
                <form method="post" action="/crm/chat/{sesion_id_str}/estado?token={token}">
                    <div style="display:flex;gap:6px">
                        <select name="estado" style="flex:1">{opciones_estado}</select>
                        <button class="btn btn-blue" type="submit" style="padding:6px 10px">✓</button>
                    </div>
                </form>
            </div>
            <div class="field-row">
                <label>Score</label>
                <div style="margin-bottom:8px">{_badge_score(score)}</div>
                <form method="post" action="/crm/chat/{sesion_id_str}/score?token={token}">
                    <div style="display:flex;gap:6px">
                        <input type="number" name="score" value="{score}"
                               min="0" max="300" style="flex:1" placeholder="Ajustar score">
                        <button class="btn btn-blue" type="submit" style="padding:6px 10px">✓</button>
                    </div>
                </form>
            </div>
            <div class="field-row">
                <label>Asesor asignado</label>
                <div style="font-weight:600">
                    {_e(sesion.asesor_nombre) or '<span style="color:#bbb">Sin asignar</span>'}
                </div>
            </div>
        </div>

        <div class="card">
            <h2>📝 Notas internas</h2>
            <form method="post" action="/crm/chat/{sesion_id_str}/nota?token={token}">
                <div class="field-row">
                    <textarea name="nota" placeholder="Ej: Cliente quiere 4 lugares, reserva el viernes..."
                              rows="3"></textarea>
                </div>
                <button class="btn btn-gray" type="submit" style="width:100%">Guardar nota</button>
            </form>
            {'<div style="margin-top:12px;font-size:.82rem;color:#444;white-space:pre-wrap;line-height:1.6;border-top:1px solid #f0f2f5;padding-top:10px">' + notas_val + '</div>' if notas_val else ''}
        </div>

    </div>
</div>
</div>

<script>
// Scroll inicial al fondo del chat
(function(){{
    var el = document.getElementById('chat-msgs');
    if (el) el.scrollTop = el.scrollHeight;
}})();
</script>
</body>
</html>""")


# ═══════════════════════════════════════════════════════════════════════════════
# ACCIONES (POST → redirect de vuelta al chat)
# ═══════════════════════════════════════════════════════════════════════════════

def _redirect_chat(sesion_id: str, token: str, error: str | None = None) -> RedirectResponse:
    url = f"/crm/chat/{sesion_id}?token={token}"
    if error:
        url += f"&error={html.escape(error)}"
    return RedirectResponse(url=url, status_code=303)


def _get_sesion(db: Session, sesion_id: UUID) -> SesionIA:
    sesion = db.query(SesionIA).filter(SesionIA.id == sesion_id).first()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return sesion


@router.post("/chat/{sesion_id}/tomar")
def crm_tomar(
    sesion_id: UUID,
    asesor_nombre: str = Form(...),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    try:
        tomar_chat(db, sesion, asesor_nombre.strip())
    except ValueError as e:
        return _redirect_chat(str(sesion_id), token, str(e))
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/liberar")
def crm_liberar(
    sesion_id: UUID,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    liberar_chat(db, sesion)
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/bot/pausar")
def crm_pausar_bot(
    sesion_id: UUID,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    pausar_bot(db, sesion)
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/bot/activar")
def crm_activar_bot(
    sesion_id: UUID,
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    reactivar_bot(db, sesion)
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/estado")
def crm_estado(
    sesion_id: UUID,
    estado: str = Form(...),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    try:
        actualizar_estado_comercial(db, sesion, estado)
    except ValueError as e:
        return _redirect_chat(str(sesion_id), token, str(e))
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/score")
def crm_score(
    sesion_id: UUID,
    score: int = Form(...),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    sesion = _get_sesion(db, sesion_id)
    actualizar_score(db, sesion, score)
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/nota")
def crm_nota(
    sesion_id: UUID,
    nota: str = Form(...),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    if not nota.strip():
        return _redirect_chat(str(sesion_id), token)
    sesion = _get_sesion(db, sesion_id)
    agregar_nota(db, sesion, nota)
    return _redirect_chat(str(sesion_id), token)


@router.post("/chat/{sesion_id}/mensaje")
def crm_mensaje(
    sesion_id: UUID,
    body: str = Form(...),
    asesor_nombre: str = Form(default="Asesor"),
    token: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _check_token(token)
    if not body.strip():
        return _redirect_chat(str(sesion_id), token)
    sesion = _get_sesion(db, sesion_id)
    try:
        enviar_mensaje_asesor(db, sesion, asesor_nombre.strip() or "Asesor", body.strip())
    except Exception as exc:
        logger.error("Error enviando mensaje del asesor: %s", exc)
        return _redirect_chat(str(sesion_id), token, "Error al enviar el mensaje")
    return _redirect_chat(str(sesion_id), token)
