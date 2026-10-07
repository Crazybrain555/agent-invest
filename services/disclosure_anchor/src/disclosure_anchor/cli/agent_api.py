"""Run the private agent API: one loopback process, no access log.

``proxy_headers`` stays off so the peer is always the real socket peer, and
the server's own access log stays off because it would record query strings;
the app logs route templates only.
"""

from __future__ import annotations

import sys

from disclosure_anchor.agent_api import require_loopback_host
from disclosure_anchor.domain.errors import ConfigurationError
from disclosure_anchor.settings import load_settings


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("agent API takes no arguments; configure it through settings", file=sys.stderr)
        return 2
    settings = load_settings()
    try:
        require_loopback_host(settings.disclosure_agent_api_host)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import uvicorn

    uvicorn.run(
        "disclosure_anchor.agent_api:create_agent_api_app",
        factory=True,
        host=settings.disclosure_agent_api_host,
        port=settings.disclosure_agent_api_port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
