# Third-party notices

## Display driver

`firmware/components/billboard_epd` is vendored from
https://github.com/Hack-a-Day/2026-Billboard_Badge at commit
`5945518ef0eb2ec0d1161942b9408eea51f10a3a`, component version 0.1.0-rc.1.
The driver retains its BSD-2-Clause license, copyright and Pervasive Displays
adaptation notices. See the LICENSE in that directory. The dashboard's Unlicense
does not replace those notices. No original badge firmware dump is distributed.

## Geographic outlines

`assets/land.geojson` is Natural Earth's worldwide 1:110m land dataset, obtained
from https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_land.geojson.
It is public-domain map data, not a selection based on a user's location.
Natural Earth terms: https://www.naturalearthdata.com/about/terms-of-use/.
The dataset is bundled for reproducibility; it is not fetched at runtime.

## External services and dependencies

- Open-Meteo forecast data: https://open-meteo.com/, attribution CC BY 4.0.
  Its free hosted service has usage terms; review them for your intended use.
- Aviation Weather Center / NOAA: https://aviationweather.gov/. This project uses
  public website imagery endpoints; availability and endpoint stability are not
  guaranteed. The bundled demo map is fictional, not downloaded NOAA imagery.
- TomTom Routing API: https://developer.tomtom.com/. Each installation needs its
  own account/key and must follow its applicable terms and usage limits.
- Pillow, Python, DejaVu fonts, nginx, Squid, Debian and ESP-IDF retain their
  respective upstream licenses. Container package managers install these
  dependencies; their license files are not replaced by the project license.

The included `docs/demo.png` is generated solely by `scripts/demo.py` using
fictional weather/travel values and the public Natural Earth coastline. Its
reference point is the Pasadena civic center near City Hall, approximately
34.1478, -118.1436. Public location references:
https://www.cityofpasadena.net/contact/ and
https://ungegn.un.org/dashboard/cities/details?id=353.
No live dashboard capture or private address is distributed.
