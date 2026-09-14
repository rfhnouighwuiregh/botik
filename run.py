import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


port = int(os.getenv("PORT", "10000"))

server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)

# Start the Telegram bot as a separate process.
bot_process = subprocess.Popen([sys.executable, "bot.py"])


def shutdown(signum=None, frame=None):
    server.shutdown()
    if bot_process.poll() is None:
        bot_process.terminate()
        try:
            bot_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            bot_process.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

print(f"Health server started on port {port}")
print("Starting bot.py...")

threading.Thread(target=server.serve_forever, daemon=True).start()

# Keep this process alive while the bot is running.
try:
    bot_process.wait()
finally:
    server.shutdown()
