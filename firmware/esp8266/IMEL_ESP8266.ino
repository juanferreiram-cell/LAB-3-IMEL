#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include "secrets.h"

// Datos de conexión definidos en secrets.h, que no se sube a GitHub.
IPAddress broker(MQTT_BROKER_IP_1, MQTT_BROKER_IP_2,
                 MQTT_BROKER_IP_3, MQTT_BROKER_IP_4);
const int puertoMQTT = MQTT_PUERTO;

const char* topicoDatos = "agro/mediciones";
const char* topicoConfig = "agro/config/intervalos";
const char* topicoEstado = "agro/estado/intervalos";

// Dirección I2C del Nano y tamaño fijo del paquete de mediciones.
#define NANO_ADDR 0x08
#define TAM 18

WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

unsigned long ultimoI2C = 0;
unsigned long ultimoMQTT = 0;
unsigned long ultimoWiFi = 0;

// Comando de configuración pendiente
bool comandoPendiente = false;
bool esperandoConfirmacion = false;

byte sensorPendiente = 0;
byte segundosPendientes = 0;

// Lee un valor sin signo de 16 bits desde dos bytes.
uint16_t leerU16(byte* p, byte i) {
  return (uint16_t)p[i] |
         ((uint16_t)p[i + 1] << 8);
}

// Interpreta dos bytes como un entero con signo.
int16_t leerS16(byte* p, byte i) {
  return (int16_t)leerU16(p, i);
}

// Valida y guarda los comandos que llegan desde MQTT.
void recibirMQTT(char* topic, byte* datos,
                 unsigned int longitud) {

  if (strcmp(topic, topicoConfig) != 0) return;
  if (longitud == 0 || longitud >= 32) return;

  char texto[32];
  memcpy(texto, datos, longitud);
  texto[longitud] = '\0';

  int sensor, segundos;

  if (sscanf(texto, "%d %d",
             &sensor, &segundos) != 2) return;

  if (sensor < 1 || sensor > 4) return;
  if (segundos > 60) return;
  if (segundos < (sensor == 4 ? 3 : 1)) return;

  sensorPendiente = sensor - 1;
  segundosPendientes = segundos;
  comandoPendiente = true;

  Serial.println("Comando MQTT recibido");
}

// Envía al Nano el comando de configuración pendiente.
void enviarConfiguracion() {
  Wire.beginTransmission(NANO_ADDR);
  Wire.write(0xC1);
  Wire.write(sensorPendiente);
  Wire.write(segundosPendientes);

  byte error = Wire.endTransmission();

  if (error == 0) {
    esperandoConfirmacion = true;
    Serial.println("Configuracion enviada al Nano");
  } else {
    Serial.print("Error I2C configuracion: ");
    Serial.println(error);
  }

  comandoPendiente = false;
}

// Solicita el paquete I2C, valida el checksum y publica los datos.
void consultarNano() {
  byte p[TAM];

  byte n = Wire.requestFrom(
    (uint8_t)NANO_ADDR, (uint8_t)TAM
  );

  if (n != TAM) {
    while (Wire.available()) Wire.read();
    Serial.println("Error lectura I2C");
    return;
  }

  for (byte i = 0; i < TAM; i++) {
    p[i] = Wire.read();
  }

  byte checksum = 0;
  for (byte i = 0; i < 17; i++) {
    checksum ^= p[i];
  }

  if (p[0] != 0xA5 || checksum != p[17]) {
    Serial.println("Paquete I2C incorrecto");
    return;
  }

  byte estado = p[12];

  // Convierte el paquete recibido en un mensaje JSON.
  String json;
  json.reserve(300);

  json = "{";

  json += "\"humedad_suelo_adc\":";
  json += (estado & 1) ?
          String(leerU16(p, 2)) : "null";

  json += ",\"luminosidad_adc\":";
  json += (estado & 2) ?
          String(leerU16(p, 4)) : "null";

  json += ",\"temperatura_suelo_c\":";
  json += (estado & 4) ?
          String(leerS16(p, 6) / 10.0, 1) : "null";

  json += ",\"temperatura_aire_c\":";
  json += (estado & 8) ?
          String(leerS16(p, 8) / 10.0, 1) : "null";

  json += ",\"humedad_aire_pct\":";
  json += (estado & 8) ?
          String(leerS16(p, 10) / 10.0, 1) : "null";

  json += ",\"intervalos_s\":[";

  for (byte i = 0; i < 4; i++) {
    if (i > 0) json += ",";
    json += String(p[13 + i]);
  }

  json += "]}";

  Serial.println(json);

  // Publica las mediciones cuando MQTT está conectado.
  if (mqtt.connected()) {
    if (mqtt.publish(topicoDatos, json.c_str())) {
      Serial.println("MQTT publicado correctamente");
    } else {
      Serial.println("Error publicando MQTT");
    }
  }

  // Confirma el cambio cuando el Nano informa el nuevo intervalo.
  if (esperandoConfirmacion &&
      p[13 + sensorPendiente] ==
      segundosPendientes) {

    esperandoConfirmacion = false;

    String respuesta = "{\"sensor\":";
    respuesta += String(sensorPendiente + 1);
    respuesta += ",\"segundos\":";
    respuesta += String(segundosPendientes);
    respuesta += ",\"aplicado\":true}";

    if (mqtt.connected()) {
      mqtt.publish(topicoEstado,
                   respuesta.c_str());
    }

    Serial.println("Configuracion confirmada");
  }
}

// Configura el puerto serie, I2C, Wi-Fi y MQTT.
void setup() {
  Serial.begin(115200);

  Wire.begin(D2, D1);
  Wire.setClock(100000);
  Wire.setClockStretchLimit(15000);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  mqtt.setServer(broker, puertoMQTT);
  mqtt.setCallback(recibirMQTT);
  mqtt.setBufferSize(512);
  mqtt.setSocketTimeout(3);

  Serial.println("Estacion agricola IoT iniciada");
}

// Mantiene las comunicaciones y consulta al Nano sin bloquear el programa.
void loop() {
  unsigned long ahora = millis();

  // Controla la reconexión a la red Wi-Fi.
  if (WiFi.status() != WL_CONNECTED) {
    if (ahora - ultimoWiFi >= 15000) {
      ultimoWiFi = ahora;

      Serial.print("Estado WiFi: ");
      Serial.println(WiFi.status());

      Serial.print("Intentando conectar a: ");
      Serial.println(WIFI_SSID);

      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
  } else {
    static bool conectado = false;

    if (!conectado) {
      conectado = true;

      Serial.println("WIFI CONECTADO");
      Serial.print("IP del ESP8266: ");
      Serial.println(WiFi.localIP());
      Serial.print("Intensidad WiFi: ");
      Serial.println(WiFi.RSSI());
    }
  }


  // Vuelve a conectar con el broker cuando se pierde la sesión.
  if (WiFi.status() == WL_CONNECTED &&
      !mqtt.connected() &&
      ahora - ultimoMQTT >= 5000) {

    ultimoMQTT = ahora;

    String clienteID = "agro-esp8266-";
    clienteID += String(ESP.getChipId(), HEX);

    if (mqtt.connect(clienteID.c_str(),
                     MQTT_USUARIO, MQTT_PASSWORD)) {

      Serial.println("MQTT conectado");
      mqtt.subscribe(topicoConfig);

    } else {
      Serial.print("Error MQTT: ");
      Serial.println(mqtt.state());
    }
  }

  if (mqtt.connected()) mqtt.loop();

  if (comandoPendiente) {
    enviarConfiguracion();
  }

  // Leer sensores cada 3 segundos
  if (ahora - ultimoI2C >= 3000) {
    ultimoI2C = ahora;
    consultarNano();
  }
}
