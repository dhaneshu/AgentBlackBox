from __future__ import annotations


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install agent-black-box[server] to run the service") from exc
    uvicorn.run("blackbox_server.app:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
