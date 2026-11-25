#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <dht_nonblocking.h>
#include <util/atomic.h>

// Pines utilizados por los sensores conectados al Nano
#define SUELO A0
#define LUZ A1
#define DS_PIN 2
#define DHT_PIN 3

#define I2C_ADDR 0x08
#define SIN_DATO -32768

OneWire oneWire(DS_PIN);
DallasTemperature ds(&oneWire);
DHT_nonblocking dht(DHT_PIN, DHT_TYPE_11);

// Intervalos de muestreo en segundos, uno por cada sensor
// Orden: suelo, LDR, DS18B20, DHT11
byte intervalo[4] = {2, 2, 5, 3};
unsigned long ultimo[4] = {0, 0, 0, 0};

// Últimas mediciones disponibles para enviar por I2C
uint16_t humedadSuelo = 0;
uint16_t luminosidad = 0;
int16_t tempSuelo = SIN_DATO;
int16_t tempAire = SIN_DATO;
int16_t humedadAire = SIN_DATO;

// Bits que indican qué mediciones ya tienen un valor válido
byte estado = 0;

// Estados y tiempos de las conversiones que no deben bloquear el loop
bool convirtiendoDS = false;
bool leyendoDHT = false;
unsigned long inicioDS = 0;
unsigned long inicioDHT = 0;

// Paquete que consulta el ESP8266 mediante I2C
byte paquete[18] = {0};
byte secuencia = 0;

// Variables compartidas con la rutina de recepción I2C
volatile bool comandoPendiente = false;
volatile byte sensorComando = 0;
volatile byte segundosComando = 0;

// Guarda un entero de 16 bits en dos posiciones del paquete.
void guardar16(byte *p, byte pos, int16_t valor) {
  uint16_t n = (uint16_t)valor;
  p[pos] = n & 0xFF;
  p[pos + 1] = n >> 8;
}

// Arma el paquete de 18 bytes y calcula su checksum.
void actualizarPaquete() {
  byte p[18] = {0};

  p[0] = 0xA5;
  p[1] = secuencia++;

  guardar16(p, 2, humedadSuelo);
  guardar16(p, 4, luminosidad);
  guardar16(p, 6, tempSuelo);
  guardar16(p, 8, tempAire);
  guardar16(p, 10, humedadAire);

  p[12] = estado;

  for (byte i = 0; i < 4; i++) {
    p[13 + i] = intervalo[i];
  }

  byte checksum = 0;
  for (byte i = 0; i < 17; i++) {
    checksum ^= p[i];
  }
  p[17] = checksum;

  // Evitar que I2C lea un paquete a medio actualizar
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    memcpy(paquete, p, 18);
  }
}

// Recibe desde el ESP8266 el sensor que debe cambiar de intervalo.
void recibirI2C(int cantidad) {
  if (cantidad == 3) {
    byte orden = Wire.read();
    byte id = Wire.read();
    byte segundos = Wire.read();

    if (orden == 0xC1 && id < 4 &&
        segundos <= 60 &&
        segundos >= (id == 3 ? 3 : 1)) {

      sensorComando = id;
      segundosComando = segundos;
      comandoPendiente = true;
    }
  }

  while (Wire.available()) Wire.read();
}

// Entrega al ESP8266 la última copia del paquete de mediciones.
void responderI2C() {
  Wire.write(paquete, 18);
}

// Inicializa sensores, bus I2C y primeros tiempos de muestreo.
void setup() {
  Serial.begin(115200);

  ds.begin();
  ds.setResolution(12);
  ds.setWaitForConversion(false);

  Wire.begin(I2C_ADDR);

  // Desactivar pull-ups internas de 5 V
  digitalWrite(A4, LOW);
  digitalWrite(A5, LOW);

  Wire.onReceive(recibirI2C);
  Wire.onRequest(responderI2C);

  // Iniciar muestreos sin esperar
  for (byte i = 0; i < 4; i++) {
    ultimo[i] = millis() - intervalo[i] * 1000UL;
  }

  actualizarPaquete();

  Serial.println("NANO - Sistema agricola iniciado");
}

// Atiende las configuraciones y actualiza cada sensor cuando corresponde.
void loop() {
  unsigned long ahora = millis();
  bool actualizar = false;

  // 1. Aplicar configuraciones I2C
  bool hayComando;
  byte id = 0, segundos = 0;

  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    hayComando = comandoPendiente;

    if (hayComando) {
      id = sensorComando;
      segundos = segundosComando;
      comandoPendiente = false;
    }
  }

  if (hayComando) {
    intervalo[id] = segundos;
    ultimo[id] = ahora;
    actualizar = true;

    Serial.print("Intervalo sensor ");
    Serial.print(id + 1);
    Serial.print(": ");
    Serial.print(segundos);
    Serial.println(" s");
  }

  // 2. Humedad del suelo
  if (ahora - ultimo[0] >= intervalo[0] * 1000UL) {
    ultimo[0] = ahora;

    uint16_t lectura = analogRead(SUELO);

    if (estado & 1) {
      humedadSuelo = (3UL * humedadSuelo + lectura) / 4;
    } else {
      humedadSuelo = lectura;
    }

    estado |= 1;
    actualizar = true;
  }

  // 3. Luminosidad
  if (ahora - ultimo[1] >= intervalo[1] * 1000UL) {
    ultimo[1] = ahora;

    uint16_t lectura = analogRead(LUZ);

    if (estado & 2) {
      luminosidad = (3UL * luminosidad + lectura) / 4;
    } else {
      luminosidad = lectura;
    }

    estado |= 2;
    actualizar = true;
  }

  // 4. Temperatura del suelo
  if (!convirtiendoDS &&
      ahora - ultimo[2] >= intervalo[2] * 1000UL) {

    ultimo[2] = ahora;
    inicioDS = ahora;

    ds.requestTemperatures();
    convirtiendoDS = true;
  }

  if (convirtiendoDS &&
      ahora - inicioDS >= 750) {

    float t = ds.getTempCByIndex(0);

    if (t != DEVICE_DISCONNECTED_C &&
        t >= -55 && t <= 125) {

      tempSuelo = (int16_t)(t * 10);
      estado |= 4;
    } else {
      tempSuelo = SIN_DATO;
      estado &= ~4;
    }

    convirtiendoDS = false;
    actualizar = true;
  }

  // 5. Temperatura y humedad ambiental
  if (!leyendoDHT &&
      ahora - ultimo[3] >= intervalo[3] * 1000UL) {

    ultimo[3] = ahora;
    inicioDHT = ahora;
    leyendoDHT = true;
  }

  if (leyendoDHT) {
    float t, h;

    if (dht.measure(&t, &h)) {
      tempAire = (int16_t)(t * 10);
      humedadAire = (int16_t)(h * 10);

      estado |= 8;
      leyendoDHT = false;
      actualizar = true;
    }

    if (ahora - inicioDHT > 3500) {
      leyendoDHT = false;
    }
  }

  if (actualizar) {
    actualizarPaquete();
  }
}
