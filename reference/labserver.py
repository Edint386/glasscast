"""Tiny experiment server for the glasses screen-cast research.

GET /screen.jpg   -> 640x290 JPEG with the current time drawn on it (changes every request)
GET /screen.png   -> same as PNG
GET /clip.fset    -> 10-frame .fset (PNG frames, counter + time), new on every request
GET /clipjpg.fset -> 10-frame .fset with JPEG frames (format experiment)
GET /log          -> recent request log (to see what the simulator/device fetches)
"""
import io, struct, time, threading, json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image, ImageDraw, ImageFont

W, H = 640, 290
LOG = []
LOCK = threading.Lock()
COUNTER = [0]

try:
    FONT = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 56)
    SMALL = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 28)
except Exception:
    FONT = SMALL = ImageFont.load_default()


def frame(text, sub='', w=W, h=H):
    im = Image.new('RGB', (w, h), (10, 30, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([4, 4, w - 5, h - 5], outline=(255, 200, 0), width=4)
    d.text((30, 60), text, fill=(255, 255, 255), font=FONT)
    d.text((30, 170), sub, fill=(120, 220, 120), font=SMALL)
    return im


def encode(im, fmt):
    b = io.BytesIO()
    if fmt == 'JPEG':
        im.save(b, 'JPEG', quality=70)
    else:
        im.save(b, 'PNG', optimize=True)
    return b.getvalue()


def fset(blobs):
    n = len(blobs)
    off = 4 + 12 * n
    idx = [struct.pack('<I', n)]
    for b in blobs:
        idx.append(struct.pack('<III', off, len(b), 0))
        off += len(b)
    return b''.join(idx) + b''.join(blobs)


class H_(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        with LOCK:
            LOG.append(f'{time.strftime("%H:%M:%S")} {self.client_address[0]} {self.path} UA={self.headers.get("User-Agent","")[:60]}')
            del LOG[:-200]

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with LOCK:
            COUNTER[0] += 1
            c = COUNTER[0]
        p = self.path.split('?')[0]
        now = time.strftime('%H:%M:%S')
        if p == '/screen.jpg':
            self.send(encode(frame(now, f'req #{c} jpg'), 'JPEG'), 'image/jpeg')
        elif p == '/screen.png':
            self.send(encode(frame(now, f'req #{c} png'), 'PNG'), 'image/png')
        elif p in ('/clip.fset', '/clipjpg.fset'):
            fmt = 'JPEG' if p == '/clipjpg.fset' else 'PNG'
            blobs = [encode(frame(f'{now} f{i}', f'clip #{c} {fmt}'), fmt) for i in range(10)]
            self.send(fset(blobs), 'application/octet-stream')
        elif p == '/log':
            with LOCK:
                self.send('\n'.join(LOG).encode(), 'text/plain; charset=utf-8')
        elif p == '/healthz':
            self.send(b'ok', 'text/plain')
        else:
            self.send_response(404); self.end_headers()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print('serving on', port, flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), H_).serve_forever()
