# Bot de Telegram — avisos del Extractor de Documentos Digitales

Servicio pequeño y separado del Extractor. Recibe avisos cuando un lote arranca o
termina (desde Local, `.exe` o Railway), y responde tus preguntas por Telegram.


## Avisos automáticos a todos los usuarios

Cualquier persona que escriba al bot (**/start**, **/estado**, o cualquier mensaje) queda **registrada**.

A **todos** los registrados les llegan, sin preguntar:

- ▶️ Lote iniciado  
- ✅ / ⚠️ / ⏸ Lote terminado (o detenido / con error)  
- 🗑 Progreso borrado  
- ⏱ Posible lote atascado  

`/estado` sigue disponible para consultar en cualquier momento.

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

## 6. Órdenes remotas: detener / reanudar / reiniciar desde Telegram

Además de consultar, **cualquier persona que sepa la contraseña de órdenes** puede dar órdenes a un lote:

| Comando (o frase natural) | Qué hace |
|---|---|
| `/detener` · «detén el proceso» | Detiene el lote en proceso (terminan los casos en curso; se guarda el avance). |
| `/reanudar` · `/iniciar` · «reanuda el proceso» | Retoma el lote detenido donde quedó, con los mismos ajustes con que arrancó. |
| `/reiniciar` · «reinicia el proceso» | Lo detiene, espera a que terminen los casos en curso y lo reanuda. |

Si hay varios lotes pregunta cuál; luego pide confirmar con un botón y, por último, la **contraseña** (los administradores de `TELEGRAM_ADMIN_IDS` no la necesitan). El bot borra el mensaje donde escribiste la contraseña.

### Cómo funciona (y por qué es seguro)
El Extractor corre en cada PC, detrás de su red, así que el bot **no** puede llamarlo. En cambio el
Extractor **pregunta** al bot cada ~10 s si hay órdenes para su `FUENTE_EXTRACTOR` (`POST /comandos`), la
ejecuta y reporta el resultado (`POST /comandos/resultado`), que te llega por Telegram. Nada entra al PC
desde afuera. Además:

- Hace falta la contraseña (`TELEGRAM_CLAVE_ORDENES`, definida solo en Railway, nunca en el código) o estar en `TELEGRAM_ADMIN_IDS`. Con 3 contraseñas incorrectas seguidas se cancela la orden y esa persona queda bloqueada 15 min (`MAX_INTENTOS_CLAVE`, `BLOQUEO_CLAVE_SEG`). Quien no tiene permiso ni contraseña recibe su `chat_id`.
- La orden solo la recoge quien conozca `TELEGRAM_AVISO_TOKEN`.
- Una orden que el Extractor no recoge en `VIGENCIA_ORDEN_SEG` (600 s) se descarta: nunca se ejecuta algo viejo.
- Con `TELEGRAM_WEBHOOK_SECRET` definido, el bot rechaza mensajes que no vengan de Telegram.

### Variables nuevas en el bot
| Variable | Valor |
|---|---|
| `TELEGRAM_CLAVE_ORDENES` | **La contraseña general** para dar órdenes (cualquiera que la sepa puede, sin importar su chat_id). Si no se define, solo los administradores pueden. |
| `TELEGRAM_ADMIN_IDS` | (opcional) chat_id, separados por coma, de quienes **no necesitan escribir la contraseña**. Si no se define, solo vale `TELEGRAM_CHAT_ID`. Quita tu id de aquí si quieres que hasta tú tengas que escribirla. |
| `TELEGRAM_WEBHOOK_SECRET` | **Recomendado.** Una clave inventada. Después de definirla, vuelve a abrir `/configurar-webhook` para registrarla en Telegram. |
| `VIGENCIA_ORDEN_SEG` | (opcional) segundos que una orden espera ser recogida. Por defecto 600. |

### Variables nuevas en el Extractor (opcionales)
`EXTRACTOR_ORDENES_CADA_SEG` (cada cuántos segundos pregunta al bot; 10 por defecto) y
`EXTRACTOR_REINICIO_ESPERA_MIN` (cuánto espera `/reiniciar` a que terminen los casos en curso; 15 por defecto).

### Límites a tener presentes
- **Reanudar/reiniciar solo funciona mientras el Extractor siga abierto con la sesión iniciada** (los datos del
  lote y la sesión viven en memoria). Si se cerró, el bot lo informa y hay que iniciar desde la pantalla.
  Si la sesión tiene más de ~7 h puede haber vencido: los casos darían error de sesión.
- **No se puede iniciar un lote nuevo** desde Telegram (necesita el Excel de casos).
- Si el PC está apagado o sin internet, la orden no se recoge y vence a los 10 min.

### Al actualizar el bot
1. Sube la carpeta a Railway (o haz push al repo).
2. Define `TELEGRAM_ADMIN_IDS` (y, recomendado, `TELEGRAM_WEBHOOK_SECRET`).
3. Abre de nuevo `https://tu-bot.up.railway.app/configurar-webhook` (registra el secreto y el menú con los comandos nuevos).
4. El Extractor debe ser la versión nueva (la que pregunta por órdenes).

## 7. Avisos nuevos
El aviso de «Lote terminado» ahora indica el **modo de búsqueda** («solo empresa logueada» o «con maestro»), cuántos
casos quedaron **sin descargar** y cuántos **incompletos con maestro en otra empresa**, y reenvía como archivos los
dos Excel para reintentar (formato de la plantilla, listos para volver a cargar).

## Pruebas
```
python -m unittest discover -s tests -v
```
