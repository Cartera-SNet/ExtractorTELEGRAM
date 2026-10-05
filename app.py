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
import io
import json
import time
import base64
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests as req_lib
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# Configuración (variables de entorno)
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_AVISO_TOKEN = os.environ.get("TELEGRAM_AVISO_TOKEN", "")
UMBRAL_ERRORES_ALERTA = float(os.environ.get("UMBRAL_ERRORES_ALERTA", "20"))
# Minutos sin recibir NINGÚN aviso de progreso de un lote "en proceso"
# para considerarlo posiblemente atascado. El Extractor manda un aviso
# de "progreso" cada 10 casos o cada 2 minutos (lo que pase primero,
# ver _avisar_telegram en el Extractor) -- si pasa MUCHO más que eso sin
# noticias, algo probablemente se congeló (red caída, Tesseract
# colgado, etc.) en vez de seguir avanzando en silencio.
UMBRAL_ATASCADO_MINUTOS = float(os.environ.get("UMBRAL_ATASCADO_MINUTOS", "20"))

API_TELEGRAM = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Railway corre los contenedores en hora UTC, sin importar dónde esté el
# usuario -- sin esto, "Última actualización" salía ~5 horas ADELANTADA
# respecto a la hora real de Colombia (ej. decía 15:50 cuando en
# Colombia eran las 10:52). Se fuerza a mano a UTC-5 (hora de Colombia,
# que no tiene horario de verano, así que este offset fijo es correcto
# todo el año) en vez de usar datetime.now() sin más, que toma la hora
# del sistema del contenedor -- esa es UTC en Railway, no la del
# usuario.
ZONA_HORARIA_COLOMBIA = timezone(timedelta(hours=-5))


def _ahora_colombia() -> datetime:
    return datetime.now(ZONA_HORARIA_COLOMBIA)

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

# HISTORIAL_TERMINADOS: a diferencia de LOTES (que SOBREESCRIBE el
# último estado de cada clave_lote -- si el mismo lote corre 2 veces en
# el día, la primera corrida se pierde), esto es un registro que
# SOLO CRECE, uno por cada vez que un lote termina de verdad. Es lo que
# permite un resumen diario real ("/resumen") que no dependa de qué
# siga "vivo" en LOTES en este momento. Se recorta a los últimos 500
# para no crecer sin límite -- de sobra para cualquier resumen diario
# o semanal razonable.
HISTORIAL_TERMINADOS = []  # [{fecha, hora, fuente, usuario, servidor, empresa, stats, clave_lote, detenido, error_general}, ...]
MAX_HISTORIAL = 500

ARCHIVO_ESTADO = Path(__file__).resolve().parent / "estado_lotes.json"
ARCHIVO_USUARIOS = Path(__file__).resolve().parent / "usuarios_registrados.json"
ARCHIVO_HISTORIAL = Path(__file__).resolve().parent / "historial_terminados.json"


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


def _cargar_historial_disco():
    global HISTORIAL_TERMINADOS
    if ARCHIVO_HISTORIAL.exists():
        try:
            with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
                HISTORIAL_TERMINADOS.extend(json.load(f))
            print(f"[arranque] {len(HISTORIAL_TERMINADOS)} entrada(s) de historial recuperadas", flush=True)
        except Exception as e:
            print(f"[arranque] no se pudo leer historial_terminados.json: {e}", flush=True)


def _guardar_historial_disco():
    try:
        with open(ARCHIVO_HISTORIAL, "w", encoding="utf-8") as f:
            json.dump(HISTORIAL_TERMINADOS[-MAX_HISTORIAL:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[estado] no se pudo guardar historial_terminados.json: {e}", flush=True)


def _registrar_en_historial(clave_lote, datos):
    """Se llama cada vez que llega un aviso 'terminado' -- agrega una
    entrada NUEVA al historial (nunca sobreescribe una anterior), para
    que /resumen pueda contar correctamente aunque el mismo lote se
    haya corrido más de una vez en el día."""
    ahora = _ahora_colombia()
    entrada = {
        "clave_lote": clave_lote,
        "fecha": ahora.strftime("%Y-%m-%d"),
        "hora": ahora.strftime("%H:%M:%S"),
        "fuente": datos.get("fuente", "?"),
        "usuario": datos.get("usuario", "?"),
        "servidor": datos.get("servidor", "?"),
        "empresa": datos.get("empresa", "?"),
        "stats": datos.get("stats", {}),
        "detenido": bool(datos.get("detenido")),
        "error_general": datos.get("error_general"),
    }
    with LOCK:
        HISTORIAL_TERMINADOS.append(entrada)
        del HISTORIAL_TERMINADOS[:-MAX_HISTORIAL]  # recorta por si acaso, aunque _guardar_historial_disco ya lo hace al guardar
        _guardar_historial_disco()


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
_cargar_historial_disco()
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
    """Manda un mensaje AUTOMÁTICO a TODOS los chat_id registrados
    (cualquier persona que haya escrito al bot al menos una vez:
    /start, /estado, etc.). Si no hay nadie registrado, no hace nada."""
    if not TELEGRAM_BOT_TOKEN:
        print("[telegram] no configurado, no se puede enviar a nadie:", texto[:80], flush=True)
        return
    with LOCK:
        destinatarios = list(USUARIOS_REGISTRADOS)
    if not destinatarios:
        print("[telegram] nadie registrado todavía -- aviso no enviado a nadie. "
              "Que cada persona escriba /start o cualquier mensaje al bot.", flush=True)
        return
    print(f"[telegram] enviando aviso automático a {len(destinatarios)} usuario(s)", flush=True)
    for chat_id in destinatarios:
        _enviar_mensaje(chat_id, texto)


def _enviar_foto(chat_id, contenido_bytes: bytes, texto_pie: str = ""):
    """Manda la tarjeta de estado. Si es GIF animado usa sendAnimation
    (el robotsito se mueve); si es PNG, sendPhoto."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    es_gif = contenido_bytes[:6] in (b"GIF87a", b"GIF89a")
    try:
        if es_gif:
            req_lib.post(
                f"{API_TELEGRAM}/sendAnimation",
                data={"chat_id": chat_id, "caption": texto_pie, "parse_mode": "HTML"},
                files={"animation": ("estado.gif", contenido_bytes, "image/gif")},
                timeout=30,
            )
        else:
            req_lib.post(
                f"{API_TELEGRAM}/sendPhoto",
                data={"chat_id": chat_id, "caption": texto_pie, "parse_mode": "HTML"},
                files={"photo": ("estado.png", contenido_bytes)},
                timeout=20,
            )
    except Exception as e:
        print(f"[telegram] error enviando foto/animación a {chat_id}: {e}", flush=True)


def _enviar_documento(chat_id, nombre_archivo: str, contenido_bytes: bytes, texto_pie: str = ""):
    """Manda un archivo (ej. el Excel de errores) a UN chat, vía
    sendDocument de Telegram -- a diferencia de sendMessage, este
    endpoint necesita multipart/form-data (el archivo va como
    'files', no como json)."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    try:
        req_lib.post(
            f"{API_TELEGRAM}/sendDocument",
            data={"chat_id": chat_id, "caption": texto_pie, "parse_mode": "HTML"},
            files={"document": (nombre_archivo, contenido_bytes)},
            timeout=20,
        )
    except Exception as e:
        print(f"[telegram] error enviando documento a {chat_id}: {e}", flush=True)


def _enviar_documento_a_todos(nombre_archivo: str, contenido_bytes: bytes, texto_pie: str = ""):
    """Igual que _enviar_a_todos, pero mandando un ARCHIVO (ej. el Excel
    de errores) en vez de solo texto -- a todos los chat_id registrados."""
    if not TELEGRAM_BOT_TOKEN:
        return
    with LOCK:
        destinatarios = list(USUARIOS_REGISTRADOS)
    if not destinatarios:
        return
    for chat_id in destinatarios:
        _enviar_documento(chat_id, nombre_archivo, contenido_bytes, texto_pie)


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
        {"command": "imagen", "description": "Una imagen con la barra de progreso del lote"},
        {"command": "resumen", "description": "Balance de todos los lotes terminados hoy"},
        {"command": "quien", "description": "Qué PCs tienen un proceso activo ahora"},
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

# ─────────────────────────────────────────────────────────────
# Router de intenciones -- reconoce preguntas NATURALES, no solo
# comandos exactos tipo /estado. Cada intención tiene su propia lista
# de palabras/frases -- si el mensaje contiene cualquiera de ellas
# (o es el comando exacto), se dispara esa intención. Intencionalmente
# simple (no es IA/NLP real, solo coincidencia de texto) -- pero cubre
# bastante bien cómo la gente realmente pregunta en el día a día, sin
# tener que acordarse de comandos exactos.
# ─────────────────────────────────────────────────────────────
_PALABRAS_CLAVE_RESUMEN = (
    "resumen", "resumen del dia", "resumen del día", "como fue el dia",
    "cómo fue el día", "como nos fue", "cómo nos fue", "balance del dia",
    "balance del día", "total de hoy", "cuantos lotes", "cuántos lotes",
)
_PALABRAS_CLAVE_QUIEN = (
    "/quien", "/quién", "/fuentes",
    "quien esta", "quién está", "quien está", "quién esta",
    "quien trabaja", "quién trabaja", "quienes estan", "quiénes están",
    "que pc", "qué pc", "cuales pc", "cuáles pc", "fuentes activas",
)
_PALABRAS_CLAVE_ELIMINAR = ("/eliminar", "/borrar", "/limpiar", "borra el lote", "elimina el lote", "quita el lote")
_PALABRAS_CLAVE_IMAGEN = (
    "/imagen", "/foto", "manda una imagen", "mandame una imagen", "mándame una imagen",
    "manda una foto", "mandame una foto", "mándame una foto", "como se ve", "cómo se ve",
    "muestrame", "muéstrame", "mandame el avance en imagen", "una imagen de como va",
)
_PALABRAS_CLAVE_AYUDA = ("/start", "/help", "/ayuda", "ayuda", "que puedes hacer", "qué puedes hacer", "que me puedes decir")


def _coincide_alguna(texto, lista):
    return any(palabra in texto for palabra in lista)


def _es_pregunta_de_estado(texto: str) -> bool:
    """True si el texto (ya en minúsculas) parece estar preguntando por
    el avance -- ya sea el comando exacto /estado, o cualquier mensaje
    que contenga alguna de las palabras clave de arriba."""
    if texto in ("/estado",):
        return True
    return _coincide_alguna(texto, _PALABRAS_CLAVE_ESTADO)


# ─────────────────────────────────────────────────────────────
# Imagen tipo dashboard del estado de un lote (no es una captura de
# pantalla del Extractor -- eso no es posible desde aquí, este bot no
# tiene ningún acceso a la pantalla de la PC donde corre el Extractor.
# Es un GRÁFICO generado con los mismos datos que ya se muestran en
# texto: barra de progreso, OK/Error, tiempo estimado.
# ─────────────────────────────────────────────────────────────
_CARPETA_FUENTES = Path(__file__).resolve().parent / "fuentes"


def _cargar_fuente(tamano, negrita=False):
    """Usa las fuentes Liberation Sans empaquetadas DENTRO del proyecto
    (no depende de qué fuentes tenga instaladas el contenedor de
    Railway, que pueden ser distintas a las de un entorno de pruebas) --
    y soportan tildes/ñ correctamente, a diferencia de la fuente interna
    por defecto de Pillow."""
    nombre = "LiberationSans-Bold.ttf" if negrita else "LiberationSans-Regular.ttf"
    ruta = _CARPETA_FUENTES / nombre
    try:
        return ImageFont.truetype(str(ruta), tamano)
    except Exception:
        return ImageFont.load_default()  # respaldo, por si el archivo no está disponible por algún motivo


def _dibujar_robot(draw, ox, oy, escala=1.0, modo="loading", fase=0.0):
    """Dibuja el robotsito del Extractor (formas simples, mismos colores).
    modo: idle | loading | done | error → color de ojos/antena.
    fase: 0..1 para animación (flotación y brillo de ojos)."""
    import math
    # Flotación vertical suave
    oy = oy + int(3 * math.sin(fase * 2 * math.pi))
    colores = {
        "idle":    ((99, 102, 241), (29, 78, 216)),
        "loading": ((16, 185, 129), (5, 150, 105)),
        "done":    ((16, 185, 129), (5, 150, 105)),
        "error":   ((239, 68, 68), (220, 38, 38)),
    }
    ojo, pupila = colores.get(modo, colores["loading"])
    # En loading: ojitos fijos en verde (solo flota el cuerpo)
    if modo == "loading":
        ojo = (16, 185, 129)
        pupila = (5, 150, 105)
    cuerpo = (55, 48, 163)      # #3730a3
    cuerpo_borde = (67, 56, 202)
    oscuro = (30, 27, 75)       # #1e1b4b
    oreja = (49, 46, 129)

    def s(v):
        return int(v * escala)

    def rr(x, y, w, h, rad, fill, outline=None):
        draw.rounded_rectangle(
            [ox + s(x), oy + s(y), ox + s(x + w), oy + s(y + h)],
            radius=max(1, s(rad)), fill=fill, outline=outline,
        )

    def cir(cx, cy, r, fill):
        draw.ellipse(
            [ox + s(cx - r), oy + s(cy - r), ox + s(cx + r), oy + s(cy + r)],
            fill=fill,
        )

    # Antena
    draw.line([ox + s(60), oy + s(22), ox + s(60), oy + s(11)], fill=ojo, width=max(2, s(3)))
    cir(60, 7, 5, ojo)
    # Cabeza
    rr(29, 22, 62, 44, 11, cuerpo, cuerpo_borde)
    rr(20, 30, 10, 20, 5, oreja, cuerpo_borde)
    rr(90, 30, 10, 20, 5, oreja, cuerpo_borde)
    # Visor + ojos
    rr(37, 31, 46, 22, 7, oscuro)
    cir(49, 42, 5.5, ojo)
    cir(71, 42, 5.5, ojo)
    cir(49, 42, 2.8, pupila)
    cir(71, 42, 2.8, pupila)
    cir(51, 40, 1.2, (255, 255, 255))
    cir(73, 40, 1.2, (255, 255, 255))
    # Boca / rejilla
    rr(44, 55, 32, 7, 3, oscuro)
    # Cuello
    rr(50, 66, 20, 12, 5, oreja)
    # Cuerpo
    rr(17, 78, 86, 56, 11, cuerpo, cuerpo_borde)
    rr(27, 86, 66, 40, 7, oscuro)
    # Luces del pecho
    for lx in (42, 60, 78):
        cir(lx, 106, 3.5, ojo)
    # Brazos
    rr(5, 90, 12, 28, 5, oreja)
    rr(103, 90, 12, 28, 5, oreja)
    # Piernas
    rr(35, 134, 18, 17, 5, oreja)
    rr(67, 134, 18, 17, 5, oreja)
    rr(32, 147, 23, 9, 4, cuerpo_borde)
    rr(65, 147, 23, 9, 4, cuerpo_borde)


def _formatear_duracion(segundos):
    segundos = max(0, int(segundos))
    h, resto = divmod(segundos, 3600)
    m, s = divmod(resto, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _generar_imagen_estado(clave_lote: str):
    """Genera tarjeta estilo Extractor. En proceso: GIF con robotsito animado.
    Terminado: PNG estático."""
    info = LOTES.get(clave_lote)
    if not info:
        return None

    import math

    stats = info.get("stats", {})
    total = stats.get("total", 0)
    ok = stats.get("ok", 0)
    err = stats.get("err", 0)
    procesados = ok + err
    pct = (procesados / total * 100) if total else 0
    evento = info.get("evento")

    inicio = info.get("_inicio_timestamp")
    ahora = time.time()
    transcurrido = (ahora - inicio) if inicio else 0

    restante_txt = "—"
    if inicio and total and procesados >= 1 and transcurrido >= 5:
        velocidad = procesados / transcurrido
        restantes = max(total - procesados, 0)
        if velocidad > 0 and restantes > 0:
            restante_txt = "~" + _formatear_duracion(restantes / velocidad)
        elif restantes == 0:
            restante_txt = "0:00"

    if evento == "terminado":
        if info.get("detenido"):
            estado_txt, color_estado, modo_robot = "Detenido por el usuario", (217, 119, 6), "error"
        elif info.get("error_general"):
            estado_txt, color_estado, modo_robot = "Terminó con error", (220, 38, 38), "error"
        else:
            estado_txt, color_estado, modo_robot = "Proceso finalizado", (16, 185, 129), "done"
    else:
        estado_txt, color_estado, modo_robot = "Buscando documentos…", (99, 102, 241), "loading"

    ancho, alto = 860, 420
    c_fondo = (245, 243, 255)
    c_tarjeta = (255, 255, 255)
    c_borde = (226, 232, 240)
    c_texto = (30, 41, 59)
    c_tenue = (100, 116, 139)
    c_accent = (99, 102, 241)
    c_ok, c_ok_bg = (22, 163, 74), (220, 252, 231)
    c_err, c_err_bg = (220, 38, 38), (254, 226, 226)
    c_total_bg = (241, 245, 249)
    c_barra_bg, c_barra = (226, 232, 240), (99, 102, 241)

    f_titulo = _cargar_fuente(22, negrita=True)
    f_negrita = _cargar_fuente(17, negrita=True)
    f_normal = _cargar_fuente(16)
    f_chico = _cargar_fuente(13)
    f_stat = _cargar_fuente(26, negrita=True)
    f_timer = _cargar_fuente(20, negrita=True)

    etiqueta = _etiqueta_lote(info)
    fuente = info.get("fuente") or "?"
    empresa = info.get("empresa") or ""
    sub = f"Fuente: {fuente}"
    if empresa:
        sub += f"  ·  {empresa}"

    robot_ox, robot_oy = 36, 36
    n_frames = 1 if evento == "terminado" else 8
    frames = []

    for i in range(n_frames):
        fase = i / max(n_frames, 1)
        frame = Image.new("RGB", (ancho, alto), c_fondo)
        d = ImageDraw.Draw(frame)
        d.rounded_rectangle([16, 16, ancho - 16, alto - 16], radius=20, fill=c_tarjeta, outline=c_borde, width=1)

        _dibujar_robot(d, robot_ox, robot_oy, escala=0.95, modo=modo_robot, fase=fase)

        timer_txt = _formatear_duracion(transcurrido) if inicio else "00:00"
        try:
            bbox_t = d.textbbox((0, 0), timer_txt, font=f_timer)
            tw_t = bbox_t[2] - bbox_t[0]
        except Exception:
            tw_t = 60
        centro_robot = robot_ox + int(60 * 0.95)
        # Timer un poco más abajo para no solaparse con el robot que flota
        d.text((centro_robot - tw_t // 2, robot_oy + int(158 * 0.95) + 10),
               timer_txt, font=f_timer, fill=c_accent)

        x = 180
        y = 40
        d.text((x, y), estado_txt, font=f_titulo, fill=color_estado)
        y += 30
        d.text((x, y), etiqueta, font=f_negrita, fill=c_texto)
        y += 24
        d.text((x, y), sub, font=f_chico, fill=c_tenue)
        y += 30
        barra_x0, barra_x1 = x, ancho - 40
        barra_y0, barra_y1 = y, y + 16
        d.rounded_rectangle([barra_x0, barra_y0, barra_x1, barra_y1], radius=8, fill=c_barra_bg)
        if total and procesados > 0:
            span = barra_x1 - barra_x0
            ancho_ok = int(span * (ok / total))
            ancho_err = int(span * (err / total))
            if ancho_ok > 0:
                d.rounded_rectangle([barra_x0, barra_y0, barra_x0 + max(10, ancho_ok), barra_y1], radius=8, fill=c_barra)
            if ancho_err > 0:
                x0e = barra_x0 + ancho_ok
                d.rectangle([x0e, barra_y0, x0e + ancho_err, barra_y1], fill=c_err)
        y = barra_y1 + 10
        d.text((x, y), f"{procesados} / {total}    ({pct:.0f}%)", font=f_normal, fill=c_tenue)
        y += 32
        gap = 12
        card_w = (ancho - x - 40 - gap * 2) // 3
        card_h = 72
        for j, (label, valor, bg, fg) in enumerate([
            ("TOTAL", str(total), c_total_bg, c_texto),
            ("OK", str(ok), c_ok_bg, c_ok),
            ("ERROR", str(err), c_err_bg, c_err),
        ]):
            cx = x + j * (card_w + gap)
            d.rounded_rectangle([cx, y, cx + card_w, y + card_h], radius=12, fill=bg)
            try:
                bb = d.textbbox((0, 0), valor, font=f_stat)
                tw = bb[2] - bb[0]
            except Exception:
                tw = len(valor) * 14
            d.text((cx + (card_w - tw) // 2, y + 10), valor, font=f_stat, fill=fg)
            try:
                bb2 = d.textbbox((0, 0), label, font=f_chico)
                tw2 = bb2[2] - bb2[0]
            except Exception:
                tw2 = len(label) * 7
            d.text((cx + (card_w - tw2) // 2, y + 46), label, font=f_chico, fill=c_tenue)
        y += card_h + 16
        d.rounded_rectangle([x, y, ancho - 40, y + 40], radius=10, fill=(248, 250, 252), outline=c_borde, width=1)
        d.text((x + 14, y + 11), f"Transcurrido: {_formatear_duracion(transcurrido)}", font=f_normal, fill=c_texto)
        d.text((x + 280, y + 11), f"Estimado restante: {restante_txt}", font=f_normal, fill=c_tenue)

        frames.append(frame)

    buffer = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(buffer, format="PNG")
    else:
        frames[0].save(
            buffer, format="GIF", save_all=True, append_images=frames[1:],
            duration=110, loop=0, optimize=False,
        )
    buffer.seek(0)
    return buffer.read()



def _texto_tiempo_estimado(info: dict) -> str:
    """Calcula hace cuánto empezó el lote y, con la velocidad real
    observada hasta ahora (procesados / tiempo transcurrido), estima
    cuánto falta para terminar -- NUNCA inventa un número si no hay
    suficiente información todavía (ej. recién empezó, o no se ha
    procesado ni 1 caso), para no mostrar una estimación engañosa."""
    inicio = info.get("_inicio_timestamp")
    stats = info.get("stats", {})
    total = stats.get("total", 0)
    procesados = stats.get("ok", 0) + stats.get("err", 0)
    if not inicio or not total or procesados < 1:
        return ""  # sin datos suficientes todavía para estimar nada serio

    transcurrido_seg = time.time() - inicio
    if transcurrido_seg < 5:
        return ""  # demasiado pronto -- cualquier estimación con <5s sería puro ruido

    velocidad = procesados / transcurrido_seg  # casos por segundo
    restantes = max(total - procesados, 0)
    if restantes == 0 or velocidad <= 0:
        return ""

    eta_seg = restantes / velocidad

    def _formatear(segundos):
        segundos = int(segundos)
        horas, resto = divmod(segundos, 3600)
        minutos, _ = divmod(resto, 60)
        if horas > 0:
            return f"{horas}h {minutos}min"
        if minutos > 0:
            return f"{minutos} min"
        return "menos de 1 min"

    return (
        f"Llevan {_formatear(transcurrido_seg)} corriendo · "
        f"Estimado para terminar: ~{_formatear(eta_seg)} más"
    )


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
    if info.get("evento") != "terminado":
        texto_eta = _texto_tiempo_estimado(info)
        if texto_eta:
            lineas.append(texto_eta)
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


def _revisar_lotes_atascados():
    """Revisa TODOS los lotes que siguen 'en proceso' (no terminados) y
    avisa si alguno lleva más de UMBRAL_ATASCADO_MINUTOS sin reportar
    ningún avance -- señal de que probablemente se congeló.

    IMPORTANTE sobre cuándo se llama esto: Railway puede DORMIR este
    servicio cuando no hay tráfico (sleepApplication=true) -- un hilo de
    fondo con un "cada N minutos" NO serviría, porque mientras el
    contenedor esté dormido, ningún hilo corre. Por eso esto se llama de
    forma OPORTUNISTA, dentro de /aviso -- cada vez que CUALQUIER
    Extractor manda un aviso (lo cual ya pasa periódicamente mientras
    algo esté corriendo), se aprovecha ese momento -- en el que el
    servicio ya está despierto de todas formas -- para revisar el
    estado de TODOS los lotes, no solo el que acaba de avisar.

    Para no mandar la misma alerta una y otra vez por el mismo lote
    atascado, se marca `_alertado_atascado` en el propio registro del
    lote -- se limpia solo cuando ese lote vuelve a mandar progreso real
    (ver recibir_aviso), así que si se destraba solo, puede volver a
    alertar si se vuelve a atascar más adelante."""
    ahora = time.time()
    con_LOCK_ya_tomado = False
    lotes_atascados = []
    with LOCK:
        for clave, info in LOTES.items():
            if info.get("evento") == "terminado":
                continue
            ts = info.get("_timestamp", 0)
            minutos_sin_noticias = (ahora - ts) / 60
            if minutos_sin_noticias >= UMBRAL_ATASCADO_MINUTOS and not info.get("_alertado_atascado"):
                info["_alertado_atascado"] = True
                lotes_atascados.append((clave, info, minutos_sin_noticias))
        if lotes_atascados:
            _guardar_estado_disco()
    for clave, info, minutos in lotes_atascados:
        _enviar_a_todos(
            f"⏱ <b>Posible lote atascado</b>\n\n"
            f"<b>{_etiqueta_lote(info)}</b>\n"
            f"Fuente: {info.get('fuente', '?')}\n\n"
            f"Lleva {minutos:.0f} minutos sin reportar ningún avance nuevo "
            f"-- si el Extractor sigue corriendo, puede valer la pena revisarlo."
        )


def _texto_quien_activo():
    """Lista las "fuentes" (PCs) con algún lote activo (no terminado)
    ahora mismo -- útil cuando trabajan varias personas y se quiere
    saber quién tiene el Extractor corriendo algo en este momento."""
    activos = _lotes_activos_recientes()
    en_proceso = {clave: info for clave, info in activos.items() if info.get("evento") != "terminado"}
    if not en_proceso:
        return "No hay ninguna fuente con un proceso activo ahora mismo."
    lineas = ["<b>🖥 Quién está trabajando ahora:</b>", ""]
    for info in en_proceso.values():
        stats = info.get("stats", {})
        total, ok, err = stats.get("total", 0), stats.get("ok", 0), stats.get("err", 0)
        procesados = ok + err
        lineas.append(
            f"• {info.get('fuente', '?')} — {_etiqueta_lote(info)} "
            f"({procesados}/{total})"
        )
    return "\n".join(lineas)


def _texto_resumen_dia(fecha_str=None):
    """Arma el texto de /resumen -- usa HISTORIAL_TERMINADOS (no LOTES),
    así cuenta bien aunque el mismo lote se haya corrido más de una vez
    en el día. `fecha_str` en formato YYYY-MM-DD; por defecto, hoy
    (hora de Colombia)."""
    fecha_str = fecha_str or _ahora_colombia().strftime("%Y-%m-%d")
    entradas_del_dia = [e for e in HISTORIAL_TERMINADOS if e.get("fecha") == fecha_str]

    if not entradas_del_dia:
        etiqueta_fecha = "hoy" if fecha_str == _ahora_colombia().strftime("%Y-%m-%d") else fecha_str
        return f"No hay ningún lote terminado registrado para {etiqueta_fecha}."

    total_casos = total_ok = total_err = 0
    detenidos = 0
    # Agrupado por servidor (ej. "Bahía: 3 lotes, 45 OK, 5 error") --
    # así, si un servidor en particular tuvo un mal día, se nota de una
    # vez sin tener que sumar a mano cada lote por separado.
    por_servidor = {}
    for e in entradas_del_dia:
        stats = e.get("stats", {})
        t, ok, err = stats.get("total", 0), stats.get("ok", 0), stats.get("err", 0)
        total_casos += t; total_ok += ok; total_err += err
        if e.get("detenido"):
            detenidos += 1
        servidor = e.get("servidor") or "?"
        acumulado = por_servidor.setdefault(servidor, {"lotes": 0, "ok": 0, "err": 0})
        acumulado["lotes"] += 1
        acumulado["ok"] += ok
        acumulado["err"] += err

    pct_error_global = (total_err / total_casos * 100) if total_casos else 0
    etiqueta_fecha = "Hoy" if fecha_str == _ahora_colombia().strftime("%Y-%m-%d") else fecha_str

    lineas = [
        f"<b>📊 Resumen — {etiqueta_fecha}</b>",
        "",
        f"Lotes terminados: {len(entradas_del_dia)}" + (f" ({detenidos} detenido(s) por el usuario)" if detenidos else ""),
        f"Casos totales: {total_casos} · OK: {total_ok} · Error: {total_err} ({pct_error_global:.0f}% de error)",
        "",
        "<b>Por servidor:</b>",
    ]
    for servidor, acc in sorted(por_servidor.items(), key=lambda kv: -kv[1]["err"]):
        pct = (acc["err"] / (acc["ok"] + acc["err"]) * 100) if (acc["ok"] + acc["err"]) else 0
        marca = " ⚠️" if pct >= UMBRAL_ERRORES_ALERTA else ""
        lineas.append(f"• {servidor}: {acc['lotes']} lote(s) — OK {acc['ok']} · Error {acc['err']}{marca}")

    return "\n".join(lineas)


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
        inicio_previo = info.get("_inicio_timestamp")  # se preserva entre avisos, ver abajo
        info.update(datos)
        info["_timestamp"] = time.time()
        info["_ultima_actualizacion"] = _ahora_colombia().strftime("%Y-%m-%d %H:%M:%S")
        # "_inicio_timestamp": el momento real en que este lote empezó
        # (el PRIMER aviso que se recibió de él) -- NECESARIO para
        # calcular el tiempo estimado de finalización (velocidad =
        # procesados / tiempo transcurrido desde el inicio). Se guarda
        # UNA SOLA VEZ: si ya existía de un aviso anterior, se conserva
        # (info.update(datos) no lo toca porque el Extractor nunca manda
        # un campo con ese nombre); si no existía (este es el primer
        # aviso que se ve de este lote), se fija a ahora mismo.
        info["_inicio_timestamp"] = inicio_previo or time.time()
        # Si este lote había sido marcado como "posiblemente atascado" y
        # ahora vuelve a mandar noticias (de lo que sea: progreso o
        # terminado), se limpia esa marca -- si se vuelve a atascar más
        # adelante, puede volver a alertar.
        info.pop("_alertado_atascado", None)
        LOTES[clave_lote] = info
        _guardar_estado_disco()

    # Aviso AUTOMÁTICO al INICIAR -- todos los registrados se enteran
    # sin tener que preguntar /estado.
    if datos.get("evento") == "iniciado":
        stats = datos.get("stats", {}) or {}
        total = stats.get("total", datos.get("total_casos", "?"))
        _enviar_a_todos(
            f"▶️ <b>Lote iniciado</b>\n\n"
            f"<b>{_etiqueta_lote(info)}</b>\n"
            f"Fuente: {datos.get('fuente', info.get('fuente', '?'))}\n"
            f"Casos: {total}\n\n"
            f"Te aviso cuando termine (o si se atasca). También puedes escribir /estado."
        )

    # Aviso AUTOMÁTICO cuando el lote termina -- sin que el usuario
    # tenga que preguntar nada.
    if datos.get("evento") == "terminado":
        _registrar_en_historial(clave_lote, datos)
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

        # Si el Extractor adjuntó el Excel de errores (solo lo hace
        # cuando hubo al menos 1 error -- ver _avisar_telegram en el
        # Extractor), se reenvía como ARCHIVO real a todos los
        # registrados, no solo mencionado en el texto. Viene en base64
        # porque todo el aviso se manda como JSON normal (no
        # multipart) -- se decodifica aquí antes de reenviarlo.
        excel_b64 = datos.get("excel_errores_b64")
        if excel_b64:
            try:
                contenido = base64.b64decode(excel_b64)
                nombre = datos.get("excel_errores_nombre") or f"errores_{clave_lote}.xlsx"
                _enviar_documento_a_todos(nombre, contenido, f"📎 Excel de errores — {_etiqueta_lote(info)}")
            except Exception as e:
                print(f"[telegram] no se pudo decodificar/enviar el Excel de errores: {e}", flush=True)

    # Oportunista: ya que el servicio está despierto atendiendo este
    # aviso, se aprovecha para revisar TODOS los lotes (no solo este) en
    # busca de alguno que lleve mucho tiempo sin reportar nada.
    _revisar_lotes_atascados()

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
        if chat_id:
            _registrar_usuario(chat_id)
        _responder_callback(cq["id"])

        # El prefijo decide la acción:
        #   "ver:<clave>"            -> botones de /estado (ya existía)
        #   "borrar:<clave>"         -> PRIMER clic en /eliminar -- ahora
        #                               NO borra de una vez, pide confirmar
        #   "confirmar-borrar:<clave>" -> SEGUNDO clic, confirmando -- recién
        #                               aquí se borra de verdad
        #   "cancelar-borrar"        -> el usuario se arrepintió, no se borra nada
        # Cualquier dato viejo sin prefijo (de un despliegue anterior a
        # este cambio) se trata como "ver", para no romper botones que
        # ya estuvieran mostrados en chats antiguos al momento de
        # actualizar el bot.
        if data.startswith("confirmar-borrar:"):
            clave_lote = data[len("confirmar-borrar:"):]
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
        elif data == "cancelar-borrar":
            _enviar_mensaje(chat_id, "De acuerdo, no se eliminó nada.")
        elif data.startswith("borrar:"):
            # PRIMER clic -- no se borra todavía, se pide confirmar. Esto
            # evita borrar algo sin querer con un solo clic apresurado.
            clave_lote = data[len("borrar:"):]
            info = LOTES.get(clave_lote)
            if not info:
                _enviar_mensaje(chat_id, "Ese lote ya no está en la lista.")
            else:
                _enviar_mensaje(
                    chat_id,
                    f"¿Seguro que quieres eliminar este lote de la lista?\n\n<b>{_etiqueta_lote(info)}</b>",
                    botones=[("✅ Sí, eliminar", f"confirmar-borrar:{clave_lote}"), ("❌ No, cancelar", "cancelar-borrar")],
                )
        elif data.startswith("img:"):
            clave_lote = data[len("img:"):]
            imagen = _generar_imagen_estado(clave_lote)
            if imagen:
                _enviar_foto(chat_id, imagen)
            else:
                _enviar_mensaje(chat_id, "No se pudo generar la imagen de ese lote.")
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

    # CUALQUIER mensaje registra al usuario para avisos automáticos
    # (antes solo /start lo hacía: quien solo usaba /estado nunca recibía
    # "lote terminado" push).
    es_nuevo = _registrar_usuario(chat_id)

    if texto in _PALABRAS_CLAVE_AYUDA or _coincide_alguna(texto, _PALABRAS_CLAVE_AYUDA):
        saludo_registro = (
            "✅ Quedaste registrado -- de ahora en más te aviso automáticamente "
            "cuando un lote inicie o termine (igual que a los demás).\n\n"
            if es_nuevo else
            "✅ Ya estabas registrado -- sigues recibiendo los avisos automáticos.\n\n"
        )
        _enviar_mensaje(
            chat_id,
            f"👋 Hola, soy el bot de avisos del Extractor de Documentos Digitales.\n\n"
            f"{saludo_registro}"
            "<b>Lo que me puedes preguntar</b> (no hace falta el comando exacto, "
            "también entiendo frases naturales):\n"
            "• \"¿cómo vas?\", \"avance\" — el estado ahora mismo (/estado).\n"
            "• \"resumen del día\", \"cómo nos fue hoy\" — el balance de todos los "
            "lotes terminados hoy (/resumen).\n"
            "• \"¿quién está trabajando?\" — qué PCs tienen algo activo (/quien).\n"
            "• \"mándame una imagen\" — una tarjeta con la barra de progreso "
            "del lote (/imagen; no es una captura de pantalla del Extractor, "
            "es un gráfico generado con los mismos datos).\n"
            "• \"elimina el lote\" — borra de la lista un lote ya terminado, "
            "pidiendo confirmación antes (/eliminar).\n\n"
            "También te aviso <b>sin que preguntes nada</b> (a todos los registrados) "
            "cuando un lote <b>inicia</b> o <b>termina</b> (con alerta si hubo muchos errores), "
            "si se borró el progreso, o si un lote lleva mucho tiempo sin reportar avance."
        )
        return jsonify({"ok": True})

    if _coincide_alguna(texto, _PALABRAS_CLAVE_RESUMEN):
        _enviar_mensaje(chat_id, _texto_resumen_dia())
        return jsonify({"ok": True})

    if _coincide_alguna(texto, _PALABRAS_CLAVE_QUIEN):
        _enviar_mensaje(chat_id, _texto_quien_activo())
        return jsonify({"ok": True})

    if _coincide_alguna(texto, _PALABRAS_CLAVE_IMAGEN):
        activos = _lotes_activos_recientes()
        if not activos:
            _enviar_mensaje(chat_id, "No tengo ningún proceso activo o reciente para mostrar en imagen.")
        elif len(activos) == 1:
            (unica_clave,) = activos.keys()
            imagen = _generar_imagen_estado(unica_clave)
            if imagen:
                _enviar_foto(chat_id, imagen)
            else:
                _enviar_mensaje(chat_id, "No se pudo generar la imagen de ese lote.")
        else:
            botones = [(_etiqueta_lote(info), f"img:{clave}") for clave, info in activos.items()]
            _enviar_mensaje(chat_id, "¿De cuál lote quieres la imagen?", botones=botones)
        return jsonify({"ok": True})

    if _coincide_alguna(texto, _PALABRAS_CLAVE_ELIMINAR):
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

    _enviar_mensaje(chat_id, "No entendí ese mensaje. Escribe /ayuda para ver qué me puedes preguntar.")
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
