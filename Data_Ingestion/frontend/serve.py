"""
Standalone static dev server for the Intellidraft frontend (optional).

PREFERRED local dev: the Flask API server now serves this UI itself at the
same origin, so you only need one process:

    python Data_Ingestion/run_server.py
    # then open  http://localhost:7071/   (index.html) — /api is same-origin

Use THIS script only if you want to serve the static files separately:

    python Data_Ingestion/frontend/serve.py   # opens http://localhost:4000

In that separated setup the pages default their API base to same-origin
"/api", which won't reach the API on :7071 — type the full API URL
(http://localhost:7071/api) into the "API URL" box on the page.
"""
import http.server
import os
import webbrowser
from pathlib import Path

PORT    = 4000
SERVE_DIR = Path(__file__).parent   # …/Intellidraft/frontend/

class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(SERVE_DIR), **kw)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass   # quiet

if __name__ == "__main__":
    server = http.server.HTTPServer(("localhost", PORT), CORSHandler)
    url    = f"http://localhost:{PORT}"
    print(f"  Intellidraft UI  →  {url}  (port changed from 3000 to avoid conflicts)")
    print(f"  API expected at  →  http://localhost:7071/api")
    print("  Press Ctrl+C to stop.\n")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
