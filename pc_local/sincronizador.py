
"""Sincronizador entre la PC local y el servidor de la Estación Agrícola IoT.

Envía mediciones y promedios, consulta comandos remotos y verifica su aplicación.
"""

import ipaddress
import json
import os
import sqlite3
import time
import tomllib
import uuid
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
import requests
# Archivos y frecuencias del sincronizador

BASE = Path(__file__).resolve().parent

DB = BASE / "agro_local.db"
CONFIG = BASE / "config_sincronizador.json"
CONFIG_LOCAL = BASE / "config_local.json"
ESTADO = BASE / "estado_sincronizacion.json"

# Frecuencias en segundos.
# Estos tiempos regulan el sincronizador, no el muestreo del Arduino.
CADA_LECTURAS = 3
CADA_RESUMEN = 20
CADA_COMANDOS = 3
CADA_DIAGNOSTICO = 30
# Configuración y credenciales

if not DB.exists():
    raise SystemExit(
        "[ERROR][SQLITE] No existe agro_local.db. "
        "Iniciá basesdedatos.py."
    )

if not CONFIG.exists():
    raise SystemExit(
        "[ERROR][CONFIG] No existe config_sincronizador.json."
    )

cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

URL = cfg["url_servidor"].rstrip("/")
TOKEN = cfg["token_api"]

# Validar dirección del servidor.
u = urlsplit(URL)

if u.scheme not in ("http", "https") or not u.hostname:
    raise SystemExit("[ERROR][CONFIG] url_servidor no válida.")

# HTTP sin cifrar solo en localhost o redes privadas.
if u.scheme == "http":
    try:
        ip = ipaddress.ip_address(u.hostname)
        local = ip.is_private or ip.is_loopback
    except ValueError:
        local = u.hostname.lower() == "localhost"

    if not local:
        raise SystemExit(
            "[ERROR][SEGURIDAD] Por internet se requiere HTTPS."
        )

if len(TOKEN) < 24:
    raise SystemExit(
        "[ERROR][CONFIG] token_api no configurado."
    )

# Leer contraseña MQTT desde la configuración existente.
SECRETS = BASE / ".streamlit" / "secrets.toml"

clave_secrets = None

if SECRETS.exists():
    try:
        datos = tomllib.loads(
            SECRETS.read_text(encoding="utf-8")
        )
        clave_secrets = datos.get("mqtt_password")

    except (OSError, ValueError) as exc:
        print("[AVISO][CONFIG] Error leyendo secrets.toml:", exc)

config_local = {}
if CONFIG_LOCAL.exists():
    config_local = json.loads(CONFIG_LOCAL.read_text(encoding="utf-8"))

clave_mqtt = (
    cfg.get("mqtt_password")
    or config_local.get("mqtt_password")
    or clave_secrets
    or os.environ.get("MQTT_PASSWORD")
)

if not clave_mqtt:
    clave_mqtt = getpass("Contraseña MQTT: ")
if not clave_mqtt:
    raise SystemExit("[ERROR][MQTT] No se ingresó la contraseña del broker.")

MQTT_HOST = cfg.get("mqtt_host", "127.0.0.1")
MQTT_USER = cfg.get("mqtt_usuario", "agro")
MQTT_PUERTO = int(cfg.get("mqtt_puerto", 1883))

# Crear sesión HTTP reutilizable.
sesion = requests.Session()

sesion.headers.update({
    "Authorization": "Bearer " + TOKEN
})
# Comunicación con el servidor

def api(metodo, ruta, **kwargs):
    """Realiza solicitudes HTTP al Nivel 3."""

    respuesta = sesion.request(
        metodo,
        URL + ruta,
        timeout=12,
        **kwargs
    )

    if not respuesta.ok:
        detalle = respuesta.text[:200].replace("\n", " ")

        raise RuntimeError(
            f"HTTP {respuesta.status_code} en {ruta}: {detalle}"
        )

    return respuesta.json()
# Estado de la sincronización

def guardar_estado(estado):
    """Guarda el último registro enviado correctamente."""

    temporal = ESTADO.with_suffix(".tmp")

    temporal.write_text(
        json.dumps(estado, indent=2),
        encoding="utf-8"
    )

    temporal.replace(ESTADO)


def leer_estado():
    """Recupera el punto donde quedó la sincronización."""

    if ESTADO.exists():
        return json.loads(
            ESTADO.read_text(encoding="utf-8")
        )

    return {
        "ultimo_id_subido": 0,
        "ultima_ventana_epoch": 0
    }
# Consultas a la base local

def query(sql, args=()):
    """Consulta SQLite sin alterar sus registros."""

    with sqlite3.connect(DB, timeout=5) as db:

        db.row_factory = sqlite3.Row

        filas = db.execute(sql, args).fetchall()

        return [dict(f) for f in filas]


def ultima_lectura_local():
    """Obtiene el último ID y su fecha."""

    filas = query("""
        SELECT id, fecha
        FROM mediciones
        ORDER BY id DESC
        LIMIT 1
    """)

    return filas[0] if filas else None
# Envío de mediciones al servidor

def subir_lecturas(estado):
    """Envía únicamente las lecturas aún no sincronizadas."""

    filas = query("""
        SELECT
            id,
            fecha,
            humedad_suelo,
            luminosidad,
            temperatura_suelo,
            temperatura_aire,
            humedad_aire,
            intervalos
        FROM mediciones
        WHERE id > ?
        ORDER BY id ASC
        LIMIT 200
    """, (estado["ultimo_id_subido"],))

    # Si no existen nuevas mediciones, no se envía nada.
    if not filas:
        return

    # La API usa nombres de campo propios; acá adaptamos los de SQLite local.
    lecturas = []
    for fila in filas:
        try:
            intervalos = json.loads(fila["intervalos"] or "[]")
        except (TypeError, ValueError):
            raise RuntimeError(f"Intervalos inválidos en la lectura {fila['id']}")

        lecturas.append({
            "origen_id": int(fila["id"]),
            "fecha_hora": fila["fecha"],
            "humedad_suelo_adc": fila["humedad_suelo"],
            "luminosidad_adc": fila["luminosidad"],
            "temperatura_suelo_c": fila["temperatura_suelo"],
            "temperatura_aire_c": fila["temperatura_aire"],
            "humedad_aire_pct": fila["humedad_aire"],
            "intervalos_s": intervalos,
        })

    respuesta = api(
        "POST",
        "/api/lecturas/lote",
        json={"lecturas": lecturas}
    )

    # Solo avanzamos el cursor si el servidor respondió OK.
    if respuesta.get("ok") is not True:
        raise RuntimeError(
            "El servidor no confirmó el lote de mediciones."
        )

    estado["ultimo_id_subido"] = lecturas[-1]["origen_id"]

    guardar_estado(estado)

    print(
        f"[NIVEL 3] {len(filas)} lecturas enviadas; "
        f"último ID {estado['ultimo_id_subido']}"
    )
# Consolidación de ventanas de diez minutos

def consolidar_y_subir(estado):
    """Calcula promedios de ventanas completas de diez minutos."""

    comienzo_actual = int(time.time()) // 600 * 600

    desde = estado["ultima_ventana_epoch"]

    filas = query("""
        SELECT
          (CAST(strftime('%s', fecha) AS INTEGER)/600)*600
              AS inicio_epoch,

          COUNT(*) AS cantidad_registros,

          AVG(humedad_suelo)
              AS humedad_suelo_adc,

          AVG(luminosidad)
              AS luminosidad_adc,

          AVG(temperatura_suelo)
              AS temperatura_suelo_c,

          AVG(temperatura_aire)
              AS temperatura_aire_c,

          AVG(humedad_aire)
              AS humedad_aire_pct

        FROM mediciones

        WHERE CAST(strftime('%s', fecha) AS INTEGER) >= ?
          AND CAST(strftime('%s', fecha) AS INTEGER) < ?

        GROUP BY 1
        ORDER BY 1
    """, (desde, comienzo_actual))

    for fila in filas:

        inicio = fila.pop("inicio_epoch")

        fila["inicio_utc"] = datetime.fromtimestamp(
            inicio, timezone.utc
        ).isoformat()

        fila["fin_utc"] = datetime.fromtimestamp(
            inicio + 600, timezone.utc
        ).isoformat()

        # Enviar el resumen al Nivel 3.
        api("POST", "/api/resumenes", json=fila)

        # Registrar solamente ventanas enviadas correctamente.
        estado["ultima_ventana_epoch"] = inicio + 600

        guardar_estado(estado)

        print(
            f"[NUBE] Resumen de {fila['inicio_utc']} "
            f"enviado ({fila['cantidad_registros']} registros)"
        )

    if not filas and comienzo_actual > desde:

        estado["ultima_ventana_epoch"] = comienzo_actual

        guardar_estado(estado)
# Entrega de comandos y confirmaciones MQTT

def enviar_mqtt(sensor, segundos):
    """Envía un intervalo y espera la confirmación MQTT."""

    listo = Event()
    confirmado = Event()
    fallos = []

    cliente = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="agro-sync-" + uuid.uuid4().hex[:8]
    )

    cliente.username_pw_set(MQTT_USER, clave_mqtt)

    # Confirmación de conexión al broker.
    def conectado(cli, ud, flags, reason_code, properties):
        """Se suscribe al tópico de confirmaciones cuando conecta MQTT."""

        if reason_code.is_failure:

            fallos.append(
                f"Broker rechazó conexión: {reason_code}"
            )

            listo.set()

        else:

            codigo, _ = cli.subscribe(
                "agro/estado/intervalos",
                qos=1
            )

            if codigo != mqtt.MQTT_ERR_SUCCESS:

                fallos.append(
                    "No se pudo suscribir a las confirmaciones"
                )

                listo.set()

    # Confirmación de suscripción.
    def suscrito(cli, ud, mid, reason_codes, properties):
        """Comprueba que el broker haya aceptado la suscripción."""

        if any(rc.is_failure for rc in reason_codes):
            fallos.append(
                "Broker rechazó la suscripción"
            )

        listo.set()

    # Mensaje de confirmación del ESP8266.
    def mensaje(cli, ud, msg):
        """Detecta el acuse que confirma el cambio aplicado por el Nano."""

        try:
            d = json.loads(
                msg.payload.decode("utf-8")
            )

            if (
                d.get("sensor") == sensor
                and d.get("segundos") == segundos
                and d.get("aplicado") is True
            ):
                confirmado.set()

        except (ValueError, TypeError, UnicodeDecodeError):
            pass

    cliente.on_connect = conectado
    cliente.on_subscribe = suscrito
    cliente.on_message = mensaje

    comienzo = time.monotonic()

    try:
        cliente.connect(
            MQTT_HOST,
            MQTT_PUERTO,
            30
        )

        cliente.loop_start()

        # Esperamos a estar suscritos antes de publicar.
        if not listo.wait(6) or fallos:

            return (
                "error",
                "; ".join(fallos)
                or "MQTT no confirmó la suscripción"
            )

        print(
            f"[MQTT] Enviando sensor {sensor} = {segundos} s"
        )

        envio = cliente.publish(
            "agro/config/intervalos",
            f"{sensor} {segundos}",
            qos=1,
            retain=False
        )

        envio.wait_for_publish(timeout=5)

        if not envio.is_published():

            return (
                "error",
                "MQTT no confirmó la publicación"
            )

        # Esperar hasta 15 segundos por confirmación.
        if confirmado.wait(15):

            demora = time.monotonic() - comienzo

            return (
                "confirmado",
                f"ACK recibido después de {demora:.1f} s"
            )

        return (
            "sin_confirmacion",
            "No llegó ACK MQTT del ESP8266 en 15 segundos"
        )

    except (OSError, RuntimeError, ValueError) as exc:

        return (
            "error",
            f"Fallo de MQTT: {exc}"
        )

    finally:

        try:
            cliente.disconnect()
            cliente.loop_stop()
        except Exception:
            pass
# Consulta de comandos remotos

def procesar_comandos():
    """Busca comandos pendientes en el Nivel 3."""

    respuesta = api(
        "GET",
        "/api/comandos/pendientes"
    )

    comando = respuesta.get("comando") if isinstance(respuesta, dict) else None
    if not comando:
        return

    sensor = comando["sensor"]
    segundos = comando["segundos"]

    print(
        f"[NUBE] Comando pendiente: "
        f"sensor {sensor} = {segundos} s"
    )

    resultado, detalle = enviar_mqtt(
        sensor,
        segundos
    )

    # Informar resultado al Nivel 3.
    api(
        "POST",
        f"/api/comandos/{comando['id']}/resultado",
        json={
            "estado": resultado,
            "detalle": detalle
        }
    )

    print(
        f"[NUBE] Resultado: {resultado} ({detalle})"
    )
# Diagnóstico del broker MQTT

def comprobar_mqtt():
    """Comprueba si el broker acepta la conexión.

    No modifica los intervalos ni publica comandos.
    """

    conectado = Event()
    resultado = ["Sin respuesta"]

    cliente = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="agro-diag-" + uuid.uuid4().hex[:8]
    )

    cliente.username_pw_set(
        MQTT_USER,
        clave_mqtt
    )

    def al_conectar(cli, ud, flags, reason_code, properties):
        """Guarda el resultado de la prueba de conexión al broker."""

        if not reason_code.is_failure:
            resultado[0] = "OK"
        else:
            resultado[0] = (
                f"RECHAZADO: {reason_code}"
            )

        conectado.set()

    cliente.on_connect = al_conectar

    try:
        cliente.connect(
            MQTT_HOST,
            MQTT_PUERTO,
            5
        )

        cliente.loop_start()

        conectado.wait(3)

        return resultado[0]

    except (OSError, ValueError, RuntimeError) as exc:

        return f"ERROR: {exc}"

    finally:

        try:
            cliente.disconnect()
            cliente.loop_stop()
        except Exception:
            pass
# Diagnóstico general del sistema

def diagnostico(estado):
    """Muestra el estado incluso cuando no hay datos nuevos."""

    hora = datetime.now().astimezone().strftime(
        "%H:%M:%S"
    )

    print(f"\n[DIAG] Chequeo de las {hora}")
    # Comprobar SQLite
    try:

        ultima = ultima_lectura_local()

        if ultima is None:

            print(
                "[DIAG][SQLITE] No hay registros."
            )

        else:

            ultimo_id = int(ultima["id"])

            enviado = int(
                estado["ultimo_id_subido"]
            )

            pendientes = max(
                0,
                ultimo_id - enviado
            )

            print(
                f"[DIAG][SQLITE] "
                f"último ID local={ultimo_id}, "
                f"último enviado={enviado}, "
                f"pendientes={pendientes}"
            )

            if enviado > ultimo_id:

                print(
                    "[AVISO][ESTADO] El ID enviado supera "
                    "al ID local. ¿Se recreó agro_local.db?"
                )

            # Revisar antigüedad de la última medición.
            try:

                fecha = datetime.fromisoformat(
                    str(ultima["fecha"]).replace(
                        "Z",
                        "+00:00"
                    )
                )

                if fecha.tzinfo is None:
                    fecha = fecha.replace(
                        tzinfo=timezone.utc
                    )

                antiguedad = (
                    datetime.now(timezone.utc) - fecha
                ).total_seconds()

                if antiguedad > 30:

                    print(
                        f"[AVISO][ORIGEN] Última medición "
                        f"local de hace {int(antiguedad)} s."
                    )

                    print(
                        "[AVISO][ORIGEN] Revisá "
                        "basesdedatos.py, MQTT y ESP8266."
                    )

            except ValueError:

                print(
                    "[AVISO][SQLITE] Fecha de medición "
                    "no reconocida."
                )

    except (sqlite3.Error, OSError) as exc:

        print(
            "[ERROR][SQLITE] No pude consultar SQLite:",
            exc
        )
    # Comprobar servidor HTTP
    try:

        respuesta = sesion.get(
            URL + "/health",
            timeout=4
        )

        if respuesta.status_code == 200:

            print(
                "[DIAG][SERVIDOR] OK: responde /health"
            )

        else:

            print(
                f"[ERROR][SERVIDOR] HTTP "
                f"{respuesta.status_code}"
            )

    except requests.RequestException as exc:

        print(
            "[ERROR][SERVIDOR] No responde:",
            exc
        )
    # Comprobar Mosquitto
    estado_mqtt = comprobar_mqtt()

    print(
        f"[DIAG][MQTT] {estado_mqtt}"
    )

    if estado_mqtt != "OK":

        print(
            "[AVISO][MQTT] Revisá Mosquitto antes "
            "de configurar sensores."
        )

    print("-" * 58)
# Ejecución de tareas con reintentos

def ejecutar(etiqueta, funcion, *args):
    """Evita que un fallo temporal cierre el sincronizador."""

    try:

        funcion(*args)

    except (
        requests.RequestException,
        sqlite3.Error,
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError
    ) as exc:

        print(
            f"[ERROR][{etiqueta}] "
            f"{type(exc).__name__}: {exc}"
        )

        print(
            f"[DIAG] Se reintentará {etiqueta} "
            f"en el próximo ciclo."
        )
# Bucle principal

def main():
    """Coordina el envío de datos, los resúmenes, los comandos y el diagnóstico periódico."""

    print(
        "Estación agrícola - "
        "Sincronizador con diagnóstico automático"
    )

    print(
        "Servidor:",
        URL,
        "| Broker local:",
        MQTT_HOST
    )

    print(
        "Diagnóstico automático cada 30 segundos."
    )

    # Recuperar estado anterior.
    estado = leer_estado()

    # Comprobar todo al iniciar.
    diagnostico(estado)

    # Los tres procesos comienzan inmediatamente.
    t_lectura = 0.0
    t_resumen = 0.0
    t_poll = 0.0

    # Próximo diagnóstico en 30 segundos.
    t_diag = time.monotonic()

    while True:

        ahora = time.monotonic()

        # Enviar mediciones pendientes.
        if ahora - t_lectura >= CADA_LECTURAS:

            t_lectura = ahora

            ejecutar(
                "ENVIO LECTURAS",
                subir_lecturas,
                estado
            )

        # Enviar resumen de diez minutos.
        if ahora - t_resumen >= CADA_RESUMEN:

            t_resumen = ahora

            ejecutar(
                "RESUMEN 10 MIN",
                consolidar_y_subir,
                estado
            )

        # Consultar configuraciones pendientes.
        if ahora - t_poll >= CADA_COMANDOS:

            t_poll = ahora

            ejecutar(
                "COMANDOS",
                procesar_comandos
            )

        # Mostrar estado general.
        if ahora - t_diag >= CADA_DIAGNOSTICO:

            t_diag = ahora

            diagnostico(estado)

        time.sleep(1)
# Inicio del programa

if __name__ == "__main__":
    # Permite ejecutar el sincronizador directamente desde la terminal.

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nSincronización detenida por el usuario."
        )
