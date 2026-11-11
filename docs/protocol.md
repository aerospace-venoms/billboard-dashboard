# Device protocol, version 1

All external requests use verified HTTPS. Devices send `Authorization: Bearer
<device-token>`; browsers use a separately generated HTTP Basic login. Tokens must
not appear in URLs. Redirects are not part of the protocol.

`GET /v1/manifest.json` returns JSON containing version, width (960), height (672),
refresh_seconds, default_page, status and a pages array. Each page contains id,
title, sha256, bytes, format, image_url and frame_url. The current format is
`black-red-msb-1`, exactly 161280 bytes. Frame URLs have the form
`/v1/frames/<sha256>.bin`; PNG preview URLs use `.png`.

Frames contain an 80640-byte black plane followed by an equally sized red plane.
Pixels are row-major, MSB first within each byte, one bit per pixel per plane.
One means pigment present. Both bits zero means white; both bits one is invalid.
SHA-256 covers the complete two-plane binary payload. Content-addressed frames
avoid mixing a manifest with a newer render; the server retains four versions.
If a referenced frame expires, fetch a new manifest at the next update.

The optional `X-Badge-Supply-Mv` header on manifest GET accepts four ASCII digits
between 2500 and 6000, or `unknown`. Only device credentials may submit telemetry.
It measures regulator input, not necessarily battery voltage. Telemetry can cause
a new render and is limited to once per device per minute, at most once globally
every two seconds. A busy renderer also rejects updates immediately. On HTTP 429,
the badge may retry once without telemetry; it must not loop indefinitely.

`/`, `/dashboard.png`, `/v1/pages/home.png`, `/v1/pages/home.bin`, `/status.json` and
`/healthz` are also authenticated externally. The Docker health check on actual
loopback may read `/healthz` without credentials. Status 503 means current weather
is not ready or has become stale. The server supports ETag/If-None-Match for reads.
