"""Static configuration for the Claude usage widget."""

# This app.
APP_VERSION = "1.1.8"
REPO_URL = "https://github.com/ali-dev178/claudometer"

# Server-reported plan-usage endpoint (same one Claude Code's /usage panel calls).
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# OAuth token-refresh endpoints. Anthropic is mid-migration from console -> platform,
# so we try console first and fall back to platform on connection error / 404.
TOKEN_URLS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
]

# Well-known Claude Code OAuth client id (public).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Required beta header for the OAuth usage endpoint.
BETA_HEADER = "oauth-2025-04-20"

# Network timeout (seconds) for both the usage call and token refresh.
HTTP_TIMEOUT = 15

# Default poll interval (seconds). Overridable via env CLAUDE_WIDGET_POLL,
# clamped to [60, 300]. Community tools use 90-180s to stay under rate limits.
DEFAULT_POLL = 90

# Refresh the access token this many ms before it expires.
REFRESH_SKEW_MS = 5 * 60 * 1000

# Used in the mandatory "User-Agent: claude-code/<version>" header when the real
# installed version can't be read. A generic UA gets persistent HTTP 429s.
FALLBACK_VERSION = "2.1.173"
