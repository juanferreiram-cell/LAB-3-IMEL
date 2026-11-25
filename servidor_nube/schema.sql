-- Base de datos de la Estación Agrícola IoT.
-- Ejecutar una sola vez en la base MySQL del servidor.

CREATE TABLE IF NOT EXISTS mediciones (
    origen_id BIGINT UNSIGNED NOT NULL,
    fecha_hora DATETIME NOT NULL,
    humedad_suelo_adc DOUBLE NULL,
    luminosidad_adc DOUBLE NULL,
    temperatura_suelo DOUBLE NULL,
    temperatura_aire DOUBLE NULL,
    humedad_aire DOUBLE NULL,
    intervalos_s JSON NOT NULL,
    recibido_utc DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (origen_id),
    INDEX idx_mediciones_fecha (fecha_hora)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS resumen_10min (
    inicio_utc DATETIME NOT NULL,
    fin_utc DATETIME NOT NULL,
    cantidad_registros INT UNSIGNED NOT NULL,
    humedad_suelo_adc DOUBLE NULL,
    luminosidad_adc DOUBLE NULL,
    temperatura_suelo_c DOUBLE NULL,
    temperatura_aire_c DOUBLE NULL,
    humedad_aire_pct DOUBLE NULL,
    recibido_utc DATETIME NOT NULL,
    PRIMARY KEY (inicio_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS comandos (
    id CHAR(32) NOT NULL,
    sensor TINYINT UNSIGNED NOT NULL,
    segundos TINYINT UNSIGNED NOT NULL,
    estado ENUM('pendiente', 'confirmado', 'sin_confirmacion', 'error') NOT NULL DEFAULT 'pendiente',
    creado_utc DATETIME NOT NULL,
    confirmado_utc DATETIME NULL,
    detalle VARCHAR(400) NULL,
    PRIMARY KEY (id),
    INDEX idx_comandos_estado (estado, creado_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
