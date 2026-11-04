# -*- coding: utf-8 -*-
"""Pruebas del bot: órdenes remotas (detener/reanudar/reiniciar). Sin red: se simula Telegram.
Ejecutar:  python -m unittest discover -s tests -v"""
import os, sys, time, base64, tempfile, unittest, pathlib
from unittest import mock
os.environ.update({"TELEGRAM_BOT_TOKEN": "tok-telegram", "TELEGRAM_AVISO_TOKEN": "clave", "TELEGRAM_ADMIN_IDS": "111",
                   "TELEGRAM_CHAT_ID": "111", "TELEGRAM_WEBHOOK_SECRET": ""})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as bot

TMP = pathlib.Path(tempfile.mkdtemp())
for nombre in ("ARCHIVO_ESTADO", "ARCHIVO_USUARIOS", "ARCHIVO_HISTORIAL", "ARCHIVO_ORDENES"):
    setattr(bot, nombre, TMP / (nombre.lower() + ".json"))


def mensaje(chat, texto, mid=1):
    return {"message": {"chat": {"id": chat}, "text": texto, "message_id": mid}}


def clic(chat, data):
    return {"callback_query": {"id": "cb1", "data": data, "message": {"chat": {"id": chat}}}}


class Base(unittest.TestCase):
    def setUp(self):
        bot.LOTES.clear(); bot.ORDENES.clear(); bot.ESPERA_CLAVE.clear(); bot.INTENTOS_CLAVE.clear()
        bot.TELEGRAM_WEBHOOK_SECRET = ""
        bot.TELEGRAM_CLAVE_ORDENES = ""
        bot.TELEGRAM_ADMIN_IDS = {"111"}
        self.enviados = []
        self.p1 = mock.patch.object(bot, "_enviar_mensaje", lambda chat, texto, botones=None: self.enviados.append((str(chat), texto, botones)))
        self.p2 = mock.patch.object(bot, "_responder_callback", lambda *a, **k: None)
        self.p1.start(); self.p2.start()
        self.c = bot.app.test_client()

    def tearDown(self):
        self.p1.stop(); self.p2.stop()

    def lote(self, clave="L1", fuente="PC-A", evento="progreso", **extra):
        bot.LOTES[clave] = {"fuente": fuente, "empresa": "CM", "servidor": "Baru", "total_casos": 635, "evento": evento,
                            "stats": {"total": 635, "ok": 6, "err": 0}, "_timestamp": time.time(), **extra}

    def hablar(self, chat, texto, mid=1):
        return self.c.post("/webhook", json=mensaje(chat, texto, mid))

    def boton(self, chat, data):
        return self.c.post("/webhook", json=clic(chat, data))


class TestPermisos(Base):
    def test_quien_no_es_admin_no_puede_dar_ordenes_y_se_le_dice_su_chat_id(self):
        self.lote()
        self.hablar(999, "/detener")
        self.assertEqual(bot.ORDENES, {})
        chat, texto, botones = self.enviados[-1]
        self.assertEqual(chat, "999"); self.assertIn("999", texto); self.assertIsNone(botones)

    def test_un_no_admin_tampoco_puede_forzar_la_orden_con_un_callback_falso(self):
        self.lote()
        self.boton(999, "okord:d:L1")
        self.assertEqual(bot.ORDENES, {})

    def test_sin_admins_configurados_nadie_puede(self):
        bot.TELEGRAM_ADMIN_IDS = set()
        self.lote()
        self.hablar(111, "/detener")
        self.assertEqual(bot.ORDENES, {})

    def test_consultar_sigue_abierto_para_todos(self):
        self.lote()
        self.hablar(999, "/estado")
        self.assertIn("En proceso", self.enviados[-1][1])


class TestFlujoDeOrden(Base):
    def test_detener_pide_confirmacion_y_solo_al_confirmar_se_encola(self):
        self.lote()
        self.hablar(111, "/detener")
        chat, texto, botones = self.enviados[-1]
        self.assertIn("DETENER", texto)
        self.assertEqual(botones[0][1], "okord:d:L1")
        self.assertEqual(bot.ORDENES, {})                     # todavía nada
        self.boton(111, "okord:d:L1")
        (orden,) = bot.ORDENES.values()
        self.assertEqual((orden["comando"], orden["fuente"], orden["estado"], orden["pedido_por"]), ("detener", "PC-A", "pendiente", "111"))

    def test_cancelar_no_encola(self):
        self.lote()
        self.hablar(111, "/detener"); self.boton(111, "cancelar-orden")
        self.assertEqual(bot.ORDENES, {})

    def test_detener_con_varios_lotes_pregunta_cual_y_luego_confirma(self):
        self.lote("L1", "PC-A"); self.lote("L2", "PC-B")
        self.hablar(111, "detén el proceso")
        botones = self.enviados[-1][2]
        self.assertEqual({b[1] for b in botones}, {"ord:d:L1", "ord:d:L2"})
        self.boton(111, "ord:d:L2")
        self.assertEqual(self.enviados[-1][2][0][1], "okord:d:L2")

    def test_detener_sin_lotes_en_proceso(self):
        self.lote(evento="terminado")
        self.hablar(111, "/detener")
        self.assertIn("No hay ningún lote en proceso", self.enviados[-1][1])

    def test_reanudar_y_reiniciar_se_reconocen(self):
        self.lote(evento="terminado", detenido=True)
        for texto, corto in (("/reanudar", "r"), ("reinicia el proceso", "x"), ("/iniciar", "r"), ("/reiniciar", "x")):
            self.hablar(111, texto)
            self.assertEqual(self.enviados[-1][2][0][1], f"okord:{corto}:L1", texto)

    def test_preguntas_normales_no_se_confunden_con_ordenes(self):
        self.lote()
        for texto in ("¿cómo vas?", "avance", "para qué sirve esto", "cuánto falta para terminar"):
            self.hablar(111, texto)
        self.assertEqual(bot.ORDENES, {})


class TestColaParaElExtractor(Base):
    def _orden(self):
        self.lote()
        self.hablar(111, "/detener"); self.boton(111, "okord:d:L1")
        return next(iter(bot.ORDENES.values()))

    def poll(self, fuente="PC-A", token="clave"):
        return self.c.post("/comandos", json={"token": token, "fuente": fuente})

    def test_token_incorrecto_se_rechaza(self):
        self._orden()
        self.assertEqual(self.poll(token="mala").status_code, 403)

    def test_solo_recibe_la_orden_la_fuente_correcta_y_una_sola_vez(self):
        o = self._orden()
        self.assertEqual(self.poll("PC-OTRA").get_json()["comandos"], [])
        r = self.poll().get_json()["comandos"]
        self.assertEqual(r, [{"id": o["id"], "comando": "detener", "clave_lote": "L1"}])
        self.assertEqual(self.poll().get_json()["comandos"], [])          # ya entregada: no se repite
        self.assertEqual(bot.ORDENES[o["id"]]["estado"], "entregada")

    def test_orden_vieja_no_se_entrega(self):
        o = self._orden()
        bot.ORDENES[o["id"]]["ts"] -= bot.VIGENCIA_ORDEN_SEG + 5
        self.assertEqual(self.poll().get_json()["comandos"], [])
        self.assertEqual(bot.ORDENES[o["id"]]["estado"], "vencida")

    def test_resultado_avisa_a_quien_la_pidio(self):
        o = self._orden(); self.poll()
        self.enviados.clear()
        r = self.c.post("/comandos/resultado", json={"token": "clave", "id": o["id"], "ok": True, "mensaje": "Deteniendo."})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(bot.ORDENES[o["id"]]["estado"], "ejecutada")
        self.assertTrue(any(c == "111" and "Deteniendo." in t for c, t, _ in self.enviados))

    def test_resultado_fallido_se_marca_y_se_informa(self):
        o = self._orden(); self.poll()
        self.c.post("/comandos/resultado", json={"token": "clave", "id": o["id"], "ok": False, "mensaje": "Sesión vencida."})
        self.assertEqual(bot.ORDENES[o["id"]]["estado"], "fallida")
        self.assertIn("❌", self.enviados[-1][1])

    def test_resultado_con_token_malo_o_id_desconocido(self):
        o = self._orden()
        self.assertEqual(self.c.post("/comandos/resultado", json={"token": "x", "id": o["id"], "ok": True}).status_code, 403)
        self.assertEqual(self.c.post("/comandos/resultado", json={"token": "clave", "id": "nada", "ok": True}).status_code, 404)


class TestContrasenaGeneral(Base):
    """Cualquier persona puede dar órdenes si escribe la contraseña general; los admins no la necesitan."""
    CLAVE = "P@ss2026"

    def setUp(self):
        super().setUp()
        bot.TELEGRAM_CLAVE_ORDENES = self.CLAVE
        self.borrados = []
        self.p3 = mock.patch.object(bot, "_borrar_mensaje", lambda chat, mid: self.borrados.append((str(chat), mid)))
        self.p3.start()
        self.lote()

    def tearDown(self):
        self.p3.stop(); super().tearDown()

    def hasta_pedir_clave(self, chat=999):
        self.hablar(chat, "/detener"); self.boton(chat, "okord:d:L1")

    def test_quien_no_es_admin_si_puede_ordenar_con_la_contrasena(self):
        self.hasta_pedir_clave()
        self.assertEqual(bot.ORDENES, {})                              # todavía no: falta la contraseña
        self.assertIn("contraseña", self.enviados[-1][1])
        self.hablar(999, self.CLAVE, mid=55)
        (orden,) = bot.ORDENES.values()
        self.assertEqual((orden["comando"], orden["pedido_por"], orden["estado"]), ("detener", "999", "pendiente"))
        self.assertIn(("999", 55), self.borrados)                      # el mensaje con la clave se borra

    def test_contrasena_incorrecta_no_encola_y_tambien_se_borra(self):
        self.hasta_pedir_clave()
        self.hablar(999, "otra-cosa", mid=7)
        self.assertEqual(bot.ORDENES, {})
        self.assertIn("1/3", self.enviados[-1][1]); self.assertIn(("999", 7), self.borrados)

    def test_la_contrasena_distingue_mayusculas(self):
        self.hasta_pedir_clave()
        self.hablar(999, self.CLAVE.lower())
        self.assertEqual(bot.ORDENES, {})

    def test_tres_fallos_cancelan_la_orden_y_bloquean(self):
        self.hasta_pedir_clave()
        for _ in range(3):
            self.hablar(999, "mala")
        self.assertIn("bloqueado", self.enviados[-1][1])
        self.hablar(999, "/detener")                                   # bloqueado: ni siquiera inicia el flujo
        self.assertIn("Demasiados intentos", self.enviados[-1][1])
        self.hablar(999, self.CLAVE)                                    # y la clave correcta ahora tampoco sirve
        self.assertEqual(bot.ORDENES, {})
        bot.INTENTOS_CLAVE["999"]["hasta"] = time.time() - 1            # pasa el bloqueo
        self.hasta_pedir_clave(); self.hablar(999, self.CLAVE)
        self.assertEqual(len(bot.ORDENES), 1)

    def test_el_bloqueo_de_uno_no_afecta_a_los_demas(self):
        self.hasta_pedir_clave(999)
        for _ in range(3):
            self.hablar(999, "mala")
        self.hasta_pedir_clave(888); self.hablar(888, self.CLAVE)
        self.assertEqual([o["pedido_por"] for o in bot.ORDENES.values()], ["888"])

    def test_los_administradores_no_necesitan_contrasena(self):
        self.hablar(111, "/detener"); self.boton(111, "okord:d:L1")
        (orden,) = bot.ORDENES.values()
        self.assertEqual(orden["pedido_por"], "111")

    def test_la_contrasena_pendiente_vence(self):
        self.hasta_pedir_clave()
        bot.ESPERA_CLAVE["999"]["ts"] -= bot.VIGENCIA_CLAVE_SEG + 1
        self.hablar(999, self.CLAVE)
        self.assertEqual(bot.ORDENES, {})                               # vencida: la clave suelta no hace nada
        self.assertNotIn("999", bot.ESPERA_CLAVE)

    def test_sin_orden_pendiente_escribir_la_clave_no_hace_nada(self):
        self.hablar(999, self.CLAVE)
        self.assertEqual(bot.ORDENES, {}); self.assertEqual(self.borrados, [])

    def test_cancelar_y_otros_comandos_abandonan_la_orden_pendiente(self):
        self.hasta_pedir_clave()
        self.hablar(999, "/cancelar")
        self.assertNotIn("999", bot.ESPERA_CLAVE)
        self.hasta_pedir_clave()
        self.hablar(999, "/estado")                                     # otro comando: se abandona y se atiende
        self.assertNotIn("999", bot.ESPERA_CLAVE); self.assertIn("En proceso", self.enviados[-1][1])
        self.hablar(999, self.CLAVE); self.assertEqual(bot.ORDENES, {})

    def test_la_contrasena_nunca_se_repite_en_ningun_mensaje_del_bot(self):
        self.hasta_pedir_clave(); self.hablar(999, "mala"); self.hablar(999, self.CLAVE)
        self.assertFalse(any(self.CLAVE in t for _, t, _ in self.enviados))

    def test_sin_contrasena_definida_y_sin_ser_admin_no_se_puede(self):
        bot.TELEGRAM_CLAVE_ORDENES = ""
        self.hablar(999, "/detener"); self.boton(999, "okord:d:L1")
        self.assertEqual(bot.ORDENES, {})


class TestWebhookSecretoYAvisos(Base):
    def test_con_secreto_configurado_se_rechaza_un_mensaje_sin_el(self):
        bot.TELEGRAM_WEBHOOK_SECRET = "s3"
        self.assertEqual(self.c.post("/webhook", json=mensaje(111, "/estado")).status_code, 403)
        ok = self.c.post("/webhook", json=mensaje(111, "/estado"), headers={"X-Telegram-Bot-Api-Secret-Token": "s3"})
        self.assertEqual(ok.status_code, 200)

    def test_aviso_terminado_muestra_pendientes_y_reenvia_los_dos_excel(self):
        enviados_docs = []
        with mock.patch.object(bot, "_enviar_documento_a_todos", lambda nombre, cont, pie="": enviados_docs.append((nombre, cont, pie))), \
             mock.patch.object(bot, "_enviar_a_todos", lambda texto: self.enviados.append(("todos", texto, None))):
            self.c.post("/aviso", json={
                "token": "clave", "evento": "terminado", "clave_lote": "L9", "fuente": "PC-A", "empresa": "CMB", "servidor": "Bahia",
                "total_casos": 10, "stats": {"total": 10, "ok": 8, "err": 2}, "modo_busqueda": "solo empresa logueada",
                "sin_descargar": 2, "incompletos_maestro": 5,
                "excel_errores_b64": base64.b64encode(b"AAA").decode(), "excel_errores_nombre": "casos_sin_descargar_L9.xlsx",
                "excel_maestro_b64": base64.b64encode(b"BBB").decode(), "excel_maestro_nombre": "casos_incompletos_con_maestro_L9.xlsx"})
        texto = next(t for c, t, _ in self.enviados if c == "todos")
        self.assertIn("solo empresa logueada", texto); self.assertIn("2 sin descargar", texto); self.assertIn("5 incompletos", texto)
        self.assertEqual([(n, c) for n, c, _ in enviados_docs], [("casos_sin_descargar_L9.xlsx", b"AAA"), ("casos_incompletos_con_maestro_L9.xlsx", b"BBB")])


if __name__ == "__main__":
    unittest.main()
