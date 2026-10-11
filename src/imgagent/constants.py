"""Stable constants: schema versions, supported formats, exit-code contract."""

# --- Schema versioning -------------------------------------------------------
CANDIDATE_SCHEMA = "imgagent.candidate/v1"
MANIFEST_SCHEMA = "imgagent.manifest/v1"
RESULT_SCHEMA = "imgagent.result/v1"
INSTRUCTIONS_SCHEMA = "imgagent.agent-instructions/v1"

# --- Exit-code contract (documented + tested) --------------------------------
EXIT_OK = 0              # success, or explicitly represented partial success
EXIT_NO_RESULTS = 1      # no usable results, or unsuccessful operation
EXIT_USAGE = 2           # invalid input or usage
EXIT_CONFIG = 3          # configuration or dependency problem
EXIT_INTERRUPTED = 130   # interrupted by user (Ctrl-C)

# --- Image formats -----------------------------------------------------------
# Magic-byte sniffed formats we accept by default. AVIF needs pillow-avif-plugin.
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
FORMAT_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "AVIF": ".avif"}

# --- Rights states (see docs/SECURITY.md, docs/PROVIDERS.md) ------------------
RIGHTS_VERIFIED_PD = "verified_public_domain"
RIGHTS_VERIFIED_LICENSE = "verified_license"
RIGHTS_PERMISSION_REQUIRED = "permission_required"
RIGHTS_UNKNOWN = "unknown"
RIGHTS_RESTRICTED = "restricted"
RIGHTS_STATES = (
    RIGHTS_VERIFIED_PD,
    RIGHTS_VERIFIED_LICENSE,
    RIGHTS_PERMISSION_REQUIRED,
    RIGHTS_UNKNOWN,
    RIGHTS_RESTRICTED,
)

# Licenses that allow commercial reuse without case-by-case review.
# "nc"/"nd" variants are still verified licenses but flagged non-commercial.
NC_ND_TOKENS = ("nc", "nd")

# --- Authenticity ------------------------------------------------------------
AUTH_UNKNOWN = "unknown"
AUTH_HISTORICAL_PHOTO = "likely_authentic_historical"
AUTH_ILLUSTRATIVE = "likely_illustrative"

# --- Download states ---------------------------------------------------------
DL_NOT_DOWNLOADED = "not_downloaded"
DL_DOWNLOADING = "downloading"
DL_DOWNLOADED = "downloaded"
DL_FAILED = "failed"

# --- Misc limits -------------------------------------------------------------
MAX_REDIRECTS_DEFAULT = 8
MAX_PIXELS_DEFAULT = 80_000_000        # 80 MP safety guard against pixel bombs
DEFAULT_MAX_BYTES = 52_428_800         # 50 MiB per file
API_RESPONSE_MAX_BYTES = 20_485_760    # 20 MiB cap for JSON/API responses
