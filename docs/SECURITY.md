# imgagent security model

## Principles

Search results, page content, redirects and remote metadata are **untrusted
input**. imgagent is built to run unattended on cloud VMs and CI, so the
security defaults are strict and the escape hatches are loud.

## Outbound request policy

- **Schemes**: http/HTTPS only. `file:`, `ftp:`, `data:` URLs are rejected
  before any request is made.
- **Ports**: 80/443 only in public mode (development mode with
  `IMGAGENT_ALLOW_PRIVATE=1` may use arbitrary local ports).
- **Credentials in URLs** are rejected (they leak into logs).
- **SSRF protection**: every URL — and every redirect hop — is resolved and
  checked. Rejected targets: loopback, RFC1918 private, CGNAT (100.64/10),
  link-local, multicast, reserved, unspecified, and `*.localhost/.local/
  .internal/.home.arpa` hostnames.
- **Cloud metadata endpoints are hard-blocked** (169.254.169.254,
  fd00:ec2::254, metadata.google.internal). `IMGAGENT_ALLOW_PRIVATE=1` does
  **not** unlock them; only `IMGAGENT_ALLOW_METADATA=1` can, and it is never
  recommended.
- **Redirects** are followed manually (`max_redirects`, default 8), with full
  re-validation of every hop — a redirect into an internal network fails with
  `security_error`.
- **TLS verification is never disabled as a fallback.** There is no code path
  that turns it off automatically; setting `verify_tls = false` in config is an
  explicit, logged operator decision.

## Known limitation: DNS rebinding

DNS is re-resolved by the HTTP stack at connect time, so a strictly-timed DNS
rebinding race cannot be fully eliminated in-process. Mitigations in place:
per-request and per-redirect re-validation, response size caps, and TLS. For
hostile-network deployments, run imgagent inside an egress-restricted sandbox
(container with no route to internal ranges, or an allowlist proxy).

## Download validation

- Never trusts file extensions or `Content-Type`; validates by **magic bytes**
  then a **full Pillow decode** (`verify` + forced load for JPEG/small files).
- Rejects: HTML masquerading as images, corrupted payloads, empty files,
  oversized bodies (streamed byte cap, default 50 MiB), and images exceeding
  `max_pixels` (default 80 MP — decompression-bomb guard).
- Allowed formats by default: JPEG, PNG, WebP, GIF. AVIF requires the optional
  `imgagent[avif]` extra and is rejected with a clear dependency error
  otherwise.
- Writes are atomic: `.part` file → `os.replace`. A candidate's state only
  becomes `downloaded` after validation **and** final-path existence.
- Downloaded content is never executed.

## Storage & filenames

- Filenames are generated from slugified titles + candidate-id fragments;
  path traversal (`../`, absolute paths, Windows reserved names, leading
  dots) is sanitized.
- Metadata logs (`metadata.jsonl`) are append-only JSON lines; the SQLite
  database runs WAL mode with foreign keys on.

## Secrets

- API keys are read from environment variables only (`OPENVERSE_API_KEY`,
  `${VAR}` expansion in custom-source headers). They are never logged, never
  written to the database, manifests, or metadata files.
- imgagent performs no credential/cookie injection anywhere.

## Rate limits & politeness

- Bounded retries with exponential backoff + jitter and `Retry-After` support.
- Per-provider caps (e.g. Wikimedia: 2 API pages/query); concurrency-capped
  downloads; small JSON responses cached with a TTL to reduce provider load.
- No proxies are used to evade provider restrictions, and no access controls
  are bypassed. Blocked/limited providers fail explicitly and gracefully.
