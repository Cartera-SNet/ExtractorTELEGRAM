# Bot de Telegram — avisos del Extractor de Documentos Digitales

Servicio pequeño y separado del Extractor. Recibe avisos cuando un lote arranca o
termina (desde Local, `.exe` o Railway), y responde tus preguntas por Telegram.

## 1. Desplegar en Railway

1. Crea un **nuevo proyecto** en Railway (aparte del Extractor — son 2 servicios
   distintos).
2. Sube esta carpeta completa (o conéctala a un repo de GitHub, igual que el Extractor).
3. En la pestaña **Variables**, configura:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | El token que te dio @BotFather |
| `TELEGRAM_CHAT_ID` | (opcional, solo para quien ya la tenía antes) Tu chat_id personal -- se registra automáticamente al arrancar, sin necesidad de volver a escribir `/start`. Las personas nuevas no necesitan esta variable: les basta con escribirle `/start` al bot desde su propio Telegram. |
| `TELEGRAM_AVISO_TOKEN` | Una clave inventada por ti (ej. una contraseña larga cualquiera) — la vas a usar también en el Extractor |
| `UMBRAL_ERRORES_ALERTA` | (opcional) porcentaje de error a partir del cual el aviso se marca como alerta. Por defecto 20 |

⚠️ **Importante:** este servicio **no** debe tener `sleepApplication: true` — ya viene
configurado en `false` en `railway.json`, no lo cambies. Debe estar siempre despierto
para poder recibir avisos y responder preguntas a cualquier hora.

4. Espera a que despliegue. Anota la URL pública que Railway le asigna (algo como
   `https://tu-bot-telegram.up.railway.app`).

## 2. Conectar el bot con Telegram (un solo paso, una sola vez)

Con el servicio ya desplegado, abre en el navegador:

```
https://tu-bot-telegram.up.railway.app/configurar-webhook
```

Debe responder algo como `{"ok": true, "webhook_configurado_en": "...", ...}`. Con esto,
Telegram ya sabe que debe mandarle tus mensajes a este servicio, y **además** deja
configurado el menú de comandos (verás `/estado` y `/help` como sugerencia al escribir
"/" en el chat). Solo hace falta hacerlo una vez (o de nuevo si el servicio cambia de URL).

## 3. Conectar el Extractor con este bot

En **cada** lugar donde corras el Extractor (Local, `.exe`, Railway), configura estas
variables de entorno:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_URL` | La URL pública de este servicio (ej. `https://tu-bot-telegram.up.railway.app`) |
| `TELEGRAM_AVISO_TOKEN` | La MISMA clave que pusiste en el paso 1 |
| `FUENTE_EXTRACTOR` | Un nombre para identificar de dónde viene (ej. `Local - PC oficina`, `.exe - Victor`, `Railway`) |

En Windows (Local o antes de abrir el `.exe`), esto se hace con:

```powershell
setx TELEGRAM_BOT_URL "https://tu-bot-telegram.up.railway.app"
setx TELEGRAM_AVISO_TOKEN "la-misma-clave-de-arriba"
setx FUENTE_EXTRACTOR "Local - PC oficina"
```

(hay que cerrar y volver a abrir la terminal/el `.exe` después de correr `setx`, para
que tome la variable nueva).

En Railway (si corres el Extractor ahí), se configuran igual que cualquier otra
variable de entorno del servicio, desde la pestaña **Variables**.

Si estas variables **no** se configuran en algún lugar, el Extractor sigue funcionando
exactamente igual que siempre — el aviso a Telegram queda desactivado en silencio, sin
ningún efecto en el procesamiento.

## 4. Varias personas usando el mismo bot

Cualquier persona (desde su propio Telegram, en su propio chat con el bot) le escribe
`/start` **una sola vez** -- eso la registra. Desde ahí:

- Si **pregunta** algo ("¿cómo vas?", "/estado"), la respuesta le llega **a ella**, no a
  ninguna otra persona.
- Cuando **cualquier** lote termina (sin importar en qué PC haya corrido), **todas** las
  personas registradas reciben el aviso automático.

No hace falta tocar el ZIP del Extractor para esto -- cada PC solo necesita sus propias
variables (`TELEGRAM_BOT_URL`, `TELEGRAM_AVISO_TOKEN` iguales en todas; `FUENTE_EXTRACTOR`
distinto en cada una, para identificarlas).

## 5. Usarlo

- Escríbele `/estado`, o simplemente algo natural como "¿cómo vas?", "avance", "cuánto
  llevas" o "cómo va el proceso" — el bot reconoce todas esas variantes.
- Si hay un solo lote corriendo (en cualquiera de las 3 fuentes), responde directo.
- Si hay varios a la vez, te pregunta cuál con botones (ej. "CM · Barú · 200 casos").
- Si no hay ningún lote activo o reciente, te lo dice claramente en vez de quedarse
  callado.
- Cuando un lote termina, te avisa **sin que preguntes nada** — y si el porcentaje de
  error supera el umbral configurado, el aviso viene marcado como alerta.
- `/help` (o `/start`) te recuerda en cualquier momento qué le puedes preguntar.
- `/eliminar` te deja borrar de la lista un lote que **ya terminó** (con botones para
  elegir cuál) — un lote que sigue "en proceso" no se puede eliminar desde aquí, a
  propósito: si de verdad se quedó atascado, hay que revisarlo en el Extractor. Pide
  confirmación (sí/no) antes de borrar de verdad.
- `/resumen` (o "cómo nos fue hoy") — balance de todos los lotes terminados en el día,
  agrupado por servidor, marcando con ⚠️ el que tuvo mucho error.
- `/quien` (o "¿quién está trabajando?") — lista las fuentes (PCs) con un proceso activo
  ahora mismo.
- `/imagen` (o "mándame una imagen") — una tarjeta tipo dashboard con la barra de
  progreso, OK/Error y tiempo estimado, generada con los mismos datos de `/estado` —
  **no** es una captura de pantalla del Extractor (eso no es posible desde aquí), es un
  gráfico.
- Si un lote lleva más de `UMBRAL_ATASCADO_MINUTOS` (por defecto 20) sin reportar
  ningún avance nuevo, el bot avisa que posiblemente se atascó — sin spamear, solo una
  vez por atasco (si se destraba y se vuelve a atascar después, puede avisar de nuevo).
- Cuando un lote termina con al menos 1 error, el Extractor adjunta el Excel de errores
  y el bot lo reenvía como archivo real de Telegram, no solo mencionado en el texto.

## Notas de seguridad

- El `TELEGRAM_AVISO_TOKEN` es tuyo, no de Telegram — inventa uno largo y no lo
  compartas. Sin él (o con uno incorrecto), el endpoint `/aviso` rechaza la petición.
- El `TELEGRAM_BOT_TOKEN` sí es el de Telegram — trátalo como una contraseña. Si alguna
  vez se expone (por ejemplo, pegado en un chat), regenéralo con BotFather
  (`/token` → `Revoke current token`).
