#!/usr/bin/env python3
"""Render an explicitly fictional dashboard, without network calls or private files."""
from datetime import datetime
from io import BytesIO
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as app
from PIL import Image, ImageDraw


def main():
    app.TZ = ZoneInfo('UTC')
    app.LOCATION_LABEL = 'BILLBOARD'
    app.REGION_LABEL = 'DEMO — FICTIONAL DATA'
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
    for i in range(5):
        d.arc((-70+i*22,20+i*8,200+i*25,230+i*9), 170,330,fill=app.BLACK,width=2)
    for x,y in [(55,70),(70,83),(80,102),(90,119)]: d.ellipse((x,y,x+12,y+6), fill=app.RED)
    d.ellipse((104,104,112,112),fill=app.RED)
    d.rectangle((10,180,205,210),fill=app.WHITE)
    d.text((18,188),'SIMULATED MAP',font=app.font(17,True),fill=app.BLACK)
    # Ensure the sample has precisely the panel's three colors.
    stream=BytesIO(); tile.save(stream,format='PNG')
    satellite={'body':stream.getvalue(),'source_at':now,'satellite_at':now,'radar_at':now}
    commute={'status':'ok','fetched_at':now,'to_work':{'minutes':24,'delay_minutes':3},
             'to_home':{'minutes':28,'delay_minutes':5}}
    image=app.render(weather,satellite,commute,{},now,{'supply_mv':3920,'sampled_at':now})
    png,_=app.encode(image)
    output=Path(__file__).resolve().parents[1]/'docs/demo.png'
    output.write_bytes(png)
    print('Wrote docs/demo.png using fictional data; no network requests made.')

if __name__ == '__main__': main()
