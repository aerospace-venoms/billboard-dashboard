#!/usr/bin/env python3
"""Render an offline example, or fetch public Pasadena weather with --live."""
from datetime import datetime
import argparse
import time
from io import BytesIO
import math
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as app
from PIL import Image, ImageDraw
from awc import world_pixel, ZOOM, SPAN

# Public civic-center reference, not a private home or workplace.
PASADENA_CITY_HALL = (34.1478, -118.1436)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Fetch real Pasadena forecast and AWC imagery; no API key')
    parser.add_argument('--output', type=Path, help='Output PNG; defaults to docs/demo.png for live, generated/demo-offline.png otherwise')
    args = parser.parse_args()
    app.HOME = PASADENA_CITY_HALL
    app.INTERVAL = 3600
    app.TZ = ZoneInfo('America/Los_Angeles')
    app.LOCATION_LABEL = 'PASADENA'
    app.REGION_LABEL = 'CALIFORNIA — FICTIONAL WEATHER'
    now = datetime(2030, 6, 1, 9, tzinfo=app.TZ).timestamp()
    hours = [now+i*3600 for i in range(24)]
    days = [datetime(2030, 6, 1+i, tzinfo=app.TZ).timestamp() for i in range(7)]
    weather = {'fetched_at': now, 'data': {
        'hourly': {'time': hours, 'temperature_2m': [round(65+12*math.sin(i/5)) for i in range(24)],
                   'precipitation_probability': [10]*8+[60]*5+[30]*4+[10]*7,
                   'precipitation': [0]*8+[.04,.08,.12,.06,.02]+[0]*11},
        'daily': {'time': days, 'temperature_2m_max': [77,72,75,80,82,78,74],
                  'temperature_2m_min': [57,55,58,60,61,59,56],
                  'precipitation_probability_max': [60,40,20,10,10,30,50],
                  'precipitation_sum': [.32,.12,.02,0,0,.04,.18]}}}
    tile = Image.new('RGB',(216,216), app.WHITE)
    d = ImageDraw.Draw(tile)
    d.fontmode = '1'
    # Real public-domain coastal geography centered on Pasadena City Hall.
    cx, cy = world_pixel(*PASADENA_CITY_HALL, ZOOM)
    def project(lon, lat):
        x, y = world_pixel(lat, lon, ZOOM)
        return ((x-cx+SPAN/2)*216/SPAN, (y-cy+SPAN/2)*216/SPAN)
    land=json.loads((Path(__file__).resolve().parents[1]/'assets/land.geojson').read_text())
    for feature in land['features']:
        geometry=feature['geometry']
        polygons=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates']
        for polygon in polygons:
            for ring in polygon:
                points=[project(lon,lat) for lon,lat in ring]
                for first,second in zip(points,points[1:]):
                    if abs(second[0]-first[0]) < (256*2**ZOOM/2)*216/SPAN:
                        d.line((first,second),fill=app.BLACK,width=2)
    # Stylized clouds/echoes are explicitly fictional, not current imagery.
    for i in range(3):
        d.arc((30+i*18,12+i*12,190+i*18,170+i*12),170,280,fill=app.BLACK,width=1)
    for x,y in [(55,70),(70,83),(80,102),(90,119)]: d.ellipse((x,y,x+12,y+6), fill=app.RED)
    d.ellipse((104,104,112,112),fill=app.RED)
    d.rectangle((10,180,205,210),fill=app.WHITE)
    d.text((18,188),'SIMULATED WEATHER',font=app.font(14,True),fill=app.BLACK)
    # Ensure the sample has precisely the panel's three colors.
    stream=BytesIO(); tile.save(stream,format='PNG')
    satellite={'body':stream.getvalue(),'source_at':now,'satellite_at':now,'radar_at':now,
               'source_label':'DEMO ONLY', 'marker_label':'City Hall'}
    commute={'status':'ok','example':True,'fetched_at':now,'to_work':{'minutes':24,'delay_minutes':3},
             'to_home':{'minutes':28,'delay_minutes':5}}
    battery = {'supply_mv':3920,'sampled_at':now}
    if args.live:
        # Never load .env, a deployment cache, Wi-Fi settings, or a routing key.
        weather = app.fetch_weather()
        satellite = app.fetch_satellite()
        satellite['marker_label'] = 'City Hall'
        now = time.time()
        hours, days = app.forecast_rows(weather, now)
        if len(hours) != 24 or len(days) != 7:
            raise ValueError('Incomplete public forecast; previous preview left untouched')
        app.REGION_LABEL = 'CALIFORNIA — WEATHER SNAPSHOT'
        battery = None  # This public example has no physical badge to measure.
    image=app.render(weather,satellite,commute,{},now,battery)
    png,_=app.encode(image)
    root = Path(__file__).resolve().parents[1]
    output = args.output or root/('docs/demo.png' if args.live else 'generated/demo-offline.png')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(png)
    print(f'Wrote {output.name}: '+('real Pasadena weather/imagery; example commute only.' if args.live else 'fictional data; no network requests made.'))

if __name__ == '__main__': main()
