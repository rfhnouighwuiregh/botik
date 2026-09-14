import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


port = int(os.getenv("PORT", "10000"))

server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)

# Сначала запускаем HTTP health-сервер
threading.Thread(
    target=server.serve_forever,
    daemon=True
).start()

print(f"Health server started on port {port}", flush=True)

# Затем запускаем Telegram-бота
bot_process = subprocess.Popen([sys.executable, "bot.py"])

print("Starting bot.py...", flush=True)


def shutdown(signum=None, frame=None):
    if bot_process.poll() is None:
        bot_process.terminate()

    server.shutdown()

    try:
        bot_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        bot_process.kill()

    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

try:
    bot_process.wait()
finally:
    server.shutdown()