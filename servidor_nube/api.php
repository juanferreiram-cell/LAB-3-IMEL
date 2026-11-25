<?php

// API del Nivel 3: recibe mediciones, guarda resúmenes y administra comandos.

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
ini_set('display_errors', '0');

require_once __DIR__ . '/config.php';

date_default_timezone_set('UTC');

// Devuelve una respuesta JSON y finaliza la solicitud.
function responder($codigo, $datos) {
    http_response_code($codigo);
    echo json_encode($datos, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

// Responde con un error en el formato común de la API.
function error_api($codigo, $mensaje) {
    responder($codigo, [
        'ok' => false,
        'error' => $mensaje
    ]);
}

// Abre una conexión PDO reutilizable con la base MySQL.
function conexion() {
    static $pdo = null;

    if ($pdo === null) {
        try {
            $pdo = new PDO(
                'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=utf8mb4',
                DB_USER,
                DB_PASS,
                [
                    PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                    PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                    PDO::ATTR_EMULATE_PREPARES => false
                ]
            );
        } catch (PDOException $e) {
            error_log('API MySQL: ' . $e->getMessage());
            error_api(500, 'No se pudo conectar con la base de datos');
        }
    }

    return $pdo;
}

// Busca el encabezado Authorization enviado por el sincronizador.
function obtener_autorizacion() {
    if (!empty($_SERVER['HTTP_AUTHORIZATION'])) {
        return trim($_SERVER['HTTP_AUTHORIZATION']);
    }

    if (!empty($_SERVER['REDIRECT_HTTP_AUTHORIZATION'])) {
        return trim($_SERVER['REDIRECT_HTTP_AUTHORIZATION']);
    }

    if (function_exists('getallheaders')) {
        foreach (getallheaders() as $nombre => $valor) {
            if (strtolower($nombre) === 'authorization') {
                return trim($valor);
            }
        }
    }

    return '';
}

// Comprueba el token Bearer antes de permitir operaciones de escritura o lectura.
function autenticar() {
    $recibido = obtener_autorizacion();
    $esperado = 'Bearer ' . API_TOKEN;

    if (
        API_TOKEN === 'REEMPLAZAR_CON_UN_TOKEN_ALEATORIO_LARGO' ||
        !hash_equals($esperado, $recibido)
    ) {
        error_api(401, 'Autenticacion incorrecta');
    }
}

// Lee y valida el objeto JSON enviado en el cuerpo de la solicitud.
function recibir_json() {
    $contenido = file_get_contents('php://input');
    $datos = json_decode($contenido, true);

    if (!is_array($datos) || json_last_error() !== JSON_ERROR_NONE) {
        error_api(400, 'El cuerpo debe contener un JSON valido');
    }

    return $datos;
}

// Valida una fecha y la normaliza a la zona horaria UTC.
function fecha_utc($valor) {
    if (!is_string($valor) || trim($valor) === '') {
        error_api(400, 'Fecha invalida');
    }

    try {
        $fecha = new DateTimeImmutable($valor);
        return $fecha->setTimezone(new DateTimeZone('UTC'))->format('Y-m-d H:i:s');
    } catch (Exception $e) {
        error_api(400, 'Formato de fecha incorrecto');
    }
}

// Acepta un número finito o un valor nulo para una medición.
function numero_o_nulo($valor, $nombre) {
    if ($valor === null) {
        return null;
    }

    if (!is_int($valor) && !is_float($valor)) {
        error_api(400, 'Valor numerico invalido: ' . $nombre);
    }

    if (!is_finite((float)$valor)) {
        error_api(400, 'Valor no finito: ' . $nombre);
    }

    return $valor;
}

// Comprueba que un dato sea entero y esté dentro del rango permitido.
function entero_rango($valor, $nombre, $minimo, $maximo) {
    if (!is_int($valor) || $valor < $minimo || $valor > $maximo) {
        error_api(400, 'Valor entero invalido: ' . $nombre);
    }

    return $valor;
}

// Valida los cuatro intervalos que informa el Arduino Nano.
function validar_intervalos($valor) {
    if (!is_array($valor) || count($valor) !== 4) {
        error_api(400, 'Se esperan cuatro intervalos');
    }

    $intervalos = array_values($valor);

    foreach ($intervalos as $i => $segundos) {
        entero_rango($segundos, 'intervalo', 1, 60);

        if ($i === 3 && $segundos < 3) {
            error_api(400, 'El cuarto intervalo debe ser de al menos 3 segundos');
        }
    }

    return json_encode($intervalos);
}

$ruta = trim($_GET['route'] ?? '', '/');
$metodo = $_SERVER['REQUEST_METHOD'] ?? 'GET';

if ($ruta === 'health' && $metodo === 'GET') {
    conexion();

    responder(200, [
        'ok' => true,
        'api' => 'activa',
        'database' => 'conectada'
    ]);
}

autenticar();
$db = conexion();

if ($ruta === 'lecturas/lote' && $metodo === 'POST') {
    $datos = recibir_json();
    $lecturas = $datos['lecturas'] ?? null;

    if (!is_array($lecturas) || count($lecturas) < 1 || count($lecturas) > 300) {
        error_api(400, 'El lote debe contener entre 1 y 300 lecturas');
    }

    $sql = '
        INSERT INTO mediciones (
            origen_id,
            fecha_hora,
            humedad_suelo_adc,
            luminosidad_adc,
            temperatura_suelo,
            temperatura_aire,
            humedad_aire,
            intervalos_s
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON DUPLICATE KEY UPDATE
            fecha_hora = VALUES(fecha_hora),
            humedad_suelo_adc = VALUES(humedad_suelo_adc),
            luminosidad_adc = VALUES(luminosidad_adc),
            temperatura_suelo = VALUES(temperatura_suelo),
            temperatura_aire = VALUES(temperatura_aire),
            humedad_aire = VALUES(humedad_aire),
            intervalos_s = VALUES(intervalos_s)
    ';

    $stmt = $db->prepare($sql);
    $procesadas = 0;
    $ultima_id = 0;

    try {
        $db->beginTransaction();

        foreach ($lecturas as $lectura) {
            if (!is_array($lectura)) {
                error_api(400, 'Una lectura del lote no es valida');
            }

            $origen_id = entero_rango(
                $lectura['origen_id'] ?? null,
                'origen_id',
                1,
                PHP_INT_MAX
            );

            $fecha = fecha_utc($lectura['fecha_hora'] ?? null);
            $intervalos = validar_intervalos($lectura['intervalos_s'] ?? null);

            $stmt->execute([
                $origen_id,
                $fecha,
                numero_o_nulo($lectura['humedad_suelo_adc'] ?? null, 'humedad_suelo_adc'),
                numero_o_nulo($lectura['luminosidad_adc'] ?? null, 'luminosidad_adc'),
                numero_o_nulo($lectura['temperatura_suelo_c'] ?? null, 'temperatura_suelo_c'),
                numero_o_nulo($lectura['temperatura_aire_c'] ?? null, 'temperatura_aire_c'),
                numero_o_nulo($lectura['humedad_aire_pct'] ?? null, 'humedad_aire_pct'),
                $intervalos
            ]);

            $procesadas++;
            $ultima_id = max($ultima_id, $origen_id);
        }

        $db->commit();
    } catch (Throwable $e) {
        if ($db->inTransaction()) {
            $db->rollBack();
        }

        error_log('API lecturas/lote: ' . $e->getMessage());
        error_api(500, 'No se pudo guardar el lote de mediciones');
    }

    responder(200, [
        'ok' => true,
        'procesadas' => $procesadas,
        'ultima_id' => $ultima_id
    ]);
}

if ($ruta === 'resumenes' && $metodo === 'POST') {
    $datos = recibir_json();

    $inicio = fecha_utc($datos['inicio_utc'] ?? null);
    $fin = fecha_utc($datos['fin_utc'] ?? null);

    if (strtotime($fin) - strtotime($inicio) !== 600) {
        error_api(400, 'El resumen debe cubrir exactamente 10 minutos');
    }

    $cantidad = entero_rango(
        $datos['cantidad_registros'] ?? null,
        'cantidad_registros',
        1,
        1000000
    );

    $campos = [
        'humedad_suelo_adc',
        'luminosidad_adc',
        'temperatura_suelo_c',
        'temperatura_aire_c',
        'humedad_aire_pct'
    ];

    $valores = [];
    foreach ($campos as $campo) {
        $valores[] = numero_o_nulo($datos[$campo] ?? null, $campo);
    }

    $sql = '
        INSERT INTO resumen_10min (
            inicio_utc,
            fin_utc,
            cantidad_registros,
            humedad_suelo_adc,
            luminosidad_adc,
            temperatura_suelo_c,
            temperatura_aire_c,
            humedad_aire_pct,
            recibido_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, UTC_TIMESTAMP())
        ON DUPLICATE KEY UPDATE
            fin_utc = VALUES(fin_utc),
            cantidad_registros = VALUES(cantidad_registros),
            humedad_suelo_adc = VALUES(humedad_suelo_adc),
            luminosidad_adc = VALUES(luminosidad_adc),
            temperatura_suelo_c = VALUES(temperatura_suelo_c),
            temperatura_aire_c = VALUES(temperatura_aire_c),
            humedad_aire_pct = VALUES(humedad_aire_pct),
            recibido_utc = UTC_TIMESTAMP()
    ';

    $stmt = $db->prepare($sql);
    $stmt->execute([
        $inicio,
        $fin,
        $cantidad,
        $valores[0],
        $valores[1],
        $valores[2],
        $valores[3],
        $valores[4]
    ]);

    responder(200, [
        'ok' => true,
        'inicio_utc' => $inicio
    ]);
}

if ($ruta === 'comandos' && $metodo === 'POST') {
    $datos = recibir_json();

    $sensor = entero_rango($datos['sensor'] ?? null, 'sensor', 1, 4);
    $segundos = entero_rango($datos['segundos'] ?? null, 'segundos', 1, 60);

    if ($sensor === 4 && $segundos < 3) {
        error_api(400, 'El cuarto sensor requiere al menos 3 segundos');
    }

    $id = bin2hex(random_bytes(16));

    $stmt = $db->prepare('
        INSERT INTO comandos (id, sensor, segundos, estado, creado_utc)
        VALUES (?, ?, ?, "pendiente", UTC_TIMESTAMP())
    ');
    $stmt->execute([$id, $sensor, $segundos]);

    responder(201, [
        'ok' => true,
        'id' => $id,
        'estado' => 'pendiente'
    ]);
}

if ($ruta === 'comandos/pendientes' && $metodo === 'GET') {
    $stmt = $db->query('
        SELECT id, sensor, segundos, estado, creado_utc
        FROM comandos
        WHERE estado = "pendiente"
        ORDER BY creado_utc ASC
        LIMIT 1
    ');

    $comando = $stmt->fetch();

    responder(200, [
        'ok' => true,
        'comando' => $comando ?: null
    ]);
}

if (
    preg_match('#^comandos/([a-f0-9]{32})/resultado$#', $ruta, $coincidencia) &&
    $metodo === 'POST'
) {
    $datos = recibir_json();
    $estado = $datos['estado'] ?? '';

    $estados_validos = ['confirmado', 'sin_confirmacion', 'error'];

    if (!in_array($estado, $estados_validos, true)) {
        error_api(400, 'Estado de comando invalido');
    }

    $detalle = substr((string)($datos['detalle'] ?? ''), 0, 400);

    $stmt = $db->prepare('
        UPDATE comandos
        SET estado = ?,
            confirmado_utc = IF(? = "confirmado", UTC_TIMESTAMP(), NULL),
            detalle = ?
        WHERE id = ?
    ');

    $stmt->execute([
        $estado,
        $estado,
        $detalle,
        $coincidencia[1]
    ]);

    if ($stmt->rowCount() === 0) {
        $buscar = $db->prepare('SELECT id FROM comandos WHERE id = ?');
        $buscar->execute([$coincidencia[1]]);

        if (!$buscar->fetch()) {
            error_api(404, 'Comando inexistente');
        }
    }

    responder(200, [
        'ok' => true,
        'estado' => $estado
    ]);
}

error_api(404, 'Ruta no encontrada');
