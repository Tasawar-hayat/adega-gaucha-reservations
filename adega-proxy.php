<?php
// Adega Gaucha — PHP proxy to Python reservation API
// Upload to: WordPress root (same folder as wp-config.php)
// Access at: https://adegagaucha.com/adega-proxy.php

define('PYTHON_URL', 'http://127.0.0.1:8080/webhook/adega-book');
define('EXPECTED_KEY', 'adega-widget-2026');

header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, GET, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, x-api-key');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit;
}

// GET ?test=1 — quick health check (visit in browser to diagnose)
if ($_SERVER['REQUEST_METHOD'] === 'GET' && isset($_GET['test'])) {
    $result = proxy_request('{"action":"restaurant-info"}', EXPECTED_KEY);
    echo json_encode([
        'proxy'        => 'ok',
        'python_url'   => PYTHON_URL,
        'curl_enabled' => function_exists('curl_init'),
        'python_response' => $result,
    ], JSON_PRETTY_PRINT);
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    http_response_code(405);
    echo json_encode(['success' => false, 'error' => 'Method not allowed']);
    exit;
}

$api_key = isset($_SERVER['HTTP_X_API_KEY']) ? $_SERVER['HTTP_X_API_KEY'] : '';
$body    = file_get_contents('php://input');
if (!$body) {
    echo json_encode(['success' => false, 'error' => 'Empty request body']);
    exit;
}

$result = proxy_request($body, $api_key);
http_response_code($result['http_code']);
echo $result['body'];

// ── helpers ──────────────────────────────────────────────────────────────────

function proxy_request($body, $api_key) {
    $headers = [
        'Content-Type: application/json',
        'x-api-key: ' . $api_key,
        'Content-Length: ' . strlen($body),
    ];

    // Try curl first
    if (function_exists('curl_init')) {
        $ch = curl_init(PYTHON_URL);
        curl_setopt_array($ch, [
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => $body,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 30,
            CURLOPT_CONNECTTIMEOUT => 5,
            CURLOPT_HTTPHEADER     => $headers,
        ]);
        $response  = curl_exec($ch);
        $http_code = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $error     = curl_error($ch);
        curl_close($ch);

        if ($error) {
            return [
                'http_code' => 502,
                'body'      => json_encode(['success' => false, 'error' => 'Python server unreachable: ' . $error . '. Make sure uvicorn is running on port 8080.']),
            ];
        }
        return ['http_code' => $http_code ?: 200, 'body' => $response];
    }

    // Fallback: file_get_contents (if curl disabled)
    $ctx = stream_context_create([
        'http' => [
            'method'  => 'POST',
            'header'  => implode("\r\n", $headers),
            'content' => $body,
            'timeout' => 30,
            'ignore_errors' => true,
        ],
    ]);
    $response = @file_get_contents(PYTHON_URL, false, $ctx);
    if ($response === false) {
        return [
            'http_code' => 502,
            'body'      => json_encode(['success' => false, 'error' => 'Python server unreachable (curl disabled, file_get_contents also failed). Check uvicorn on port 8080.']),
        ];
    }
    return ['http_code' => 200, 'body' => $response];
}
