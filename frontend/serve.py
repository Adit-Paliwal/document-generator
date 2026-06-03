"""
Simple dev server for the Intellidraft frontend.
Run from the Intellidraft/ directory:

    python frontend/serve.py

Then open http://localhost:4000 in your browser.
The Flask API must also be running on port 7071:

    python Data_Ingestion/run_server.py
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
