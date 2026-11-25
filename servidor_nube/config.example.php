<?php

// Copiá este archivo como config.php y completá los datos de tu hosting.
// config.php está excluido de Git para que las credenciales no queden públicas.

define('DB_HOST', '127.0.0.1');
define('DB_NAME', 'NOMBRE_BASE_DATOS');
define('DB_USER', 'USUARIO_BASE_DATOS');
define('DB_PASS', 'CONTRASENA_BASE_DATOS');

// Debe coincidir con token_api de pc_local/config_sincronizador.json.
define('API_TOKEN', 'REEMPLAZAR_CON_UN_TOKEN_ALEATORIO_LARGO');

// Hash generado con password_hash(), no la contraseña en texto plano.
define('DASHBOARD_PASSWORD_HASH', 'PEGAR_AQUI_EL_HASH_DE_LA_CLAVE_DEL_DASHBOARD');
