from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402  (must load .env first)

app = create_app()

if __name__ == "__main__":
    import os
    import threading

    port = int(os.environ.get("PORT", 8000))

    # "localhost" can resolve to ::1 (IPv6) or 127.0.0.1 (IPv4) depending on
    # the client's resolver order — on macOS this commonly prefers IPv6,
    # unlike some other setups. Binding only host="0.0.0.0" (IPv4-only)
    # means clients whose "localhost" maps to ::1 get connection-refused
    # even though 127.0.0.1 works. Binding host="::" alone doesn't fix this
    # either: macOS defaults new IPv6 sockets to v6-only (unlike Linux's
    # dual-stack default), so it wouldn't also accept 127.0.0.1. Run both an
    # IPv4 and an IPv6 listener on the same port so both forms work.
    def _run_ipv6():
        try:
            app.run(host="::", port=port, use_reloader=False)
        except OSError as exc:
            print(f"IPv6 listener on [::]:{port} unavailable ({exc}); continuing on IPv4 only.")

    threading.Thread(target=_run_ipv6, daemon=True).start()
    app.run(host="0.0.0.0", port=port, use_reloader=False)
