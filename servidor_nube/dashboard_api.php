<?php

// API privada del dashboard web. Usa la misma base MySQL que api.php.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
ini_set('display_errors', '0');

require_once __DIR__ . '/config.php';
date_default_timezone_set('UTC');

$https = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
session_name('estacion_agricola');
session_set_cookie_params([
    'lifetime' => 0,
    'path' => '/',
    'secure' => $https,
    'httponly' => true,
    'samesite' => 'Strict'
]);
session_start();

// Devuelve JSON con el código HTTP indicado.
function responder_dashboard($codigo, $datos) {
    http_response_code($codigo);
    echo json_encode($datos, JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

// Devuelve un error con el mismo formato para todas las acciones.
function error_dashboard($codigo, $mensaje) {
    responder_dashboard($codigo, ['ok' => false, 'error' => $mensaje]);
}

// Abre una conexión PDO a la base que también utiliza el sincronizador.
function conexion_dashboard() {
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
        } catch (PDOException $error) {
            error_log('Dashboard MySQL: ' . $error->getMessage());
            error_dashboard(500, 'No se pudo conectar con la base de datos');
        }
    }
    return $pdo;
}

// Comprueba que la sesión haya iniciado desde el formulario del dashboard.
function exigir_sesion_dashboard() {
    if (empty($_SESSION['dashboard_autenticado'])) {
        error_dashboard(401, 'La sesión no está iniciada');
    }
}

// Lee un objeto JSON enviado por el navegador.
function recibir_json_dashboard() {
    $datos = json_decode(file_get_contents('php://input'), true);
    if (!is_array($datos) || json_last_error() !== JSON_ERROR_NONE) {
        error_dashboard(400, 'El cuerpo debe contener un JSON válido');
    }
    return $datos;
}

// Comprueba que la fecha tenga el formato esperado por los filtros del histórico.
function validar_fecha_dashboard($valor, $campo) {
    if (!is_string($valor) || !preg_match('/^\d{4}-\d{2}-\d{2}$/', $valor)) {
        error_dashboard(400, 'Fecha no válida: ' . $campo);
    }
    $fecha = DateTimeImmutable::createFromFormat('!Y-m-d', $valor);
    if (!$fecha || $fecha->format('Y-m-d') !== $valor) {
        error_dashboard(400, 'Fecha no válida: ' . $campo);
    }
    return $valor;
}

// Devuelve el rango de fechas recibido desde el selector del dashboard.
function obtener_rango_dashboard() {
    $desde = validar_fecha_dashboard($_GET['desde'] ?? '', 'desde');
    $hasta = validar_fecha_dashboard($_GET['hasta'] ?? '', 'hasta');
    if ($desde > $hasta) {
        error_dashboard(400, 'La fecha inicial no puede ser posterior a la final');
    }
    return [$desde, $hasta];
}

$accion = $_GET['action'] ?? '';
$metodo = $_SERVER['REQUEST_METHOD'] ?? 'GET';

// Informa al navegador si todavía existe una sesión válida.
if ($accion === 'session' && $metodo === 'GET') {
    responder_dashboard(200, [
        'ok' => true,
        'autenticado' => !empty($_SESSION['dashboard_autenticado'])
    ]);
}

// Valida la contraseña del dashboard y crea una sesión nueva.
if ($accion === 'login' && $metodo === 'POST') {
    if (!defined('DASHBOARD_PASSWORD_HASH') ||
        DASHBOARD_PASSWORD_HASH === 'PEGAR_AQUI_EL_HASH_DE_LA_CLAVE_DEL_DASHBOARD') {
        error_dashboard(503, 'Falta configurar la contraseña del dashboard en config.php');
    }

    $datos = recibir_json_dashboard();
    $clave = $datos['password'] ?? '';
    if (!is_string($clave) || !password_verify($clave, DASHBOARD_PASSWORD_HASH)) {
        error_dashboard(401, 'Contraseña incorrecta');
    }

    session_regenerate_id(true);
    $_SESSION['dashboard_autenticado'] = true;
    $_SESSION['inicio_sesion'] = time();
    responder_dashboard(200, ['ok' => true, 'autenticado' => true]);
}

// Cierra la sesión y elimina sus datos del servidor.
if ($accion === 'logout' && $metodo === 'POST') {
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $parametros = session_get_cookie_params();
        setcookie(session_name(), '', [
            'expires' => time() - 42000,
            'path' => $parametros['path'],
            'domain' => $parametros['domain'],
            'secure' => $parametros['secure'],
            'httponly' => $parametros['httponly'],
            'samesite' => 'Strict'
        ]);
    }
    session_destroy();
    responder_dashboard(200, ['ok' => true]);
}

exigir_sesion_dashboard();
$db = conexion_dashboard();

// Devuelve las mediciones individuales del período seleccionado.
if ($accion === 'measurements' && $metodo === 'GET') {
    [$desde, $hasta] = obtener_rango_dashboard();
    $limite = filter_var($_GET['limite'] ?? 1500, FILTER_VALIDATE_INT);
    if ($limite === false) $limite = 1500;
    $limite = max(1, min(1500, $limite));

    $stmt = $db->prepare(''
        . 'SELECT origen_id, DATE_FORMAT(fecha_hora, "%Y-%m-%d %H:%i:%s") AS fecha_hora, '
        . 'humedad_suelo_adc, luminosidad_adc, temperatura_suelo, temperatura_aire, '
        . 'humedad_aire, intervalos_s '
        . 'FROM mediciones '
        . 'WHERE fecha_hora >= ? AND fecha_hora < DATE_ADD(?, INTERVAL 1 DAY) '
        . 'ORDER BY fecha_hora DESC LIMIT ' . (int)$limite
    );
    $stmt->execute([$desde, $hasta]);
    responder_dashboard(200, ['ok' => true, 'datos' => $stmt->fetchAll()]);
}

// Devuelve los promedios que el sincronizador consolida cada diez minutos.
if ($accion === 'summaries' && $metodo === 'GET') {
    [$desde, $hasta] = obtener_rango_dashboard();
    $stmt = $db->prepare(''
        . 'SELECT DATE_FORMAT(inicio_utc, "%Y-%m-%d %H:%i:%s") AS inicio_utc, '
        . 'DATE_FORMAT(fin_utc, "%Y-%m-%d %H:%i:%s") AS fin_utc, '
        . 'cantidad_registros, humedad_suelo_adc, luminosidad_adc, '
        . 'temperatura_suelo_c, temperatura_aire_c, humedad_aire_pct '
        . 'FROM resumen_10min '
        . 'WHERE inicio_utc >= ? AND inicio_utc < DATE_ADD(?, INTERVAL 1 DAY) '
        . 'ORDER BY inicio_utc DESC'
    );
    $stmt->execute([$desde, $hasta]);
    responder_dashboard(200, ['ok' => true, 'datos' => $stmt->fetchAll()]);
}

// Muestra los últimos comandos y el estado que confirmó el ESP8266/Nano.
if ($accion === 'commands' && $metodo === 'GET') {
    $stmt = $db->query(''
        . 'SELECT id, sensor, segundos, estado, '
        . 'DATE_FORMAT(creado_utc, "%Y-%m-%d %H:%i:%s") AS creado_utc, '
        . 'DATE_FORMAT(confirmado_utc, "%Y-%m-%d %H:%i:%s") AS confirmado_utc, detalle '
        . 'FROM comandos ORDER BY creado_utc DESC LIMIT 100'
    );
    responder_dashboard(200, ['ok' => true, 'datos' => $stmt->fetchAll()]);
}

// Crea un comando pendiente para que el sincronizador lo entregue por MQTT.
if ($accion === 'command' && $metodo === 'POST') {
    $datos = recibir_json_dashboard();
    $sensor = $datos['sensor'] ?? null;
    $segundos = $datos['segundos'] ?? null;

    if (!is_int($sensor) || $sensor < 1 || $sensor > 4) {
        error_dashboard(400, 'El sensor debe estar entre 1 y 4');
    }
    if (!is_int($segundos) || $segundos < 1 || $segundos > 60) {
        error_dashboard(400, 'El intervalo debe estar entre 1 y 60 segundos');
    }
    if ($sensor === 4 && $segundos < 3) {
        error_dashboard(400, 'El HW-507 requiere un mínimo de 3 segundos');
    }

    $id = bin2hex(random_bytes(16));
    $stmt = $db->prepare(''
        . 'INSERT INTO comandos (id, sensor, segundos, estado, creado_utc) '
        . 'VALUES (?, ?, ?, "pendiente", UTC_TIMESTAMP())'
    );
    $stmt->execute([$id, $sensor, $segundos]);
    responder_dashboard(201, ['ok' => true, 'id' => $id, 'estado' => 'pendiente']);
}

error_dashboard(404, 'Acción no encontrada');
