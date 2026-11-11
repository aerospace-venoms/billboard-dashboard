import copy
from datetime import datetime, timedelta
from hashlib import sha256
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as app
from PIL import Image


def weather(now):
    midnight = datetime.fromtimestamp(now, app.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    timestamps = [int(midnight.timestamp()) + i * 3600 for i in range(200)]
    dates = [int((midnight + timedelta(days=i)).timestamp()) for i in range(8)]
    return {'location': list(app.HOME), 'fetched_at': now, 'data': {
        'hourly': {'time': timestamps, 'temperature_2m': [50] * 200,
                   'precipitation_probability': [40] * 200, 'precipitation': [.12] * 200},
        'daily': {'time': dates, 'temperature_2m_max': [60] * 8, 'temperature_2m_min': [40] * 8,
                  'precipitation_probability_max': [80] * 8, 'precipitation_sum': [.5] * 8}}}


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.zone = patch.object(app, 'TZ', app.ZoneInfo('America/Chicago'))
        self.zone.start()
        self.addCleanup(self.zone.stop)

    def test_24_real_hours_across_dst_and_seven_local_days(self):
        for date in ['2026-11-01T00:30', '2026-03-08T00:30', '2026-11-10T23:59', '2026-11-11T00:00']:
            now = datetime.fromisoformat(date).replace(tzinfo=app.TZ).timestamp()
            hours, days = app.forecast_rows(weather(now), now)
            self.assertEqual(len(hours), 24)
            self.assertEqual(len(days), 7)
            self.assertLessEqual(hours[0]['time'], now)
            self.assertLess(now - hours[0]['time'], 3600)
            self.assertEqual(datetime.fromtimestamp(hours[0]['time'], app.TZ).hour, datetime.fromtimestamp(now, app.TZ).hour)
            self.assertEqual(hours[-1]['time'] - hours[0]['time'], 23 * 3600)
            self.assertEqual(datetime.fromtimestamp(days[0]['time'], app.TZ).date(), datetime.fromtimestamp(now, app.TZ).date())

    def test_missing_is_not_zero_and_expired_forecasts_disappear(self):
        now = 1794366000
        data = weather(now)
        data['data']['hourly']['precipitation'][1] = None
        self.assertEqual(app.value(None), '--')
        self.assertEqual(app.value(0, places=2), '0.00')
        self.assertEqual(app.forecast_rows(data, now + 10 * 86400), ([], []))

    def test_render_and_binary_roundtrip(self):
        now = 1794366000
        w = weather(now)
        w['data']['hourly']['temperature_2m'][2] = None
        w['data']['daily']['precipitation_sum'][0] = None
        im = app.render(w, None, {'status': 'needs_key'}, {}, now)
        png, binary = app.encode(im)
        self.assertEqual(len(binary), 161280)
        self.assertEqual(Image.open(BytesIO(png)).size, (960, 672))
        reconstructed = Image.new('RGB', (960, 672), app.WHITE)
        pixels = []
        for i in range(960 * 672):
            bit = 128 >> (i & 7)
            b, r = binary[i >> 3] & bit, binary[80640 + (i >> 3)] & bit
            self.assertFalse(b and r)
            pixels.append(app.RED if r else app.BLACK if b else app.WHITE)
        reconstructed.putdata(pixels)
        self.assertEqual(reconstructed.tobytes(), im.tobytes())
        self.assertEqual(Image.open(BytesIO(png)).convert('RGB').tobytes(), im.tobytes())

    def test_mercator_reference_and_awc_times(self):
        from awc import world_pixel, tile_stamp
        self.assertEqual(world_pixel(0, 0, 0), (128, 128))
        x, y = world_pixel(0, 0, 7)
        self.assertEqual((int(x//256), 127-int(y//256)), (64, 63))
        self.assertEqual(tile_stamp(b'["202611110300", "202611110350"]', radar=True)[0], '202611110350')
        with self.assertRaises(ValueError):
            tile_stamp(b'<html>Unavailable</html>')

    def test_awc_composite_radar_alignment_palette_and_home(self):
        from awc import fetch_map, world_pixel
        cx, cy = world_pixel(*app.HOME, 7)
        def fake_fetch(url):
            if '/api/info/satellite' in url:
                return b'202611110300', {}
            if '/api/info/radar' in url:
                return b'["202611110310"]', {}
            tile = Image.new('RGBA', (256, 256), (120,120,120,255))
            if '/rad_rala/' in url:
                tile = Image.new('RGBA', (256, 256))
                name = url.rsplit('/', 1)[1].removesuffix('.png').split('_')
                tx, ty = int(name[1]), 127-int(name[2])
                from PIL import ImageDraw
                ImageDraw.Draw(tile).rectangle((cx-40-tx*256, cy-40-ty*256,
                                               cx-30-tx*256, cy-30-ty*256), fill=(0,200,0,255))
            buffer = BytesIO()
            tile.save(buffer, format='PNG')
            return buffer.getvalue(), {}
        with patch('awc.time.sleep'):
            result = fetch_map(fake_fetch, app.HOME)
        im = Image.open(BytesIO(result['body']))
        self.assertEqual(im.size, (216,216))
        self.assertTrue(set(im.getdata()) <= {app.WHITE, app.BLACK, app.RED})
        self.assertEqual(im.getpixel((108,108)), app.RED)  # Home at viewport center.
        self.assertEqual(im.getpixel((79,79)), app.RED)  # Radar patch northwest.
        self.assertNotEqual(im.getpixel((140,140)), app.RED)
        self.assertLess(result['satellite_at'], result['radar_at'])

    def test_plane_bit_order(self):
        im = Image.new('RGB', (960, 672), app.WHITE)
        im.putpixel((0, 0), app.BLACK)
        im.putpixel((7, 0), app.RED)
        im.putpixel((0, 1), app.RED)
        _, frame = app.encode(im)
        self.assertEqual(frame[0], 0x80)
        self.assertEqual(frame[80640], 0x01)
        self.assertEqual(frame[80640 + 120], 0x80)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.destination = patch.object(app, 'WORK', (1.0, 1.0))
        self.destination.start()
        self.addCleanup(self.destination.stop)

    def test_both_traffic_directions_use_depart_now(self):
        response = {'routes': [{'summary': {'travelTimeInSeconds': 961, 'trafficDelayInSeconds': 120}}]}
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / 'key'
            key.write_text('fake-test-key')
            with patch.dict(app.os.environ, {'TOMTOM_KEY_FILE': str(key)}), patch.object(app, 'fetch', return_value=(json.dumps(response).encode(), {})) as get:
                result = app.fetch_commute()
        self.assertEqual(result['to_work']['minutes'], 17)
        self.assertEqual(result['to_home']['delay_minutes'], 2)
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertIn('traffic=true', call.args[0])
            self.assertIn('departAt=now', call.args[0])
        self.assertIn(f'{app.HOME[0]},{app.HOME[1]}:{app.WORK[0]},{app.WORK[1]}', get.call_args_list[0].args[0])
        self.assertIn(f'{app.WORK[0]},{app.WORK[1]}:{app.HOME[0]},{app.HOME[1]}', get.call_args_list[1].args[0])

    def test_no_key_no_network(self):
        with patch.dict(app.os.environ, {'TOMTOM_KEY_FILE': '/nonexistent-test-key'}), patch.object(app, 'fetch') as get:
            self.assertEqual(app.fetch_commute()['status'], 'needs_key')
            get.assert_not_called()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(app, 'DATA', Path(self.tmp.name))
        self.patch.start()
        self.dashboard = app.Dashboard()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_failure_keeps_weather_but_not_old_traffic_and_survives_restart(self):
        now = app.time.time()
        forecast = weather(now)
        with patch.object(app, 'fetch_weather', return_value=forecast), patch.object(app, 'fetch_satellite', side_effect=TimeoutError), patch.object(app, 'fetch_commute', return_value={'status': 'needs_key'}):
            self.dashboard.refresh()
        self.dashboard.commute = {'status': 'ok', 'fetched_at': now, 'to_work': {'minutes': 15, 'delay_minutes': 0}}
        with patch.object(app, 'fetch_weather', side_effect=TimeoutError), patch.object(app, 'fetch_satellite', side_effect=TimeoutError), patch.object(app, 'fetch_commute', side_effect=TimeoutError):
            self.dashboard.refresh()
        self.assertEqual(self.dashboard.weather, forecast)
        self.assertEqual(self.dashboard.commute['status'], 'unavailable')
        self.assertIn('weather', self.dashboard.status['errors'])
        restarted = app.Dashboard()
        self.assertEqual(restarted.weather, forecast)
        self.assertEqual(restarted.commute['status'], 'disabled')

    def test_http_manifest_integrity_etag_unknown_page_health(self):
        credentials_path = Path(self.tmp.name) / 'auth.json'
        credentials_path.write_text(json.dumps({'devices': {'test': sha256(b'test-token').hexdigest()},
                                               'viewers': {'test': sha256(b'test-password').hexdigest()}}))
        credentials = app.Credentials(credentials_path)
        server = ThreadingHTTPServer(('127.0.0.1', 0), app.handler(self.dashboard, credentials))
        def authenticated_open(request):
            if isinstance(request, str):
                request = Request(request)
            request.add_header('Authorization', 'Bearer test-token')
            return urlopen(request)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with authenticated_open(base + '/v1/manifest.json') as r:
                manifest = json.load(r)
            self.assertEqual(manifest['status']['battery']['state'], 'unknown')
            req = Request(base + '/v1/manifest.json', headers={'X-Badge-Supply-Mv':'3730'})
            with authenticated_open(req) as r:
                manifest = json.load(r)
            self.assertEqual(manifest['status']['battery']['percent'], 20)
            self.assertTrue(manifest['status']['battery']['low'])
            self.assertEqual(app.Dashboard().battery_sample['supply_mv'], 3730)
            with self.assertRaises(HTTPError) as invalid:
                authenticated_open(Request(base + '/v1/manifest.json', headers={'X-Badge-Supply-Mv':'invalid'}))
            self.assertEqual(invalid.exception.code, 400)
            page = manifest['pages'][0]
            with authenticated_open(base + page['frame_url']) as r:
                frame, etag = r.read(), r.headers['ETag']
            self.assertEqual(sha256(frame).hexdigest(), page['sha256'])
            self.assertEqual(len(frame), page['bytes'])
            with self.assertRaises(HTTPError) as error:
                authenticated_open(Request(base + page['frame_url'], headers={'If-None-Match': etag}))
            self.assertEqual(error.exception.code, 304)
            for path, code in [('/v1/pages/missing.png', 404), ('/healthz', 503)]:
                with self.assertRaises(HTTPError) as error:
                    authenticated_open(base + path)
                self.assertEqual(error.exception.code, code)
            self.dashboard.weather = weather(app.time.time())
            self.dashboard.publish()
            with authenticated_open(base + '/healthz') as r:
                self.assertEqual(r.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
