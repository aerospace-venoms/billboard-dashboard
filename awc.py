"""AWC GFA's public, timestamped Web Mercator/TMS image tiles.

Endpoints verified against AWC's GFA map client, November 2026. These are public
website image services, not part of AWC's documented /api/data contract.
"""
from datetime import datetime, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import re
import time

from PIL import Image, ImageDraw, ImageOps

BASE = 'https://aviationweather.gov'
SIZE = 216
ZOOM = 7
SPAN = 256  # Viewport span in level-7 Web Mercator pixels.


def world_pixel(lat, lon, zoom):
    """XYZ/Web Mercator global pixels; tile requests convert XYZ y to TMS y."""
    scale = 256 * 2 ** zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    return ((lon + 180) / 360 * scale,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * scale)


def tile_stamp(body, radar=False):
    text = body.decode().strip()
    if radar:
        dates = json.loads(text)
        text = max(d for d in dates if d) if dates else ''
    if not re.fullmatch(r'\d{12}', text):
        raise ValueError('invalid AWC image timestamp')
    return text, datetime.strptime(text, '%Y%m%d%H%M').replace(tzinfo=timezone.utc).timestamp()


def mosaic(fetch, layer, timestamp, home, zoom, span):
    cx, cy = world_pixel(*home, zoom)
    left, top = math.floor(cx - span / 2), math.floor(cy - span / 2)
    right, bottom = left + span, top + span
    x0, y0 = left // 256, top // 256
    x1, y1 = (right - 1) // 256, (bottom - 1) // 256
    canvas = Image.new('RGBA', ((x1-x0+1)*256, (y1-y0+1)*256))
    for y in range(y0, y1+1):
        for x in range(x0, x1+1):
            prefix = 'rad' if layer.startswith('rad_') else 'sat'
            url = (f'{BASE}/data/tilecache/{layer}/{timestamp[:8]}/{timestamp[8:]}/'
                   f'{zoom}/{prefix}_{x}_{2**zoom-1-y}.png')
            body, _ = fetch(url)
            with Image.open(BytesIO(body), formats=['PNG']) as tile:
                if tile.size != (256, 256):
                    raise ValueError('unexpected AWC tile dimensions')
                canvas.paste(tile.convert('RGBA'), ((x-x0)*256, (y-y0)*256))
            # Respect AWC guidance to wait between consecutive requests.
            time.sleep(.1)
    return canvas.crop((left-x0*256, top-y0*256, right-x0*256, bottom-y0*256)), (left, top)


def fetch_map(fetch, home):
    date = datetime.now(timezone.utc).strftime('%Y%m%d%H%M')
    sat_body, _ = fetch(f'{BASE}/api/info/satellite?type=ir&format=tiledate&date={date}')
    sat_stamp, sat_time = tile_stamp(sat_body)
    time.sleep(.1)
    radar_body, _ = fetch(f'{BASE}/api/info/radar?type=rala&format=json&num=1&date={date}')
    rad_stamp, rad_time = tile_stamp(radar_body, radar=True)
    # Satellite max native zoom is 6; radar is 7, as specified by AWC's client.
    # Use one common level-7 viewport to avoid a subpixel shift between layers.
    # Request a slightly larger satellite crop, then transform to exact bounds.
    radar, (left, top) = mosaic(fetch, 'rad_rala', rad_stamp, home, ZOOM, SPAN)
    satellite, (sl, st) = mosaic(fetch, 'sat_ir', sat_stamp, home, ZOOM-1, SPAN//2+2)
    satellite = satellite.transform((SIZE, SIZE), Image.Transform.EXTENT,
        (left/2-sl, top/2-st, (left+SPAN)/2-sl, (top+SPAN)/2-st), Image.Resampling.BILINEAR)
    # Fixed tonal mapping: hotter/darker IR pixels become pale; cold/bright cloud
    # tops become dense black stipple. This is IR brightness, not a cloud mask.
    gray = satellite.convert('L').point(lambda v: round(255 * (1 - max(0, min(1, (v-90)/165)))))
    im = gray.convert('1').convert('RGB')
    d = ImageDraw.Draw(im)
    def project(lat, lon):
        x, y = world_pixel(lat, lon, ZOOM)
        return ((x-left)*SIZE/SPAN, (y-top)*SIZE/SPAN)
    states = json.loads((Path(__file__).parent/'assets/land.geojson').read_text())
    for feature in states['features']:
        geometry = feature['geometry']
        polygons = [geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates']
        for polygon in polygons:
            for ring in polygon:
                d.line([project(lat, lon) for lon, lat in ring], fill=(0, 0, 0), width=1)
    # RALA is radar reflectivity at lowest altitude. Label as echoes, not proven
    # surface rain; clear-air/nonmeteorological echoes can occur in radar data.
    echoes = radar.getchannel('A').resize((SIZE, SIZE), Image.Resampling.NEAREST)
    echoes = echoes.point(lambda v: 255 if v > 0 else 0)
    im.paste((255, 0, 0), (0, 0, SIZE, SIZE), echoes)
    hx, hy = project(*home)
    d.ellipse((hx-5, hy-5, hx+5, hy+5), fill=(255,255,255), outline=(0,0,0))
    d.ellipse((hx-3, hy-3, hx+3, hy+3), fill=(255,0,0))
    buffer = BytesIO()
    im.save(buffer, format='PNG')
    return {'body': buffer.getvalue(), 'provider': 'awc-ir-rala-v1', 'location': list(home),
            'fetched_at': time.time(), 'source_at': min(sat_time, rad_time),
            'satellite_at': sat_time, 'radar_at': rad_time}
