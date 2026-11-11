import importlib.util
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from test_dashboard import weather

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('demo_script',ROOT/'scripts/demo.py')
demo=importlib.util.module_from_spec(spec);spec.loader.exec_module(demo)

class DemoTests(unittest.TestCase):
    def run_demo(self, live):
        now=1906574400
        stream=BytesIO();Image.new('RGB',(216,216),'white').save(stream,format='PNG')
        satellite={'body':stream.getvalue(),'source_at':now,'satellite_at':now,'radar_at':now}
        original={name:getattr(demo.app,name) for name in ['TZ','HOME','INTERVAL','LOCATION_LABEL','REGION_LABEL']}
        def forecast():
            self.assertEqual(demo.app.HOME,demo.PASADENA_CITY_HALL)
            return weather(now)
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(demo.app,**original):
            output=Path(tmp)/'demo.png'
            argv=['demo.py','--output',str(output)]+(['--live'] if live else [])
            with patch('sys.argv',argv),patch.object(demo.time,'time',return_value=now), \
                 patch.object(demo.app,'fetch_weather',side_effect=forecast) as fetch_weather, \
                 patch.object(demo.app,'fetch_satellite',return_value=satellite) as fetch_map, \
                 patch.object(demo.app,'fetch_commute') as fetch_commute:
                demo.main()
                fetch_commute.assert_not_called()
                self.assertEqual(fetch_weather.call_count,int(live))
                self.assertEqual(fetch_map.call_count,int(live))
                with Image.open(output) as image:
                    self.assertEqual(image.size,(960,672))
                    self.assertTrue(set(image.convert('RGB').get_flattened_data()) <= {demo.app.WHITE,demo.app.BLACK,demo.app.RED})

    def test_offline_demo_does_not_fetch(self): self.run_demo(False)
    def test_live_demo_fetches_only_public_weather(self): self.run_demo(True)
