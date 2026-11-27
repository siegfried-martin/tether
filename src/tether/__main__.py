"""Admin commands.

    python -m tether serve [--reload]   run the app (one worker, always)
    python -m tether keygen             print a new encryption key
    python -m tether vapid-key          print a new Web Push (VAPID) private key
"""

import sys

USAGE = "usage: python -m tether serve [--reload] | keygen | vapid-key"


def serve(reload: bool) -> None:
    import uvicorn

    from tether.config import get_settings

    settings = get_settings()
    # Hard rule: exactly one worker. The pollers live in this process; a second
    # worker would duplicate every one of them, so there is no option for more.
    uvicorn.run(
        "tether.web.app:main_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=reload,
        # Trust X-Forwarded-* only from the local reverse proxy (uvicorn's default allow-list).
        proxy_headers=True,
        server_header=False,
    )


def main() -> None:
    args = sys.argv[1:]
    if args == ["keygen"]:
        from tether.crypto import generate_key

        print(generate_key())
    elif args == ["vapid-key"]:
        from tether.connectors.webpush import Vapid

        print(Vapid.generate())
    elif args and args[0] == "serve" and set(args[1:]) <= {"--reload"}:
        serve(reload="--reload" in args)
    else:
        print(USAGE, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
