"""Canonical nationality labels and flag image codes for race results."""
import re
import unicodedata


# PCS flags use ISO alpha-2 codes. FirstCycling and PCM CDB data use country
# names or cycling/IOC codes, so normalize the common codes to one label.
_COUNTRIES = {
    'au': ('Australia', 'AUS'), 'at': ('Austria', 'AUT'),
    'be': ('Belgium', 'BEL'), 'br': ('Brazil', 'BRA'),
    'ca': ('Canada', 'CAN'), 'co': ('Colombia', 'COL'),
    'hr': ('Croatia', 'CRO'), 'cz': ('Czech Republic', 'CZE'),
    'dk': ('Denmark', 'DEN'), 'ee': ('Estonia', 'EST'),
    'fi': ('Finland', 'FIN'), 'fr': ('France', 'FRA'),
    'de': ('Germany', 'GER'), 'gb': ('United Kingdom', 'GBR'),
    'gr': ('Greece', 'GRE'), 'hu': ('Hungary', 'HUN'),
    'ie': ('Ireland', 'IRL'), 'il': ('Israel', 'ISR'),
    'it': ('Italy', 'ITA'), 'jp': ('Japan', 'JPN'),
    'kz': ('Kazakhstan', 'KAZ'), 'lv': ('Latvia', 'LAT'),
    'lt': ('Lithuania', 'LTU'), 'lu': ('Luxembourg', 'LUX'),
    'mx': ('Mexico', 'MEX'), 'nl': ('Netherlands', 'NED'),
    'nz': ('New Zealand', 'NZL'), 'no': ('Norway', 'NOR'),
    'pl': ('Poland', 'POL'), 'pt': ('Portugal', 'POR'),
    'ro': ('Romania', 'ROU'), 'ru': ('Russia', 'RUS'),
    'rs': ('Serbia', 'SRB'), 'sk': ('Slovakia', 'SVK'),
    'si': ('Slovenia', 'SLO'), 'za': ('South Africa', 'RSA'),
    'kr': ('South Korea', 'KOR'), 'es': ('Spain', 'ESP'),
    'se': ('Sweden', 'SWE'), 'ch': ('Switzerland', 'SUI'),
    'tr': ('Turkey', 'TUR'), 'ua': ('Ukraine', 'UKR'),
    'us': ('United States', 'USA'), 'uy': ('Uruguay', 'URU'),
    'ar': ('Argentina', 'ARG'), 'bo': ('Bolivia', 'BOL'),
    'cl': ('Chile', 'CHI'), 'cr': ('Costa Rica', 'CRC'),
    'ec': ('Ecuador', 'ECU'), 'er': ('Eritrea', 'ERI'),
    'et': ('Ethiopia', 'ETH'), 'ge': ('Georgia', 'GEO'),
    'is': ('Iceland', 'ISL'), 'ke': ('Kenya', 'KEN'),
    'ma': ('Morocco', 'MAR'), 'pe': ('Peru', 'PER'),
    'ph': ('Philippines', 'PHI'),
    'tw': ('Taiwan', 'TPE'), 'th': ('Thailand', 'THA'),
    'uz': ('Uzbekistan', 'UZB'), 've': ('Venezuela', 'VEN'),
    'cn': ('China', 'CHN'), 'by': ('Belarus', 'BLR'),
    'bg': ('Bulgaria', 'BUL'), 'ba': ('Bosnia and Herzegovina', 'BIH'),
    'cy': ('Cyprus', 'CYP'), 'mt': ('Malta', 'MLT'),
    'md': ('Moldova', 'MDA'), 'me': ('Montenegro', 'MNE'),
    'mk': ('North Macedonia', 'MKD'), 'ad': ('Andorra', 'AND'),
    'al': ('Albania', 'ALB'), 'am': ('Armenia', 'ARM'),
    'az': ('Azerbaijan', 'AZE'), 'do': ('Dominican Republic', 'DOM'),
    'jm': ('Jamaica', 'JAM'), 'pr': ('Puerto Rico', 'PUR'),
    'sg': ('Singapore', 'SGP'), 'id': ('Indonesia', 'INA'),
    'in': ('India', 'IND'),
    'my': ('Malaysia', 'MAS'), 'vn': ('Vietnam', 'VIE'),
    'hk': ('Hong Kong', 'HKG'), 'qa': ('Qatar', 'QAT'),
    'bh': ('Bahrain', 'BRN'), 'ae': ('United Arab Emirates', 'UAE'),
    'rw': ('Rwanda', 'RWA'), 'dz': ('Algeria', 'ALG'),
    'tn': ('Tunisia', 'TUN'), 'sn': ('Senegal', 'SEN'),
    'ng': ('Nigeria', 'NGR'), 'eg': ('Egypt', 'EGY'),
    'ir': ('Iran', 'IRI'), 'kg': ('Kyrgyzstan', 'KGZ'),
    'mn': ('Mongolia', 'MGL'), 'np': ('Nepal', 'NEP'),
    'pk': ('Pakistan', 'PAK'), 'sa': ('Saudi Arabia', 'KSA'),
    'lk': ('Sri Lanka', 'SRI'), 'zw': ('Zimbabwe', 'ZIM'),
    'na': ('Namibia', 'NAM'), 'mu': ('Mauritius', 'MRI'),
    'pa': ('Panama', 'PAN'),
    'py': ('Paraguay', 'PAR'), 'sv': ('El Salvador', 'ESA'),
    'gt': ('Guatemala', 'GUA'), 'hn': ('Honduras', 'HON'),
    'ni': ('Nicaragua', 'NCA'), 'tt': ('Trinidad and Tobago', 'TTO'),
    'bs': ('Bahamas', 'BAH'), 'bb': ('Barbados', 'BAR'),
    'cu': ('Cuba', 'CUB'), 'ht': ('Haiti', 'HAI'),
    'lc': ('Saint Lucia', 'LCA'), 'ky': ('Cayman Islands', 'CAY'),
    'fo': ('Faroe Islands', 'FRO'), 'xk': ('Kosovo', 'KOS'),
    'sm': ('San Marino', 'SMR'), 'va': ('Vatican City', 'VAT'),
    'gd': ('Grenada', 'GRN'), 'sy': ('Syria', 'SYR'),
    'af': ('Afghanistan', 'AFG'), 'ao': ('Angola', 'ANG'),
    'bd': ('Bangladesh', 'BAN'), 'bz': ('Belize', 'BIZ'),
    'bj': ('Benin', 'BEN'), 'bt': ('Bhutan', 'BHU'),
    'bw': ('Botswana', 'BOT'), 'bf': ('Burkina Faso', 'BUR'),
    'bi': ('Burundi', 'BDI'), 'kh': ('Cambodia', 'CAM'),
    'cm': ('Cameroon', 'CMR'), 'cf': ('Central African Republic', 'CAF'),
    'td': ('Chad', 'CHA'), 'cg': ('Republic of the Congo', 'CGO'),
    'cd': ('Democratic Republic of the Congo', 'COD'),
    'ci': ('Ivory Coast', 'CIV'), 'dj': ('Djibouti', 'DJI'),
    'ga': ('Gabon', 'GAB'), 'gm': ('Gambia', 'GAM'),
    'gh': ('Ghana', 'GHA'), 'gn': ('Guinea', 'GUI'),
    'gw': ('Guinea-Bissau', 'GBS'), 'gq': ('Equatorial Guinea', 'GEQ'),
    'gy': ('Guyana', 'GUY'), 'iq': ('Iraq', 'IRQ'),
    'jo': ('Jordan', 'JOR'), 'kw': ('Kuwait', 'KUW'),
    'lb': ('Lebanon', 'LBN'), 'ly': ('Libya', 'LBA'),
    'mg': ('Madagascar', 'MAD'), 'mw': ('Malawi', 'MAW'),
    'ml': ('Mali', 'MLI'), 'mr': ('Mauritania', 'MTN'),
    'mz': ('Mozambique', 'MOZ'), 'ne': ('Niger', 'NIG'),
    'om': ('Oman', 'OMA'), 'pg': ('Papua New Guinea', 'PNG'),
    'st': ('Sao Tome and Principe', 'STP'), 'sl': ('Sierra Leone', 'SLE'),
    'so': ('Somalia', 'SOM'), 'sd': ('Sudan', 'SUD'),
    'ss': ('South Sudan', 'SSD'), 'tg': ('Togo', 'TOG'),
    'tz': ('Tanzania', 'TAN'), 'ug': ('Uganda', 'UGA'),
    'zm': ('Zambia', 'ZAM'), 'ye': ('Yemen', 'YEM'),
    'ag': ('Antigua and Barbuda', 'ANT'), 'aw': ('Aruba', 'ARU'),
    'dm': ('Dominica', 'DMA'), 'fj': ('Fiji', 'FIJ'),
    'kn': ('Saint Kitts and Nevis', 'SKN'),
    'vc': ('Saint Vincent and the Grenadines', 'VIN'),
    'sx': ('Sint Maarten', 'SXM'), 'cv': ('Cape Verde', 'CPV'),
    'sr': ('Suriname', 'SUR'), 'cw': ('Curaçao', 'CUW'),
    'bm': ('Bermuda', 'BER'), 'mq': ('Martinique', 'MTQ'),
    'gp': ('Guadeloupe', 'GLP'), 'nc': ('New Caledonia', 'NCL'),
    're': ('Réunion', 'REU'), 'gu': ('Guam', 'GUM'),
    'vi': ('U.S. Virgin Islands', 'ISV'), 'vg': ('British Virgin Islands', 'IVB'),
    'je': ('Jersey', 'JER'), 'im': ('Isle of Man', 'IMN'),
    'to': ('Tonga', 'TGA'), 'ws': ('Samoa', 'SAM'),
    'bn': ('Brunei', 'BRU'), 'kp': ('North Korea', 'PRK'),
    'la': ('Laos', 'LAO'), 'mo': ('Macao', 'MAC'),
    'mm': ('Myanmar', 'MYA'), 'ps': ('Palestine', 'PLE'),
}

_ALIASES = {
    'great britain': 'United Kingdom', 'uk': 'United Kingdom',
    'gb': 'United Kingdom', 'united kingdom of great britain and northern ireland': 'United Kingdom',
    'usa': 'United States', 'united states of america': 'United States',
    'czechia': 'Czech Republic', 'turkiye': 'Turkey', 'türkiye': 'Turkey',
    'holland': 'Netherlands', 'south korea': 'South Korea',
    'trinidad tobago': 'Trinidad and Tobago',
    'trinidad tobago west indies': 'Trinidad and Tobago',
    'st vincent grenadines': 'Saint Vincent and the Grenadines',
    'st vincent and grenadines': 'Saint Vincent and the Grenadines',
    'saint vincent grenadines': 'Saint Vincent and the Grenadines',
    'cape verde islands': 'Cape Verde', 'cabo verde': 'Cape Verde',
    'dutch caribbean': 'Sint Maarten', 'saint maarten': 'Sint Maarten',
    'curacao': 'Curaçao', 'reunion': 'Réunion',
    'u s virgin islands': 'U.S. Virgin Islands', 'us virgin islands': 'U.S. Virgin Islands',
}


def _key(value):
    normalized = unicodedata.normalize('NFKD', str(value or ''))
    normalized = ''.join(char for char in normalized if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', normalized.casefold()))


_CODE_TO_COUNTRY = {}
_COUNTRY_TO_CODE = {}
_COUNTRY_LABELS = {}
for _alpha2, (_country, _ioc) in _COUNTRIES.items():
    _CODE_TO_COUNTRY[_alpha2] = _country
    _CODE_TO_COUNTRY[_ioc.casefold()] = _country
    _COUNTRY_TO_CODE[_key(_country)] = _alpha2
    _COUNTRY_LABELS[_key(_country)] = _country


def canonical_nationality(value):
    """Return a stable English nationality label where the source is known."""
    value = str(value or '').strip()
    if not value:
        return None
    key = _key(value)
    if key in _CODE_TO_COUNTRY:
        return _CODE_TO_COUNTRY[key]
    if key in _ALIASES:
        return _ALIASES[key]
    if key in _COUNTRY_LABELS:
        return _COUNTRY_LABELS[key]
    # Normalize punctuation differences such as CDB's ``United-Kingdom``.
    return ' '.join(part.capitalize() for part in key.split())


def nationality_flag_code(value):
    """Return the ISO alpha-2 code used to request a country flag image."""
    label = canonical_nationality(value)
    if not label:
        return None
    return _COUNTRY_TO_CODE.get(_key(label))


