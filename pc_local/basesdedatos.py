"""Receptor MQTT de la Estación Agrícola IoT.

Guarda las mediciones recibidas del ESP8266 en agro_local.db.
"""

import getpass
import json
import os
import sqlite3
from pathlib import Path

import paho.mqtt.client as mqtt

BASE = Path(__file__).resolve().parent
RUTA_DB = BASE / "agro_local.db"
RUTA_CONFIG = BASE / "config_local.json"
TOPICO_DATOS = "agro/mediciones"


def cargar_configuracion():
    """Lee los datos del broker y permite completarlos con variables de entorno."""
    datos = {}
    if RUTA_CONFIG.exists():
        datos = json.loads(RUTA_CONFIG.read_text(encoding="utf-8"))

    return {
        "mqtt_host": os.getenv("MQTT_HOST", datos.get("mqtt_host", "127.0.0.1")),
        "mqtt_puerto": int(os.getenv("MQTT_PORT", datos.get("mqtt_puerto", 1883))),
        "mqtt_usuario": os.getenv("MQTT_USER", datos.get("mqtt_usuario", "agro")),
        "mqtt_password": os.getenv("MQTT_PASSWORD", datos.get("mqtt_password", "")),
    }


def preparar_base():
    """Crea la tabla local si todavía no existe y devuelve la conexión."""
    db = sqlite3.connect(RUTA_DB)
    db.execute("""
        CREATE TABLE IF NOT EXISTS mediciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            humedad_suelo INTEGER,
            luminosidad INTEGER,
            temperatura_suelo REAL,
            temperatura_aire REAL,
            humedad_aire REAL,
            intervalos TEXT
        )
    """)
    db.commit()
    return db


def configurar_cliente(db, ajustes):
    """Prepara el cliente MQTT y registra las funciones que procesan los mensajes."""
    cliente = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="agro-db-local"
    )
    cliente.username_pw_set(ajustes["mqtt_usuario"], ajustes["mqtt_password"])

    # Se suscribe al tópico de mediciones cuando el broker acepta la conexión.
    def conectado(client, userdata, flags, reason_code, properties):
        """Confirma la conexión y comienza a escuchar las mediciones."""
        if reason_code == 0:
            print("MQTT conectado")
            client.subscribe(TOPICO_DATOS)
        else:
            print("Error MQTT:", reason_code)

    # Cada mensaje válido se guarda como un nuevo registro de SQLite.
    def recibir(client, userdata, mensaje):
        """Convierte el JSON recibido y lo guarda en la base local."""
        try:
            datos = json.loads(mensaje.payload.decode("utf-8"))
            db.execute("""
                INSERT INTO mediciones (
                    fecha, humedad_suelo, luminosidad,
                    temperatura_suelo, temperatura_aire,
                    humedad_aire, intervalos
                ) VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)
            """, (
                datos.get("humedad_suelo_adc"),
                datos.get("luminosidad_adc"),
                datos.get("temperatura_suelo_c"),
                datos.get("temperatura_aire_c"),
                datos.get("humedad_aire_pct"),
                json.dumps(datos.get("intervalos_s", []), ensure_ascii=False)
            ))
            db.commit()
            print("Medición guardada:", datos)
        except (ValueError, TypeError, UnicodeDecodeError, sqlite3.Error) as error:
            print("Error guardando medición:", error)

    cliente.on_connect = conectado
    cliente.on_message = recibir
    return cliente


def main():
    """Inicia el receptor y mantiene activa la conexión con Mosquitto."""
    ajustes = cargar_configuracion()
    if not ajustes["mqtt_password"]:
        ajustes["mqtt_password"] = getpass.getpass("Contraseña MQTT: ")

    db = preparar_base()
    cliente = configurar_cliente(db, ajustes)

    print("Conectando al broker MQTT...")
    print(f"Broker: {ajustes['mqtt_host']}:{ajustes['mqtt_puerto']}")

    try:
        cliente.connect(ajustes["mqtt_host"], ajustes["mqtt_puerto"], 60)
        cliente.loop_forever()
    except KeyboardInterrupt:
        print("Finalizando receptor MQTT...")
    finally:
        cliente.disconnect()
        db.close()


if __name__ == "__main__":
    main()
