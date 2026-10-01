# -*- coding: utf-8 -*-
"""
Bot de Telegram — avisos del Extractor de Documentos Digitales
============================================================
Servicio PEQUEÑO Y SEPARADO del Extractor -- vive en su propio Railway,
corriendo todo el tiempo (a diferencia del Extractor, este NO debe
dormirse nunca, para poder recibir avisos y responder preguntas a
cualquier hora).

Qué hace:
  1. Recibe avisos del Extractor (Local, .exe o Railway) en /aviso,
     cuando un lote arranca o termina -- los guarda en memoria.
  2. Recibe mensajes de Telegram en /webhook (cuando le escribes
     /estado, o le das clic a un botón).
  3. Responde:
     - Si hay 0 lotes activos: "no hay ningún proceso corriendo".
     - Si hay 1 solo: responde directo con el estado.
     - Si hay 2+ : muestra botones para elegir cuál, y responde
       el que elijas.
  4. Cuando le llega un aviso de "terminado", manda un mensaje
     AUTOMÁTICO sin que preguntes -- incluye un resumen y avisa fuerte
     si hubo muchos errores.

Variables de entorno necesarias (se configuran en Railway):
  TELEGRAM_BOT_TOKEN   -- el token que te dio BotFather
  TELEGRAM_CHAT_ID     -- tu chat_id personal (a quién avisarle)
  TELEGRAM_AVISO_TOKEN -- una clave inventada por ti, compartida con el
                          Extractor -- para que nadie más pueda mandarle
                          avisos falsos a este bot.
  UMBRAL_ERRORES_ALERTA (opcional, default 20) -- si el % de error de
                          un lote terminado supera esto, el aviso
                          automático se marca como alerta.

Arranque: gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
"""
import os
import json
import time
import threading
from pathlib import Path
from datetime import datetime

import requests as req_lib
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# Configuración (variables de entorno)
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_AVISO_TOKEN = os.environ.get("TELEGRAM_AVISO_TOKEN", "")
UMBRAL_ERRORES_ALERTA = float(os.environ.get("UMBRAL_ERRORES_ALERTA", "20"))

API_TELEGRAM = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

if not TELEGRAM_BOT_TOKEN:
    print("[arranque] AVISO: TELEGRAM_BOT_TOKEN no está configurado -- "
          "el bot no podrá mandar ni recibir mensajes de Telegram.", flush=True)


# ─────────────────────────────────────────────────────────────
# Estado en memoria -- "lotes activos" (uno por cada clave_lote distinta)
# y "usuarios registrados" (todo chat_id que haya escrito /start)
# ─────────────────────────────────────────────────────────────
# Se guarda también en disco (estado_lotes.json / usuarios_registrados.json)
# para no perder nada si Railway reinicia el servicio -- pero la fuente
# de verdad al responder preguntas es siempre lo último que haya en
# memoria.
LOCK = threading.Lock()
LOTES = {}  # clave_lote -> dict con toda la info del último aviso recibido

# ANTES: el bot solo mandaba mensajes a un TELEGRAM_CHAT_ID fijo (una
# sola persona, puesto a mano como variable de entorno) -- esto causaba
# 2 problemas reales: (1) aunque otra persona le escribiera /estado al
# bot, la respuesta igual le llegaba SOLO al dueño del chat_id fijo,
# nunca a quien preguntó; (2) no había forma de que varias personas
# recibieran los avisos automáticos de "lote terminado".
#
# AHORA: cualquiera que le escriba /start al bot queda REGISTRADO (su
# chat_id se guarda aquí) -- las respuestas a preguntas van siempre al
# chat de quien preguntó (eso ya lo hace Telegram por sí solo, con el
# chat_id que viene en cada mensaje), y los avisos AUTOMÁTICOS (lote
# terminado) se mandan a TODOS los chat_id registrados, no a uno fijo.
USUARIOS_REGISTRADOS = set()  # conjunto de chat_id (como string)

ARCHIVO_ESTADO = Path(__file__).resolve().parent / "estado_lotes.json"
ARCHIVO_USUARIOS = Path(__file__).resolve().parent / "usuarios_registrados.json"


def _cargar_estado_disco():
    global LOTES
    if ARCHIVO_ESTADO.exists():
        try:
            with open(ARCHIVO_ESTADO, "r", encoding="utf-8") as f:
                LOTES.update(json.load(f))
            print(f"[arranque] {len(LOTES)} lote(s) recuperados de estado_lotes.json", flush=True)
        except Exception as e:
            print(f"[arranque] no se pudo leer estado_lotes.json: {e}", flush=True)


def _guardar_estado_disco():
    try:
        with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
            json.dump(LOTES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[estado] no se pudo guardar estado_lotes.json: {e}", flush=True)


def _cargar_usuarios_disco():
    global USUARIOS_REGISTRADOS
    if ARCHIVO_USUARIOS.exists():
        try:
            with open(ARCHIVO_USUARIOS, "r", encoding="utf-8") as f:
                USUARIOS_REGISTRADOS.update(json.load(f))
            print(f"[arranque] {len(USUARIOS_REGISTRADOS)} usuario(s) registrados recuperados", flush=True)
        except Exception as e:
            print(f"[arranque] no se pudo leer usuarios_registrados.json: {e}", flush=True)


def _guardar_usuarios_disco():
    try:
        with open(ARCHIVO_USUARIOS, "w", encoding="utf-8") as f:
            json.dump(sorted(USUARIOS_REGISTRADOS), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[estado] no se pudo guardar usuarios_registrados.json: {e}", flush=True)


def _registrar_usuario(chat_id):
    """Agrega un chat_id a la lista de registrados (si no estaba ya) y
    lo persiste a disco. Devuelve True si era NUEVO (para poder avisarle
    distinto la primera vez, si se quisiera)."""
    chat_id = str(chat_id)
    with LOCK:
        es_nuevo = chat_id not in USUARIOS_REGISTRADOS
        USUARIOS_REGISTRADOS.add(chat_id)
        if es_nuevo:
            _guardar_usuarios_disco()
    return es_nuevo


_cargar_estado_disco()
_cargar_usuarios_disco()
# Compatibilidad hacia atrás: si TELEGRAM_CHAT_ID sigue configurado (el
# valor fijo de antes), se registra también como un usuario más -- así
# quien ya lo tenía puesto no deja de recibir avisos de un día para otro
# solo por este cambio, mientras las demás personas usan /start.
if TELEGRAM_CHAT_ID:
    _registrar_usuario(TELEGRAM_CHAT_ID)


# ─────────────────────────────────────────────────────────────
# Utilidades para hablar con la API de Telegram
# ─────────────────────────────────────────────────────────────
def _enviar_mensaje(chat_id, texto: str, botones=None):
    """Manda un mensaje a UN chat específico -- se usa para responder
    preguntas (siempre al chat_id de quien preguntó, nunca a uno fijo).
    `botones` es una lista de (texto_boton, callback_data) para armar un
    teclado inline -- se usa para el "¿cuál lote?" cuando hay varios
    activos."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("[telegram] no configurado o sin chat_id, no se puede enviar:", texto[:80], flush=True)
        return
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
    }
    if botones:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in botones]
        })
    try:
        req_lib.post(f"{API_TELEGRAM}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"[telegram] error enviando mensaje a {chat_id}: {e}", flush=True)


def _enviar_a_todos(texto: str):
    """Manda un mensaje AUTOMÁTICO (ej. "lote terminado") a TODOS los
    chat_id que se hayan registrado con /start -- reemplaza el
    comportamiento anterior de mandar siempre a un único
    TELEGRAM_CHAT_ID fijo. Si no hay nadie registrado todavía, no hace
    nada (no hay a quién avisarle)."""
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] no configurado, no se puede enviar a nadie:", texto[:80], flush=True)
        return
    with LOCK:
        destinatarios = list(USUARIOS_REGISTRADOS)
    if not destinatarios:
        print("[telegram] nadie registrado todavía (nadie ha escrito /start) -- aviso no enviado a nadie", flush=True)
        return
    for chat_id in destinatarios:
        _enviar_mensaje(chat_id, texto)


def _responder_callback(callback_query_id: str):
    """Le dice a Telegram 'ya procesé el clic del botón' -- sin esto,
    el botón se queda con el relojito de 'cargando' en el celular."""
    try:
        req_lib.post(f"{API_TELEGRAM}/answerCallbackQuery",
                      json={"callback_query_id": callback_query_id}, timeout=5)
    except Exception:
        pass


def _configurar_comandos():
    """Le dice a Telegram qué comandos mostrar como sugerencia cuando el
    usuario escribe "/" en el chat -- puramente cosmético/de usabilidad,
    no cambia qué es capaz de responder el bot (eso lo decide
    `_es_pregunta_de_estado` más abajo)."""
    if not TELEGRAM_BOT_TOKEN:
        return
    comandos = [
        {"command": "estado", "description": "Ver cómo va el proceso ahora mismo"},
        {"command": "eliminar", "description": "Borrar de la lista un lote ya terminado"},
        {"command": "help", "description": "Ver qué le puedes preguntar a este bot"},
    ]
    try:
        req_lib.post(f"{API_TELEGRAM}/setMyCommands", json={"commands": comandos}, timeout=10)
    except Exception as e:
        print(f"[telegram] no se pudo configurar el menú de comandos: {e}", flush=True)


# Palabras sueltas que, en cualquier combinación dentro del mensaje,
# hacen que se interprete como "pregunta por el estado" -- SIN llegar a
# ser interpretación de lenguaje natural real: es una lista fija de
# palabras clave, revisada con un chequeo simple de texto. Si el
# mensaje no contiene ninguna de estas, cae al mensaje de "no entendí".
_PALABRAS_CLAVE_ESTADO = (
    "estado", "avance", "proceso", "procesos", "progreso",
    "como vas", "cómo vas", "como va", "cómo va", "que tal", "qué tal",
    "listo", "termino", "terminó", "llevas", "cuanto va", "cuánto va",
)


def _es_pregunta_de_estado(texto: str) -> bool:
    """True si el texto (ya en minúsculas) parece estar preguntando por
    el avance -- ya sea el comando exacto /estado, o cualquier mensaje
    que contenga alguna de las palabras clave de arriba."""
    if texto in ("/estado",):
        return True
    return any(palabra in texto for palabra in _PALABRAS_CLAVE_ESTADO)


# ─────────────────────────────────────────────────────────────
# Armar el texto de estado de un lote
# ─────────────────────────────────────────────────────────────
def _etiqueta_lote(info: dict) -> str:
    """Ej: 'CM · Baru · 200 casos' -- lo que se ve en los botones y en
    los mensajes, para identificar de cuál lote se está hablando."""
    empresa = info.get("empresa") or "?"
    servidor = info.get("servidor") or "?"
    total = info.get("total_casos") or info.get("stats", {}).get("total") or "?"
    return f"{empresa} · {servidor} · {total} casos"


def _texto_estado(clave_lote: str) -> str:
    info = LOTES.get(clave_lote)
    if not info:
        return "No tengo información de ese lote."

    stats = info.get("stats", {})
    total = stats.get("total", 0)
    ok = stats.get("ok", 0)
    err = stats.get("err", 0)
    procesados = ok + err
    pct = round(procesados / total * 100, 1) if total else 0

    lineas = [
        f"<b>{_etiqueta_lote(info)}</b>",
        f"Fuente: {info.get('fuente', '?')}",
        f"Usuario: {info.get('usuario', '?')}",
        "",
    ]
    if info.get("evento") == "terminado":
        if info.get("detenido"):
            lineas.append("⏸ <b>Detenido por el usuario</b>")
        elif info.get("error_general"):
            lineas.append(f"⚠️ <b>Terminó con error:</b> {info['error_general']}")
        else:
            lineas.append("✅ <b>Terminado</b>")
    else:
        lineas.append("🔄 <b>En proceso</b>")

    lineas.append(f"Avance: {procesados} / {total} ({pct}%)")
    lineas.append(f"OK: {ok} · Error: {err}")
    ultima = info.get("_ultima_actualizacion")
    if ultima:
        lineas.append(f"\n<i>Última actualización: {ultima}</i>")
    return "\n".join(lineas)


def _lotes_terminados_recientes(horas=48):
    """Igual que `_lotes_activos_recientes`, pero solo los que YA
    terminaron (evento == "terminado") -- se usa para /eliminar, porque
    a propósito NO se permite borrar un lote que sigue "en proceso" (si
    de verdad se quedó atascado, lo correcto es revisarlo en el
    Extractor, no simplemente esconderlo del bot)."""
    ahora = time.time()
    terminados = {}
    for clave, info in LOTES.items():
        ts = info.get("_timestamp", 0)
        if ahora - ts <= horas * 3600 and info.get("evento") == "terminado":
            terminados[clave] = info
    return terminados


def _lotes_activos_recientes(horas=48):
    """Lotes de los que se tiene noticia en las últimas N horas -- para
    no ofrecer para siempre un lote de hace 2 semanas que ya nadie
    recuerda."""
    ahora = time.time()
    activos = {}
    for clave, info in LOTES.items():
        ts = info.get("_timestamp", 0)
        if ahora - ts <= horas * 3600:
            activos[clave] = info
    return activos


# ─────────────────────────────────────────────────────────────
# Endpoint: recibe avisos del Extractor
# ─────────────────────────────────────────────────────────────
@app.route("/aviso", methods=["POST"])
def recibir_aviso():
    datos = request.get_json(silent=True) or {}

    if TELEGRAM_AVISO_TOKEN and datos.get("token") != TELEGRAM_AVISO_TOKEN:
        return jsonify({"ok": False, "error": "token inválido"}), 403

    clave_lote = datos.get("clave_lote")
    if not clave_lote:
        return jsonify({"ok": False, "error": "falta clave_lote"}), 400

    # Caso especial: "borrado" -- cuando alguien le da al botón "Borrar
    # progreso" en el Extractor, ese lote deja de existir DE VERDAD, no
    # es solo "otro estado más" -- por eso se ELIMINA de la lista de
    # lotes activos (en vez de actualizarlo con datos.update, que lo
    # dejaría ahí mostrando "en proceso" o "terminado" para siempre,
    # aunque ya no exista nada que consultar). Sin esto, preguntar
    # "/estado" después de borrar un progreso seguiría mostrando el
    # último dato viejo, como si el lote siguiera vivo.
    if datos.get("evento") == "borrado":
        with LOCK:
            info_borrada = LOTES.pop(clave_lote, None)
            _guardar_estado_disco()
        if info_borrada:
            _enviar_a_todos(
                f"🗑 <b>Progreso borrado</b>\n\n"
                f"<b>{_etiqueta_lote({**info_borrada, **datos})}</b>\n"
                f"Fuente: {datos.get('fuente', info_borrada.get('fuente', '?'))}\n\n"
                f"Ya no hay ningún proceso activo para este lote."
            )
        return jsonify({"ok": True})

    with LOCK:
        info = LOTES.get(clave_lote, {})
        info.update(datos)
        info["_timestamp"] = time.time()
        info["_ultima_actualizacion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOTES[clave_lote] = info
        _guardar_estado_disco()

    # Aviso AUTOMÁTICO cuando el lote termina -- sin que el usuario
    # tenga que preguntar nada.
    if datos.get("evento") == "terminado":
        stats = datos.get("stats", {})
        total = stats.get("total", 0)
        err = stats.get("err", 0)
        pct_error = (err / total * 100) if total else 0

        if datos.get("detenido"):
            encabezado = "⏸ <b>Lote detenido por el usuario</b>"
        elif datos.get("error_general"):
            encabezado = "🛑 <b>Lote terminó con error general</b>"
        elif pct_error >= UMBRAL_ERRORES_ALERTA:
            encabezado = f"⚠️ <b>Lote terminado — {pct_error:.0f}% de error, revisa</b>"
        else:
            encabezado = "✅ <b>Lote terminado</b>"

        _enviar_a_todos(f"{encabezado}\n\n{_texto_estado(clave_lote)}")

    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────
# Endpoint: webhook de Telegram (mensajes y clics de botones)
# ─────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook_telegram():
    update = request.get_json(silent=True) or {}

    # Clic en un botón ("¿cuál lote?") -- responde al chat de quien le
    # dio clic, tomado del propio callback_query (no de un fijo).
    if "callback_query" in update:
        cq = update["callback_query"]
        data = cq.get("data", "")
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        _responder_callback(cq["id"])

        # El prefijo decide la acción: "ver:<clave>" (botones de /estado,
        # ya existía) o "borrar:<clave>" (botones nuevos de /eliminar).
        # Cualquier dato viejo sin prefijo (de un despliegue anterior a
        # este cambio) se trata como "ver", para no romper botones que
        # ya estuvieran mostrados en chats antiguos al momento de
        # actualizar el bot.
        if data.startswith("borrar:"):
            clave_lote = data[len("borrar:"):]
            with LOCK:
                info_borrada = LOTES.pop(clave_lote, None)
                if info_borrada:
                    _guardar_estado_disco()
            if info_borrada:
                _enviar_mensaje(
                    chat_id,
                    f"🗑 <b>Eliminado de la lista</b>\n\n<b>{_etiqueta_lote(info_borrada)}</b>"
                )
            else:
                _enviar_mensaje(chat_id, "Ese lote ya no estaba en la lista (puede que alguien más ya lo haya eliminado).")
        else:
            clave_lote = data[len("ver:"):] if data.startswith("ver:") else data
            _enviar_mensaje(chat_id, _texto_estado(clave_lote))
        return jsonify({"ok": True})

    # Mensaje de texto normal -- el chat_id de quien escribió viene en
    # el propio mensaje (message.chat.id), NUNCA se usa un chat_id fijo
    # para responder preguntas.
    mensaje = update.get("message", {})
    chat_id = mensaje.get("chat", {}).get("id")
    texto = (mensaje.get("text") or "").strip().lower()

    if not chat_id:
        # Update sin chat identificable (raro, pero no debe tronar) --
        # no hay a quién responder.
        return jsonify({"ok": True})

    if texto in ("/start", "/help", "/ayuda"):
        # /start registra a esta persona para que, de ahora en más,
        # también reciba los avisos AUTOMÁTICOS (lote terminado) -- antes
        # esos avisos solo le llegaban a un chat_id fijo puesto a mano.
        es_nuevo = _registrar_usuario(chat_id)
        saludo_registro = (
            "✅ Quedaste registrado -- de ahora en más también te voy a avisar "
            "automáticamente cuando un lote termine.\n\n"
            if es_nuevo else ""
        )
        _enviar_mensaje(
            chat_id,
            f"👋 Hola, soy el bot de avisos del Extractor de Documentos Digitales.\n\n"
            f"{saludo_registro}"
            "<b>Lo que me puedes preguntar:</b>\n"
            "/estado — o simplemente escribe algo como \"¿cómo vas?\", \"avance\" "
            "o \"cómo va el proceso\" — te digo el estado ahora mismo.\n"
            "/eliminar — borra de la lista un lote que YA terminó (no uno que sigue en proceso).\n\n"
            "También te aviso <b>sin que preguntes nada</b> cuando un lote termina "
            "(y te marco una alerta si tuvo muchos errores)."
        )
        return jsonify({"ok": True})

    if _es_pregunta_de_estado(texto):
        activos = _lotes_activos_recientes()
        if not activos:
            _enviar_mensaje(chat_id, "No tengo ningún proceso activo o reciente registrado.")
        elif len(activos) == 1:
            (unica_clave,) = activos.keys()
            _enviar_mensaje(chat_id, _texto_estado(unica_clave))
        else:
            botones = [(_etiqueta_lote(info), f"ver:{clave}") for clave, info in activos.items()]
            _enviar_mensaje(chat_id, "¿Cuál lote quieres consultar?", botones=botones)
        return jsonify({"ok": True})

    if texto in ("/eliminar", "/borrar", "/limpiar"):
        # A propósito, solo ofrece lotes YA TERMINADOS -- uno que sigue
        # "en proceso" no se deja eliminar desde aquí, ni aunque parezca
        # atascado; si de verdad se quedó colgado, lo correcto es
        # revisarlo en el Extractor (ahí vive el progreso real), no
        # simplemente esconderlo de la lista del bot.
        terminados = _lotes_terminados_recientes()
        if not terminados:
            _enviar_mensaje(chat_id, "No tengo ningún lote terminado para eliminar ahora mismo.")
        else:
            botones = [(_etiqueta_lote(info), f"borrar:{clave}") for clave, info in terminados.items()]
            _enviar_mensaje(chat_id, "¿Cuál lote terminado quieres eliminar de la lista?", botones=botones)
        return jsonify({"ok": True})

    _enviar_mensaje(chat_id, "No entendí ese mensaje. Escribe /estado para ver el avance.")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────
# Endpoints de salud / configuración
# ─────────────────────────────────────────────────────────────
@app.route("/")
def salud():
    return jsonify({
        "ok": True,
        "servicio": "Bot de Telegram - Extractor de Documentos Digitales",
        "telegram_configurado": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "lotes_en_memoria": len(LOTES),
    })


@app.route("/configurar-webhook")
def configurar_webhook():
    """Visita esta URL UNA VEZ (desde el navegador) después de desplegar,
    para decirle a Telegram dónde mandar los mensajes que te escriban Y
    para configurar el menú de comandos (/estado, /help) que aparece
    como sugerencia al escribir "/" en el chat. No hace falta volver a
    correrla salvo que cambie la URL del servicio."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"ok": False, "error": "TELEGRAM_BOT_TOKEN no configurado"}), 400
    # request.url_root arma la URL con el protocolo que Flask ve POR
    # DENTRO del contenedor -- y Railway (como casi cualquier plataforma
    # con proxy/balanceador delante) le entrega el trafico al contenedor
    # como http:// plano, aunque hacia afuera la URL publica sea https://.
    # Sin esto, Telegram rechaza el webhook con "An HTTPS URL must be
    # provided" -- se fuerza https:// a mano, ya que Railway SIEMPRE
    # expone sus dominios *.up.railway.app en https.
    host_sin_protocolo = request.url_root.split("://", 1)[-1].rstrip("/")
    url_publica = f"https://{host_sin_protocolo}/webhook"
    try:
        resp = req_lib.post(f"{API_TELEGRAM}/setWebhook", json={"url": url_publica}, timeout=10)
        _configurar_comandos()
        return jsonify({"ok": True, "webhook_configurado_en": url_publica, "respuesta_telegram": resp.json()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=puerto, debug=False)
