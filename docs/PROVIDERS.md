# Provider support matrix

Last full integration-test run: **2026-10-12** (live Wikimedia + Openverse
search/download verified; see `tests/test_integration.py`).

| Provider | Name | Type | Key | Default | Best effort | Status |
|---|---|---|---|---|---|---|
| Wikimedia Commons | `wikimedia` | Official MediaWiki API | no | yes | no | **tested live** |
| Openverse | `openverse` | Official API | optional `OPENVERSE_API_KEY` | yes | no | **tested live** |
| DuckDuckGo Images | `duckduckgo` | Unofficial vqd flow | no | yes | **yes** | volatile; graceful failure verified |
| Pinterest | `pinterest` | Unofficial resource endpoint + SSR JSON | no | opt-in | **yes** | IP-reputation gated; graceful failure verified |
| Webpage extractor | `generic` | og:image / JSON-LD / `<img>` | no | inspect-only | yes | tested (mock + live pages) |
| Custom official APIs | `custom:<name>` | config-driven JSON API | per-config | opt-in | no | tested via mock adapter |

## wikimedia

- Query formats: free text; sent as `<query> filetype:bitmap` to the generator
  search, namespace 6 (File:).
- Auth: none. Politeness: max 50 results/page, hard cap 2 API pages per query,
  imgagent User-Agent, retry with backoff on 429/5xx.
- Returns: original file URL, thumbnail, description page, dimensions, mime,
  byte size, license name/URL, artist, description, computed attribution.
- Rights mapping: `Public domain*`/`CC0` → `verified_public_domain`;
  `CC BY*`/`CC BY-SA*`/`Attribution*`/`ShareAlike*` → `verified_license` with
  required notice recorded; anything else → `permission_required`.
- Formats: JPEG, PNG, GIF, WebP (AVIF when decoders allow); PDFs/DjVu/SVG
  filtered out client-side.
- Limitations: full-text search only (no spatial queries); some files lack
  structured license (mapped honestly to unknown/permission_required).
- Terms: official API usage — see Wikimedia API terms and each file's license.

## openverse

- Query formats: free text; anonymous tier caps `page_size` at 20.
- Auth: anonymous allowed; `OPENVERSE_API_KEY` (env only) raises limits.
- Returns: full image URL, thumbnail, foreign landing page, creator, license
  code/version/URL, dimensions, filetype, source collection.
- Rights mapping: `cc0`/`pdm` → `verified_public_domain`; all others →
  `verified_license` with `nc`/`nd` restrictions recorded in
  `extra.license_restrictions` for policy filtering.
- Limitations: anonymous rate limits (~100 req/day, 5 req/min); content varies
  by source collection.
- Terms: official API; each asset carries its own CC license terms.

## duckduckgo (best effort)

- Query formats: free text via the community `vqd` token flow.
- Auth: none. Unofficial — may throttle, region-lock or change without notice.
- Returns: image URL, thumbnail, source page, dimensions, no license metadata
  (`rights_status` stays `unknown`).
- Limitations: **rights are never inferred**; results require your own review.
  Timeouts/blocks surface as structured `errors[]` entries — never crashes the
  run.
- Terms: automated access must respect DDG policies; keep volumes low.

## pinterest (best effort, opt-in)

- Query formats: free text. Two anonymous strategies, tried in order:
  1. `GET {base}/resource/BaseSearchResource/get/` — the unofficial JSON
     endpoint the pinterest.com web app itself uses;
  2. the search page HTML → embedded `__PWS_DATA__` / `__PWS_INITIAL_PROPS__`
     JSON (server-side-rendered results), parsed location-agnostically.
- Auth: none. No cookies, no login — only public anonymous surfaces, with
  standard browser-like headers. Regional domains via
  `[sources.pinterest] base_url = "https://www.pinterest.de"` (or
  `IMGAGENT_SOURCE_PINTEREST_API_URL`).
- Returns: `i.pinimg.com/originals/` image URL (736x fallback), thumbnail,
  `pinterest.com/pin/<id>/` source page, dimensions, title/description,
  `extra.repin_count/board/pinner/outer_link`.
- Rights: always `unknown` — Pinterest exposes no license metadata, and a pin
  existing proves nothing about reuse rights. `--require-reuse-rights`
  therefore always excludes pinterest candidates; use this source for
  inspiration/mood-board discovery, not for rights-cleared publication.
- Limitations: **results are gated by IP reputation** — datacenter/VPN IPs
  commonly get HTTP 403 on strategy 1 and an empty "app shell" page (HTTP 200
  with no data) on strategy 2; both are reported honestly: the shell surfaces
  as a structured `errors[]` entry, never as fabricated results. Residential
  networks typically work. Endpoint shape may change at any time; only this
  adapter is affected (`best_effort`).
- Terms: automated access may conflict with Pinterest's Terms of Service; this
  adapter performs no bypass of access controls and keeps volumes low. Respect
  robots.txt and applicable law; treat results as page references.

## generic

- Input: any publicly accessible http(s) page or image URL.
- Extracts: `og:image` (+width/height), JSON-LD `image`/`thumbnailUrl`,
  `<link rel=image_src>`, first 20 `<img>` elements; data: URIs ignored.
- Pinterest pin URLs: resolved as **public page references** — og:image
  extracted when served; the pin URL is preserved as `source_page_url`.
  For keyless pin *search*, use the dedicated best-effort `pinterest` source
  (see above). Automated access may conflict with Pinterest's terms; treat as
  a study reference, not authorization to scrape.
- Rights: always `unknown` — extraction proves nothing about permission.
- Limitations: JS-rendered pages yield nothing (no headless browser in the
  base set); pages behind login/paywall fail explicitly.

## custom:<name> (bring your own official API)

- Declared in `config.toml [sources.custom.<name>]`: endpoint, params,
  headers (`${ENV_VAR}` expansion), `results_path`, per-field dot-paths.
- Suitable for Unsplash, Pexels, Flickr API, your own search infra — anything
  returning JSON with image URLs.
- Rights: whatever you declare via `rights_status` (default `unknown`).

## Explicitly not implemented

- Google Images HTML scraping (fragile, ToS-hostile). Configure an official
  API as a `custom:` source instead.
- Login/session cookies, residential proxies, TLS-fingerprint or CAPTCHA
  evasion for Pinterest or any other source — blocked means blocked, and the
  failure is reported honestly.
- CAPTCHA/paywall/login/access-control bypass of any kind.
