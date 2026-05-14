import os
import socket
import threading
import webbrowser
from weepulse_monitor import create_app

def find_free_port(preferred=5000):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

def open_browser(url: str):
    webbrowser.open(url)

def main():
    app = create_app()

    port = int(os.getenv("WEEPULSE_PORT", "5000"))
    port = find_free_port(port)
    url = f"http://127.0.0.1:{port}"

    threading.Timer(1.0, open_browser, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, debug=False)

if __name__ == "__main__":
    main()