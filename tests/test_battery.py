import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from battery import estimate, parse_header
import dashboard as app


class BatteryTests(unittest.TestCase):
    def state(self, mv):
        return estimate({'supply_mv': mv, 'sampled_at': 1000}, 1000)

    def test_curve_monotonic_and_low_threshold(self):
        percentages = [self.state(v)['percent'] for v in range(3000, 4251)]
        self.assertEqual(percentages, sorted(percentages))
        self.assertEqual(self.state(3300)['percent'], 0)
        self.assertEqual(self.state(4200)['percent'], 100)
        self.assertEqual(self.state(3730)['percent'], 20)
        self.assertTrue(self.state(3730)['low'])
        self.assertFalse(self.state(3734)['low'])

    def test_usb_unknown_and_stale_do_not_claim_charge(self):
        usb = self.state(4900)
        self.assertEqual(usb['state'], 'usb')
        self.assertIsNone(usb['percent'])
        self.assertIsNone(self.state(4300)['percent'])
        self.assertIsNone(self.state(None)['percent'])
        stale = estimate({'supply_mv': 3730, 'sampled_at': 1000}, 9000)
        self.assertEqual(stale['state'], 'stale')
        self.assertIsNone(stale['percent'])
        self.assertFalse(stale['low'])

    def test_header_validation(self):
        self.assertEqual(parse_header('3820'), 3820)
        self.assertIsNone(parse_header('unknown'))
        for invalid in ['-1', '99999999', '3820.0', 'NaN', '24', '6001', '３８２０']:
            with self.assertRaises(ValueError):
                parse_header(invalid)

    def test_red_icon_at_twenty_percent_and_black_above(self):
        now = 1794409000
        for mv, color in [(3730, app.RED), (3734, app.BLACK), (4900, app.BLACK)]:
            image = app.render(None, None, {'status':'needs_key'}, {}, now,
                               {'supply_mv':mv, 'sampled_at':now})
            self.assertEqual(image.getpixel((370,22)), color)
            app.encode(image)  # Strict three-color palette validation.
