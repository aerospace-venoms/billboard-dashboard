"""Approximate single-cell 4.2 V LiPo gauge; not a manufacturer-specific characterization.

The badge senses regulator input, not an isolated cell terminal. External USB
power masks the cell voltage. Percentages are estimates, not a coulomb counter.
"""
import math

# Provisional low-load curve, in millivolts. Adjust after measurements on this pack.
CURVE = ((3300, 0), (3500, 5), (3650, 10), (3730, 20), (3770, 30),
         (3790, 40), (3820, 50), (3870, 60), (3920, 70), (3980, 80),
         (4080, 90), (4200, 100))


def estimate(sample, now, max_age=7200):
    result = {'state': 'unknown', 'percent': None, 'low': False,
              'supply_mv': None, 'sampled_at': None, 'capacity_mah': 1800}
    if not isinstance(sample, dict):
        return result
    mv, received = sample.get('supply_mv'), sample.get('sampled_at')
    if not isinstance(received, (int, float)) or not math.isfinite(received):
        return result
    result['sampled_at'] = received
    if now-received > max_age or received > now+60:
        result['state'] = 'stale'
        return result
    if not isinstance(mv, int) or isinstance(mv, bool) or not 2500 <= mv <= 6000:
        return result
    result['supply_mv'] = mv
    if mv > 4350:
        result['state'] = 'usb'  # External supply inferred, not charge completion.
        return result
    if mv > 4250:
        return result  # Ambiguous supply / outside normal cell voltage range.
    percent = 100 if mv >= CURVE[-1][0] else 0
    for (v0, p0), (v1, p1) in zip(CURVE, CURVE[1:]):
        if v0 <= mv < v1:
            percent = round(p0 + (mv-v0)*(p1-p0)/(v1-v0))
            break
    result.update(state='battery', percent=percent, low=percent <= 20)
    return result


def parse_header(value):
    if value == 'unknown':
        return None
    if value is None or not value.isascii() or not value.isdigit() or len(value) != 4:
        raise ValueError('invalid supply voltage')
    mv = int(value)
    if not 2500 <= mv <= 6000:
        raise ValueError('supply voltage out of range')
    return mv
