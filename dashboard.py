"""Server-rendered, three-color e-paper dashboard. No Home Assistant dependency."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from awc import fetch_map
from security import Credentials, BoundedHTTPServer
from battery import estimate as estimate_battery, parse_header as parse_battery_header

WIDTH, HEIGHT = 960, 672
BLACK, WHITE, RED = (0, 0, 0), (255, 255, 255), (255, 0, 0)
TZ = ZoneInfo(os.getenv('TZ', 'UTC'))
INTERVAL = max(60, int(os.getenv('REFRESH_SECONDS', '3600')))
DATA = Path(os.getenv('DATA_DIR', str(Path(__file__).parent / 'data')))
# Import defaults support offline rendering/tests; live startup requires HOME_*.
HOME = (float(os.getenv('HOME_LAT') or '0'), float(os.getenv('HOME_LON') or '0'))
WORK = ((float(os.environ['WORK_LAT']), float(os.environ['WORK_LON']))
        if os.getenv('WORK_LAT') and os.getenv('WORK_LON') else None)
LOCATION_LABEL = os.getenv('LOCATION_LABEL', 'DASHBOARD')
REGION_LABEL = os.getenv('REGION_LABEL', '')
MAP_ENABLED = os.getenv('MAP_ENABLED', 'true').lower() == 'true'
LOG = logging.getLogger('dashboard')


def fetch(url):
    req = Request(url, headers={'User-Agent': 'BillboardDashboard/0.1 (personal weather display)'})
    with urlopen(req, timeout=20) as response:
        body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise ValueError('response too large')
        return body, response.headers


def fetch_weather():
    query = urlencode(dict(latitude=HOME[0], longitude=HOME[1],
        hourly='temperature_2m,precipitation_probability,precipitation',
        daily='temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum',
        temperature_unit='fahrenheit', precipitation_unit='inch',
        timezone=str(TZ), timeformat='unixtime', forecast_days=8))
    body, _ = fetch('https://api.open-meteo.com/v1/forecast?' + query)
    data = json.loads(body)
    # Reject incomplete structure; null measurements remain explicitly missing.
    for group, keys in [('hourly', ['temperature_2m', 'precipitation_probability', 'precipitation']),
                        ('daily', ['temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max', 'precipitation_sum'])]:
        count = len(data[group]['time'])
        if count < (48 if group == 'hourly' else 7):
            raise ValueError('incomplete forecast')
        for key in keys:
            if len(data[group][key]) != count:
                raise ValueError('misaligned forecast')
    return {'data': data, 'fetched_at': time.time(), 'location': list(HOME)}


def fetch_satellite():
    return fetch_map(fetch, HOME)


def fetch_commute():
    if WORK is None:
        return {'status': 'disabled'}
    path = Path(os.getenv('TOMTOM_KEY_FILE', '/run/secrets/tomtom_key'))
    if not path.exists() or not (key := path.read_text().strip()):
        return {'status': 'needs_key'}
    result = {'status': 'ok', 'fetched_at': time.time()}
    for name, origin, destination in [('to_work', HOME, WORK), ('to_home', WORK, HOME)]:
        locations = f'{origin[0]},{origin[1]}:{destination[0]},{destination[1]}'
        query = urlencode(dict(key=key, traffic='true', departAt='now', travelMode='car',
                               routeType='fastest', routeRepresentation='summaryOnly'))
        body, _ = fetch(f'https://api.tomtom.com/routing/1/calculateRoute/{locations}/json?{query}')
        summary = json.loads(body)['routes'][0]['summary']
        seconds = summary['travelTimeInSeconds']
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError('invalid route duration')
        result[name] = {'minutes': math.ceil(seconds / 60),
                       'delay_minutes': round(summary.get('trafficDelayInSeconds', 0) / 60)}
    return result


def forecast_rows(weather, now):
    if not weather:
        return [], []
    data = weather['data']
    hourly, daily = data['hourly'], data['daily']
    # Include the current hour and the following 23 real hours.
    # Provider precipitation values still describe the preceding hour.
    start = math.floor(now / 3600) * 3600
    hours = []
    for i, stamp in enumerate(hourly['time']):
        if start <= stamp < start + 24 * 3600:
            hours.append({'time': stamp, **{k: hourly[k][i] for k in
                         ['temperature_2m', 'precipitation_probability', 'precipitation']}})
    today = datetime.fromtimestamp(now, TZ).date()
    days = []
    for i, stamp in enumerate(daily['time']):
        date = datetime.fromtimestamp(stamp, TZ).date()
        if today <= date < today + timedelta(days=7):
            days.append({'time': stamp, **{k: daily[k][i] for k in
                         ['temperature_2m_max', 'temperature_2m_min', 'precipitation_probability_max', 'precipitation_sum']}})
    return hours, days


FONTS = {}

def font(size, bold=False):
    key = (size, bold)
    if key not in FONTS:
        name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
        FONTS[key] = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/' + name, size)
    return FONTS[key]


def value(number, suffix='', places=0):
    return '--' if number is None else f'{number:.{places}f}{suffix}'


def stamp(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%b %d %I:%M%p').replace('AM', 'am').replace('PM', 'pm')


def render(weather, satellite, commute, errors, now, battery=None):
    im = Image.new('RGB', (WIDTH, HEIGHT), WHITE)
    d = ImageDraw.Draw(im)
    # Disable font antialiasing so all generated graphics have exactly three colors.
    d.fontmode = '1'
    def text(x, y, s, size=16, bold=False, color=BLACK, anchor=None):
        d.text((x, y), s, font=font(size, bold), fill=color, anchor=anchor)
    def line(y):
        d.line((20, y, 940, y), fill=BLACK, width=2)
    hours, days = forecast_rows(weather, now)
    text(20, 12, LOCATION_LABEL[:18], 24, True)
    text(20, 42, REGION_LABEL[:40], 12)
    meter = estimate_battery(battery, now, max_age=2*INTERVAL)
    color = RED if meter['low'] else BLACK
    # Battery icon and short label fit between the location and date.
    d.rectangle((370, 22, 403, 41), outline=color, width=2)
    d.rectangle((404, 27, 407, 36), fill=color)
    if meter['percent'] is not None:
        width = round(27 * meter['percent'] / 100)
        if width:
            d.rectangle((374, 26, 373+width, 37), fill=color)
        label = '~' + str(meter['percent']) + '%'
    elif meter['state'] == 'usb':
        label = 'USB'
    else:
        label = '—'
    text(416, 20, label, 18, True, color)
    if meter['low']:
        text(370, 45, 'Needs charging', 10, color=RED)
    text(940, 12, datetime.fromtimestamp(now, TZ).strftime('%A, %B %d'), 21, True, anchor='ra')
    text(940, 38, 'Updated ' + stamp(now) + ' ' + datetime.fromtimestamp(now, TZ).tzname(), 14, anchor='ra')
    line(63)
    stale = weather and (now - weather['fetched_at'] > INTERVAL * 1.5 or 'weather' in errors)
    text(20, 73, 'NEXT 24 HOURS', 18, True)
    for label, y in [('Hour ' + datetime.fromtimestamp(now, TZ).tzname(), 102), ('Temp °F', 151), ('Precip %', 194), ('Precip in', 215)]:
        text(20, y, label, 13, True)
    d.line((104, 100, 104, 232), fill=BLACK)
    if stale:
        text(225, 76, 'STALE ' + stamp(weather['fetched_at']), 13, True, RED)
    if hours:
        col = 828 / 24
        temps = [h['temperature_2m'] for h in hours if h['temperature_2m'] is not None]
        lo, hi = (min(temps), max(temps)) if temps else (0, 1)
        span = max(hi - lo, 10)
        previous = None
        for i, h in enumerate(hours):
            x = 112 + col * (i + .5)
            dt = datetime.fromtimestamp(h['time'], TZ)
            label = dt.strftime('%I%p').lstrip('0').lower().replace('am', 'a').replace('pm', 'p')
            text(x, 102, label, 14, True, BLACK, 'ma')
            t = h['temperature_2m']
            if t is not None:
                y = 178 - (t - lo) / span * 25
                if previous:
                    d.line((*previous, x, y), fill=BLACK, width=2)
                d.ellipse((x-2, y-2, x+2, y+2), fill=BLACK)
                text(x, y-22, value(t), 15, True, anchor='ma')
                previous = (x, y)
            else:
                previous = None
                text(x, 143, '--', anchor='ma')
            probability = h['precipitation_probability']
            text(x, 194, value(probability, '%'), 12, color=RED if probability is not None and probability >= 40 else BLACK, anchor='ma')
            text(x, 215, value(h['precipitation'], places=2), 13, anchor='ma')
    else:
        text(20, 143, 'Weather unavailable — waiting for a forecast', 22)
    line(239)
    text(20, 250, 'TODAY + NEXT 6 DAYS', 18, True)
    text(940, 253, 'HIGH / LOW °F   •   MAX HOURLY PRECIP %   •   TOTAL INCHES', 12, anchor='ra')
    for i, day in enumerate(days):
        x = 20 + i * 920 / 7
        if i:
            d.line((x, 282, x, 376), fill=BLACK)
        cx = x + 920 / 14
        dt = datetime.fromtimestamp(day['time'], TZ)
        text(cx, 282, 'Today' if i == 0 else dt.strftime('%a %d'), 17, True, anchor='ma')
        text(cx, 309, value(day['temperature_2m_max'], '°') + ' / ' + value(day['temperature_2m_min'], '°'), 23, True, anchor='ma')
        text(cx, 346, value(day['precipitation_probability_max'], '%') + '  ' + value(day['precipitation_sum'], '"', 2), 17, anchor='ma')
    if not days:
        text(20, 310, 'Daily forecast unavailable', 22)
    line(388)
    text(20, 398, 'REGIONAL WEATHER', 16, True)
    if satellite:
        thumb = Image.open(BytesIO(satellite['body']), formats=['PNG']).convert('RGB')
        im.paste(thumb, (20, 424))
        source_time = satellite.get('source_at')
        outdated = ('satellite' in errors or source_time is None or now - source_time > 7200)
        text(245, 439, 'AWC / NOAA', 14, True)
        text(245, 465, 'Infrared', 12)
        text(245, 483, datetime.fromtimestamp(satellite['satellite_at'], TZ).strftime('%m/%d %H:%M %Z'), 11)
        text(245, 508, 'Radar echoes', 12, color=RED)
        text(245, 526, datetime.fromtimestamp(satellite['radar_at'], TZ).strftime('%m/%d %H:%M %Z'), 11)
        d.ellipse((245, 558, 251, 564), fill=RED)
        text(257, 553, 'Home', 12)
        text(245, 580, 'Regional view', 11)
        if outdated:
            text(245, 604, 'STALE', 12, True, RED)
    else:
        text(20, 465, 'AWC map unavailable', 18)
    d.line((375, 404, 375, 635), fill=BLACK)
    text(398, 398, 'COMMUTE', 18, True)
    text(940, 402, 'LEAVING AT LAST UPDATE', 12, anchor='ra')
    for i, (key, label) in enumerate([('to_work', 'HOME → WORK'), ('to_home', 'WORK → HOME')]):
        x = 398 + i * 280
        text(x, 445, label, 17, True)
        route = commute.get(key) if commute.get('status') == 'ok' else None
        if route:
            text(x, 472, str(route['minutes']), 49, True)
            text(x+100, 500, 'min', 18)
            text(x, 539, f"Traffic delay {route['delay_minutes']} min", 15)
        else:
            text(x, 480, '—', 44, True)
            text(x, 542, {'needs_key': 'Awaiting API key', 'disabled': 'Not configured'}.get(commute.get('status'), 'Traffic unavailable'), 15)
    if commute.get('status') == 'ok':
        text(398, 590, 'TomTom • ' + stamp(commute['fetched_at']), 13)
    else:
        text(398, 590, 'TomTom routing ready to configure', 14)
    line(647)
    text(20, 653, 'Weather: Open-Meteo (CC BY 4.0) • Precip: liquid equivalent • Daily %: hourly maximum', 11)
    text(940, 653, f'Refresh {INTERVAL // 60} min', 11, anchor='ra')
    return im


def encode(im):
    """Row-major, MSB-first, 1=pigment; black plane followed by red plane."""
    pixels = list(im.get_flattened_data())
    black, red = bytearray(WIDTH * HEIGHT // 8), bytearray(WIDTH * HEIGHT // 8)
    for i, pixel in enumerate(pixels):
        if pixel == BLACK:
            black[i >> 3] |= 128 >> (i & 7)
        elif pixel == RED:
            red[i >> 3] |= 128 >> (i & 7)
        elif pixel != WHITE:
            raise ValueError('image contains non-palette color')
    # A palette PNG avoids expensive RGB decoding on small devices.
    palette = Image.new('P', (1, 1))
    palette.putpalette(list(WHITE + BLACK + RED) + [0] * (768 - 9))
    indexed = im.quantize(palette=palette, dither=Image.Dither.NONE)
    stream = BytesIO()
    indexed.save(stream, format='PNG', optimize=True)
    return stream.getvalue(), bytes(black + red)


class Dashboard:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.weather = None
        self.satellite = None
        self.commute = {'status': 'needs_key' if WORK else 'disabled'}
        self.errors = {}
        self.lock = threading.Lock()
        self.frames = {}
        self.render_lock = threading.Lock()
        self.telemetry_times = {}
        self.telemetry_last = float("-inf")
        self.battery_sample = None
        try:
            sample = json.loads((DATA / "battery.json").read_text())
            if isinstance(sample, dict):
                self.battery_sample = sample
        except (OSError, ValueError):
            pass
        try:
            weather = json.loads((DATA / 'weather.json').read_text())
            if weather.get('location') == list(HOME):
                self.weather = weather
        except (OSError, ValueError):
            pass
        try:
            meta = json.loads((DATA / 'satellite.json').read_text())
            if meta.get('provider') != 'awc-ir-rala-v1' or meta.get('location') != list(HOME):
                raise ValueError('different map provider or location')
            self.satellite = {**meta, 'body': (DATA / 'satellite.png').read_bytes()}
            with Image.open(BytesIO(self.satellite['body']), formats=['PNG']) as im:
                im.verify()
        except (OSError, ValueError):
            self.satellite = None
        if not MAP_ENABLED:
            self.satellite = None
        self.publish()

    def record_battery(self, supply_mv, device):
        # Never queue an unbounded number of expensive renders behind this lock.
        if not self.render_lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if now - self.telemetry_times.get(device, float('-inf')) < 60 or now - self.telemetry_last < 2:
                return False
            self.telemetry_times[device] = now
            self.telemetry_last = now
            self.battery_sample = {'supply_mv': supply_mv, 'sampled_at': time.time(), 'device': device}
            self.save('battery.json', json.dumps(self.battery_sample).encode())
            self._publish()
            return True
        finally:
            self.render_lock.release()

    def publish(self):
        with self.render_lock:
            self._publish()

    def _publish(self):
        now = time.time()
        png, binary = encode(render(self.weather, self.satellite, self.commute, self.errors, now, self.battery_sample))
        digest = sha256(binary).hexdigest()
        hours, days = forecast_rows(self.weather, now)
        status = {'rendered_at': now, 'errors': dict(self.errors),
                  'timezone': str(TZ), 'timezone_abbreviation': datetime.fromtimestamp(now, TZ).tzname(),
                  'hourly_start': hours[0]['time'] if hours else None,
                  'battery': estimate_battery(self.battery_sample, now, max_age=2*INTERVAL),
                  'weather_fetched_at': self.weather['fetched_at'] if self.weather else None,
                  'weather_complete': len(hours) == 24 and len(days) == 7,
                  'satellite_source_at': self.satellite.get('source_at') if self.satellite else None,
                  'map_provider': self.satellite.get('provider') if self.satellite else None,
                  'infrared_at': self.satellite.get('satellite_at') if self.satellite else None,
                  'radar_at': self.satellite.get('radar_at') if self.satellite else None,
                  'commute': self.commute.get('status')}
        manifest = {'version': 1, 'width': WIDTH, 'height': HEIGHT, 'refresh_seconds': INTERVAL,
                    'default_page': 'home', 'pages': [{'id': 'home', 'title': 'Weather & commute',
                    'sha256': digest, 'bytes': len(binary), 'format': 'black-red-msb-1',
                    'image_url': f'/v1/frames/{digest}.png', 'frame_url': f'/v1/frames/{digest}.bin'}],
                    'status': status}
        with self.lock:
            self.frames[digest] = (png, binary)
            while len(self.frames) > 4:
                self.frames.pop(next(iter(self.frames)))
            self.current = digest
            self.manifest = manifest
            self.status = status

    def refresh(self):
        jobs = {'weather': fetch_weather, 'commute': fetch_commute}
        if MAP_ENABLED:
            jobs['satellite'] = fetch_satellite
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {name: pool.submit(fn) for name, fn in jobs.items()}
            for name, future in futures.items():
                try:
                    result = future.result()
                    setattr(self, name, result)
                    self.errors.pop(name, None)
                    if name == 'weather':
                        self.save('weather.json', json.dumps(result).encode())
                    elif name == 'satellite':
                        self.save('satellite.png', result['body'])
                        self.save('satellite.json', json.dumps({k: v for k, v in result.items() if k != 'body'}).encode())
                except Exception as exc:
                    # URL-bearing exceptions can contain API keys. Log only the class.
                    self.errors[name] = type(exc).__name__
                    LOG.warning('%s fetch failed (%s)', name, type(exc).__name__)
                    if name == 'commute':
                        self.commute = {'status': 'unavailable'}
        self.publish()
        LOG.info('Dashboard refreshed; errors=%s', list(self.errors))

    @staticmethod
    def save(name, body):
        temporary = DATA / (name + '.tmp')
        temporary.write_bytes(body)
        temporary.replace(DATA / name)

    def run(self):
        while True:
            started = time.monotonic()
            try:
                self.refresh()
            except Exception as exc:
                LOG.error('Refresh failed (%s)', type(exc).__name__)
            time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


PREVIEW = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Billboard Dashboard</title><style>body{margin:24px;background:#eee;font:16px system-ui}img{width:960px;max-width:100%;height:auto;border:1px solid #333}a{color:#333}</style><img src="/dashboard.png" alt="Weather, satellite and commute dashboard"><p><a href="/dashboard.png">PNG</a> &middot; <a href="/v1/manifest.json">Device manifest</a> &middot; <a href="/status.json">Data status</a></p><script>setInterval(()=>{document.querySelector('img').src='/dashboard.png?t='+Date.now()},60000)</script></html>'''


def handler(app, credentials):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'Dashboard'
        sys_version = ''

        def do_GET(self):
            if len(self.path) > 256 or len(self.headers.get_all('Authorization', [])) > 1:
                self.send_error(400)
                return
            try:
                path = urlsplit(self.path).path
            except ValueError:
                self.send_error(400)
                return
            # Only the in-container Docker health probe bypasses authentication.
            health_probe = self.client_address[0] == '127.0.0.1' and path == '/healthz'
            identity = credentials.authenticate(self.headers.get('Authorization'))
            if not identity and not health_probe:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="Billboard Dashboard", charset="UTF-8"')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            supply = self.headers.get('X-Badge-Supply-Mv')
            if path == '/v1/manifest.json' and supply is not None:
                if not identity or identity[0] != 'device':
                    self.send_error(403)
                    return
                try:
                    supply_mv = parse_battery_header(supply)
                except ValueError:
                    self.send_error(400, 'Invalid battery telemetry')
                    return
                if not app.record_battery(supply_mv, identity[1]):
                    self.send_response(429)
                    self.send_header('Retry-After', '60')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            with app.lock:
                if path == '/':
                    body, content_type = PREVIEW, 'text/html; charset=utf-8'
                elif path in ('/healthz', '/status.json', '/v1/manifest.json'):
                    obj = app.manifest if path.endswith('manifest.json') else app.status
                    body, content_type = json.dumps(obj).encode(), 'application/json'
                elif path in ('/dashboard.png', '/v1/pages/home.png', '/v1/pages/home.bin') or path.startswith('/v1/frames/'):
                    digest = path.rsplit('/', 1)[-1].split('.')[0] if path.startswith('/v1/frames/') else app.current
                    if digest not in app.frames or not path.endswith(('.png', '.bin')):
                        self.send_error(404)
                        return
                    png, binary = app.frames[digest]
                    body, content_type = (png, 'image/png') if path.endswith('.png') else (binary, 'application/octet-stream')
                else:
                    self.send_error(404)
                    return
            etag = '"' + sha256(body).hexdigest() + '"'
            healthy = (app.status['weather_complete'] and app.status['weather_fetched_at'] is not None
                       and time.time() - app.status['weather_fetched_at'] < INTERVAL * 2
                       and time.time() - app.status['rendered_at'] < INTERVAL * 2)
            code = 503 if path == '/healthz' and not healthy else 200
            self.send_response(304 if code == 200 and self.headers.get('If-None-Match') == etag else code)
            self.send_header('ETag', etag)
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Type', content_type)
            self.send_header('X-Content-Type-Options', 'nosniff')
            if self.headers.get('If-None-Match') != etag:
                self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.headers.get('If-None-Match') != etag:
                self.wfile.write(body)

        def log_message(self, format, *args):
            pass
    return Handler


def validate_configuration():
    if not os.getenv('HOME_LAT') or not os.getenv('HOME_LON'):
        raise ValueError('Set HOME_LAT and HOME_LON; run scripts/setup.py first')
    locations = [HOME] + ([WORK] if WORK else [])
    if any(not (-85 <= lat <= 85 and -180 <= lon <= 180) for lat, lon in locations):
        raise ValueError('Coordinates outside supported range')
    if bool(os.getenv('WORK_LAT')) != bool(os.getenv('WORK_LON')):
        raise ValueError('Set both WORK_LAT and WORK_LON, or leave both empty')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='fetch and export preview/device files, then exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    validate_configuration()
    credentials = None if args.once else Credentials(os.environ.get('AUTH_FILE', '/run/secrets/auth.json'))
    app = Dashboard()
    if args.once:
        app.refresh()
        png, binary = app.frames[app.current]
        app.save('dashboard.png', png)
        app.save('dashboard.bin', binary)
        app.save('manifest.json', json.dumps(app.manifest, indent=2).encode())
        return
    threading.Thread(target=app.run, daemon=True).start()
    BoundedHTTPServer(('0.0.0.0', 8090), handler(app, credentials)).serve_forever()


if __name__ == '__main__':
    main()
