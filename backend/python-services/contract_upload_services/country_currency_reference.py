"""
country_currency_reference.py
──────────────────────────────
Static ISO country -> currency reference data for the `currency_country_consistency`
validation template. This is PUBLIC, universal reference data (ISO 3166-1 country
codes/names mapped to their current ISO 4217 legal-tender currency, sourced from the
Unicode CLDR territory-currency dataset) — the same category as the `_US_TERRITORIES`
/ `_COUNTRY_ALIASES` tables in rule_normalizer.py and `uszips_reference.py` — NOT
contract, carrier or MGA-specific data. Generated ONCE offline from `babel`+
`pycountry` (dev-only tools; NOT a runtime dependency of this codebase — the data is
frozen here as a plain Python literal so validation never needs those packages
installed).

COUNTRY_CURRENCY maps alpha-2 code -> (alpha-3 code, name, official_name|None,
(currency_code, ...)). A tuple of >1 currency code (7 countries, e.g. Panama
USD+PAB, Haiti HTG+USD) means BOTH are current legal tender — either is valid.

Exports:
  COUNTRY_CURRENCY   — alpha-2 -> (alpha3, name, official, currencies) as above;
                       the canonical source data (also used to regenerate the
                       bundled Parquet below if the source data ever changes)
  VALID_ALPHA2       — frozenset of every alpha-2 code

The data ships (pre-denormalized) as a bundled Parquet file
(`data/country_currency.parquet`, ~6 KB, 937 rows: alias, currency — every
recognized country spelling — alpha-2, alpha-3, name, official name — paired
with every currency it accepts), loaded into DuckDB as a REAL table
(`country_currency`) by `load_reference_table` below — mirroring
`uszips_reference.py`'s pattern exactly, so `_b_currency_country_consistency`
does a short JOIN/EXISTS against a real table instead of embedding ~900 CASE
branches inline in every compiled query."""

COUNTRY_CURRENCY = {
    'AD': ('AND', 'ANDORRA', 'PRINCIPALITY OF ANDORRA', ('EUR',)),
    'AE': ('ARE', 'UNITED ARAB EMIRATES', None, ('AED',)),
    'AF': ('AFG', 'AFGHANISTAN', 'ISLAMIC REPUBLIC OF AFGHANISTAN', ('AFN',)),
    'AG': ('ATG', 'ANTIGUA AND BARBUDA', None, ('XCD',)),
    'AI': ('AIA', 'ANGUILLA', None, ('XCD',)),
    'AL': ('ALB', 'ALBANIA', 'REPUBLIC OF ALBANIA', ('ALL',)),
    'AM': ('ARM', 'ARMENIA', 'REPUBLIC OF ARMENIA', ('AMD',)),
    'AO': ('AGO', 'ANGOLA', 'REPUBLIC OF ANGOLA', ('AOA',)),
    'AR': ('ARG', 'ARGENTINA', 'ARGENTINE REPUBLIC', ('ARS',)),
    'AS': ('ASM', 'AMERICAN SAMOA', None, ('USD',)),
    'AT': ('AUT', 'AUSTRIA', 'REPUBLIC OF AUSTRIA', ('EUR',)),
    'AU': ('AUS', 'AUSTRALIA', None, ('AUD',)),
    'AW': ('ABW', 'ARUBA', None, ('AWG',)),
    'AX': ('ALA', 'ÅLAND ISLANDS', None, ('EUR',)),
    'AZ': ('AZE', 'AZERBAIJAN', 'REPUBLIC OF AZERBAIJAN', ('AZN',)),
    'BA': ('BIH', 'BOSNIA AND HERZEGOVINA', 'REPUBLIC OF BOSNIA AND HERZEGOVINA', ('BAM',)),
    'BB': ('BRB', 'BARBADOS', None, ('BBD',)),
    'BD': ('BGD', 'BANGLADESH', "PEOPLE'S REPUBLIC OF BANGLADESH", ('BDT',)),
    'BE': ('BEL', 'BELGIUM', 'KINGDOM OF BELGIUM', ('EUR',)),
    'BF': ('BFA', 'BURKINA FASO', None, ('XOF',)),
    'BG': ('BGR', 'BULGARIA', 'REPUBLIC OF BULGARIA', ('BGN',)),
    'BH': ('BHR', 'BAHRAIN', 'KINGDOM OF BAHRAIN', ('BHD',)),
    'BI': ('BDI', 'BURUNDI', 'REPUBLIC OF BURUNDI', ('BIF',)),
    'BJ': ('BEN', 'BENIN', 'REPUBLIC OF BENIN', ('XOF',)),
    'BL': ('BLM', 'SAINT BARTHÉLEMY', None, ('EUR',)),
    'BM': ('BMU', 'BERMUDA', None, ('BMD',)),
    'BN': ('BRN', 'BRUNEI DARUSSALAM', None, ('BND',)),
    'BO': ('BOL', 'BOLIVIA, PLURINATIONAL STATE OF', 'PLURINATIONAL STATE OF BOLIVIA', ('BOB',)),
    'BQ': ('BES', 'BONAIRE, SINT EUSTATIUS AND SABA', 'BONAIRE, SINT EUSTATIUS AND SABA', ('USD',)),
    'BR': ('BRA', 'BRAZIL', 'FEDERATIVE REPUBLIC OF BRAZIL', ('BRL',)),
    'BS': ('BHS', 'BAHAMAS', 'COMMONWEALTH OF THE BAHAMAS', ('BSD',)),
    'BT': ('BTN', 'BHUTAN', 'KINGDOM OF BHUTAN', ('BTN', 'INR')),
    'BV': ('BVT', 'BOUVET ISLAND', None, ('NOK',)),
    'BW': ('BWA', 'BOTSWANA', 'REPUBLIC OF BOTSWANA', ('BWP',)),
    'BY': ('BLR', 'BELARUS', 'REPUBLIC OF BELARUS', ('BYN',)),
    'BZ': ('BLZ', 'BELIZE', None, ('BZD',)),
    'CA': ('CAN', 'CANADA', None, ('CAD',)),
    'CC': ('CCK', 'COCOS (KEELING) ISLANDS', None, ('AUD',)),
    'CD': ('COD', 'CONGO, THE DEMOCRATIC REPUBLIC OF THE', None, ('CDF',)),
    'CF': ('CAF', 'CENTRAL AFRICAN REPUBLIC', None, ('XAF',)),
    'CG': ('COG', 'CONGO', 'REPUBLIC OF THE CONGO', ('XAF',)),
    'CH': ('CHE', 'SWITZERLAND', 'SWISS CONFEDERATION', ('CHF',)),
    'CI': ('CIV', "CÔTE D'IVOIRE", "REPUBLIC OF CÔTE D'IVOIRE", ('XOF',)),
    'CK': ('COK', 'COOK ISLANDS', None, ('NZD',)),
    'CL': ('CHL', 'CHILE', 'REPUBLIC OF CHILE', ('CLP',)),
    'CM': ('CMR', 'CAMEROON', 'REPUBLIC OF CAMEROON', ('XAF',)),
    'CN': ('CHN', 'CHINA', "PEOPLE'S REPUBLIC OF CHINA", ('CNY',)),
    'CO': ('COL', 'COLOMBIA', 'REPUBLIC OF COLOMBIA', ('COP',)),
    'CR': ('CRI', 'COSTA RICA', 'REPUBLIC OF COSTA RICA', ('CRC',)),
    'CU': ('CUB', 'CUBA', 'REPUBLIC OF CUBA', ('CUP',)),
    'CV': ('CPV', 'CABO VERDE', 'REPUBLIC OF CABO VERDE', ('CVE',)),
    'CW': ('CUW', 'CURAÇAO', 'CURAÇAO', ('XCG',)),
    'CX': ('CXR', 'CHRISTMAS ISLAND', None, ('AUD',)),
    'CY': ('CYP', 'CYPRUS', 'REPUBLIC OF CYPRUS', ('EUR',)),
    'CZ': ('CZE', 'CZECHIA', 'CZECH REPUBLIC', ('CZK',)),
    'DE': ('DEU', 'GERMANY', 'FEDERAL REPUBLIC OF GERMANY', ('EUR',)),
    'DJ': ('DJI', 'DJIBOUTI', 'REPUBLIC OF DJIBOUTI', ('DJF',)),
    'DK': ('DNK', 'DENMARK', 'KINGDOM OF DENMARK', ('DKK',)),
    'DM': ('DMA', 'DOMINICA', 'COMMONWEALTH OF DOMINICA', ('XCD',)),
    'DO': ('DOM', 'DOMINICAN REPUBLIC', None, ('DOP',)),
    'DZ': ('DZA', 'ALGERIA', "PEOPLE'S DEMOCRATIC REPUBLIC OF ALGERIA", ('DZD',)),
    'EC': ('ECU', 'ECUADOR', 'REPUBLIC OF ECUADOR', ('USD',)),
    'EE': ('EST', 'ESTONIA', 'REPUBLIC OF ESTONIA', ('EUR',)),
    'EG': ('EGY', 'EGYPT', 'ARAB REPUBLIC OF EGYPT', ('EGP',)),
    'EH': ('ESH', 'WESTERN SAHARA', None, ('MAD',)),
    'ER': ('ERI', 'ERITREA', 'THE STATE OF ERITREA', ('ERN',)),
    'ES': ('ESP', 'SPAIN', 'KINGDOM OF SPAIN', ('EUR',)),
    'ET': ('ETH', 'ETHIOPIA', 'FEDERAL DEMOCRATIC REPUBLIC OF ETHIOPIA', ('ETB',)),
    'FI': ('FIN', 'FINLAND', 'REPUBLIC OF FINLAND', ('EUR',)),
    'FJ': ('FJI', 'FIJI', 'REPUBLIC OF FIJI', ('FJD',)),
    'FK': ('FLK', 'FALKLAND ISLANDS (MALVINAS)', None, ('FKP',)),
    'FM': ('FSM', 'MICRONESIA, FEDERATED STATES OF', 'FEDERATED STATES OF MICRONESIA', ('USD',)),
    'FO': ('FRO', 'FAROE ISLANDS', None, ('DKK',)),
    'FR': ('FRA', 'FRANCE', 'FRENCH REPUBLIC', ('EUR',)),
    'GA': ('GAB', 'GABON', 'GABONESE REPUBLIC', ('XAF',)),
    'GB': ('GBR', 'UNITED KINGDOM', 'UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND', ('GBP',)),
    'GD': ('GRD', 'GRENADA', None, ('XCD',)),
    'GE': ('GEO', 'GEORGIA', None, ('GEL',)),
    'GF': ('GUF', 'FRENCH GUIANA', None, ('EUR',)),
    'GG': ('GGY', 'GUERNSEY', None, ('GBP',)),
    'GH': ('GHA', 'GHANA', 'REPUBLIC OF GHANA', ('GHS',)),
    'GI': ('GIB', 'GIBRALTAR', None, ('GIP',)),
    'GL': ('GRL', 'GREENLAND', None, ('DKK',)),
    'GM': ('GMB', 'GAMBIA', 'REPUBLIC OF THE GAMBIA', ('GMD',)),
    'GN': ('GIN', 'GUINEA', 'REPUBLIC OF GUINEA', ('GNF',)),
    'GP': ('GLP', 'GUADELOUPE', None, ('EUR',)),
    'GQ': ('GNQ', 'EQUATORIAL GUINEA', 'REPUBLIC OF EQUATORIAL GUINEA', ('XAF',)),
    'GR': ('GRC', 'GREECE', 'HELLENIC REPUBLIC', ('EUR',)),
    'GS': ('SGS', 'SOUTH GEORGIA AND THE SOUTH SANDWICH ISLANDS', None, ('GBP',)),
    'GT': ('GTM', 'GUATEMALA', 'REPUBLIC OF GUATEMALA', ('GTQ',)),
    'GU': ('GUM', 'GUAM', None, ('USD',)),
    'GW': ('GNB', 'GUINEA-BISSAU', 'REPUBLIC OF GUINEA-BISSAU', ('XOF',)),
    'GY': ('GUY', 'GUYANA', 'REPUBLIC OF GUYANA', ('GYD',)),
    'HK': ('HKG', 'HONG KONG', 'HONG KONG SPECIAL ADMINISTRATIVE REGION OF CHINA', ('HKD',)),
    'HM': ('HMD', 'HEARD ISLAND AND MCDONALD ISLANDS', None, ('AUD',)),
    'HN': ('HND', 'HONDURAS', 'REPUBLIC OF HONDURAS', ('HNL',)),
    'HR': ('HRV', 'CROATIA', 'REPUBLIC OF CROATIA', ('EUR',)),
    'HT': ('HTI', 'HAITI', 'REPUBLIC OF HAITI', ('HTG', 'USD')),
    'HU': ('HUN', 'HUNGARY', 'HUNGARY', ('HUF',)),
    'ID': ('IDN', 'INDONESIA', 'REPUBLIC OF INDONESIA', ('IDR',)),
    'IE': ('IRL', 'IRELAND', None, ('EUR',)),
    'IL': ('ISR', 'ISRAEL', 'STATE OF ISRAEL', ('ILS',)),
    'IM': ('IMN', 'ISLE OF MAN', None, ('GBP',)),
    'IN': ('IND', 'INDIA', 'REPUBLIC OF INDIA', ('INR',)),
    'IO': ('IOT', 'BRITISH INDIAN OCEAN TERRITORY', None, ('USD',)),
    'IQ': ('IRQ', 'IRAQ', 'REPUBLIC OF IRAQ', ('IQD',)),
    'IR': ('IRN', 'IRAN, ISLAMIC REPUBLIC OF', 'ISLAMIC REPUBLIC OF IRAN', ('IRR',)),
    'IS': ('ISL', 'ICELAND', 'REPUBLIC OF ICELAND', ('ISK',)),
    'IT': ('ITA', 'ITALY', 'ITALIAN REPUBLIC', ('EUR',)),
    'JE': ('JEY', 'JERSEY', None, ('GBP',)),
    'JM': ('JAM', 'JAMAICA', None, ('JMD',)),
    'JO': ('JOR', 'JORDAN', 'HASHEMITE KINGDOM OF JORDAN', ('JOD',)),
    'JP': ('JPN', 'JAPAN', None, ('JPY',)),
    'KE': ('KEN', 'KENYA', 'REPUBLIC OF KENYA', ('KES',)),
    'KG': ('KGZ', 'KYRGYZSTAN', 'KYRGYZ REPUBLIC', ('KGS',)),
    'KH': ('KHM', 'CAMBODIA', 'KINGDOM OF CAMBODIA', ('KHR',)),
    'KI': ('KIR', 'KIRIBATI', 'REPUBLIC OF KIRIBATI', ('AUD',)),
    'KM': ('COM', 'COMOROS', 'UNION OF THE COMOROS', ('KMF',)),
    'KN': ('KNA', 'SAINT KITTS AND NEVIS', None, ('XCD',)),
    'KP': ('PRK', "KOREA, DEMOCRATIC PEOPLE'S REPUBLIC OF", "DEMOCRATIC PEOPLE'S REPUBLIC OF KOREA", ('KPW',)),
    'KR': ('KOR', 'KOREA, REPUBLIC OF', None, ('KRW',)),
    'KW': ('KWT', 'KUWAIT', 'STATE OF KUWAIT', ('KWD',)),
    'KY': ('CYM', 'CAYMAN ISLANDS', None, ('KYD',)),
    'KZ': ('KAZ', 'KAZAKHSTAN', 'REPUBLIC OF KAZAKHSTAN', ('KZT',)),
    'LA': ('LAO', "LAO PEOPLE'S DEMOCRATIC REPUBLIC", None, ('LAK',)),
    'LB': ('LBN', 'LEBANON', 'LEBANESE REPUBLIC', ('LBP',)),
    'LC': ('LCA', 'SAINT LUCIA', None, ('XCD',)),
    'LI': ('LIE', 'LIECHTENSTEIN', 'PRINCIPALITY OF LIECHTENSTEIN', ('CHF',)),
    'LK': ('LKA', 'SRI LANKA', 'DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA', ('LKR',)),
    'LR': ('LBR', 'LIBERIA', 'REPUBLIC OF LIBERIA', ('LRD',)),
    'LS': ('LSO', 'LESOTHO', 'KINGDOM OF LESOTHO', ('LSL', 'ZAR')),
    'LT': ('LTU', 'LITHUANIA', 'REPUBLIC OF LITHUANIA', ('EUR',)),
    'LU': ('LUX', 'LUXEMBOURG', 'GRAND DUCHY OF LUXEMBOURG', ('EUR',)),
    'LV': ('LVA', 'LATVIA', 'REPUBLIC OF LATVIA', ('EUR',)),
    'LY': ('LBY', 'LIBYA', 'LIBYA', ('LYD',)),
    'MA': ('MAR', 'MOROCCO', 'KINGDOM OF MOROCCO', ('MAD',)),
    'MC': ('MCO', 'MONACO', 'PRINCIPALITY OF MONACO', ('EUR',)),
    'MD': ('MDA', 'MOLDOVA, REPUBLIC OF', 'REPUBLIC OF MOLDOVA', ('MDL',)),
    'ME': ('MNE', 'MONTENEGRO', 'MONTENEGRO', ('EUR',)),
    'MF': ('MAF', 'SAINT MARTIN (FRENCH PART)', None, ('EUR',)),
    'MG': ('MDG', 'MADAGASCAR', 'REPUBLIC OF MADAGASCAR', ('MGA',)),
    'MH': ('MHL', 'MARSHALL ISLANDS', 'REPUBLIC OF THE MARSHALL ISLANDS', ('USD',)),
    'MK': ('MKD', 'NORTH MACEDONIA', 'REPUBLIC OF NORTH MACEDONIA', ('MKD',)),
    'ML': ('MLI', 'MALI', 'REPUBLIC OF MALI', ('XOF',)),
    'MM': ('MMR', 'MYANMAR', 'REPUBLIC OF MYANMAR', ('MMK',)),
    'MN': ('MNG', 'MONGOLIA', None, ('MNT',)),
    'MO': ('MAC', 'MACAO', 'MACAO SPECIAL ADMINISTRATIVE REGION OF CHINA', ('MOP',)),
    'MP': ('MNP', 'NORTHERN MARIANA ISLANDS', 'COMMONWEALTH OF THE NORTHERN MARIANA ISLANDS', ('USD',)),
    'MQ': ('MTQ', 'MARTINIQUE', None, ('EUR',)),
    'MR': ('MRT', 'MAURITANIA', 'ISLAMIC REPUBLIC OF MAURITANIA', ('MRU',)),
    'MS': ('MSR', 'MONTSERRAT', None, ('XCD',)),
    'MT': ('MLT', 'MALTA', 'REPUBLIC OF MALTA', ('EUR',)),
    'MU': ('MUS', 'MAURITIUS', 'REPUBLIC OF MAURITIUS', ('MUR',)),
    'MV': ('MDV', 'MALDIVES', 'REPUBLIC OF MALDIVES', ('MVR',)),
    'MW': ('MWI', 'MALAWI', 'REPUBLIC OF MALAWI', ('MWK',)),
    'MX': ('MEX', 'MEXICO', 'UNITED MEXICAN STATES', ('MXN',)),
    'MY': ('MYS', 'MALAYSIA', None, ('MYR',)),
    'MZ': ('MOZ', 'MOZAMBIQUE', 'REPUBLIC OF MOZAMBIQUE', ('MZN',)),
    'NA': ('NAM', 'NAMIBIA', 'REPUBLIC OF NAMIBIA', ('NAD', 'ZAR')),
    'NC': ('NCL', 'NEW CALEDONIA', None, ('XPF',)),
    'NE': ('NER', 'NIGER', 'REPUBLIC OF THE NIGER', ('XOF',)),
    'NF': ('NFK', 'NORFOLK ISLAND', None, ('AUD',)),
    'NG': ('NGA', 'NIGERIA', 'FEDERAL REPUBLIC OF NIGERIA', ('NGN',)),
    'NI': ('NIC', 'NICARAGUA', 'REPUBLIC OF NICARAGUA', ('NIO',)),
    'NL': ('NLD', 'NETHERLANDS', 'KINGDOM OF THE NETHERLANDS', ('EUR',)),
    'NO': ('NOR', 'NORWAY', 'KINGDOM OF NORWAY', ('NOK',)),
    'NP': ('NPL', 'NEPAL', 'FEDERAL DEMOCRATIC REPUBLIC OF NEPAL', ('NPR',)),
    'NR': ('NRU', 'NAURU', 'REPUBLIC OF NAURU', ('AUD',)),
    'NU': ('NIU', 'NIUE', 'NIUE', ('NZD',)),
    'NZ': ('NZL', 'NEW ZEALAND', None, ('NZD',)),
    'OM': ('OMN', 'OMAN', 'SULTANATE OF OMAN', ('OMR',)),
    'PA': ('PAN', 'PANAMA', 'REPUBLIC OF PANAMA', ('PAB', 'USD')),
    'PE': ('PER', 'PERU', 'REPUBLIC OF PERU', ('PEN',)),
    'PF': ('PYF', 'FRENCH POLYNESIA', None, ('XPF',)),
    'PG': ('PNG', 'PAPUA NEW GUINEA', 'INDEPENDENT STATE OF PAPUA NEW GUINEA', ('PGK',)),
    'PH': ('PHL', 'PHILIPPINES', 'REPUBLIC OF THE PHILIPPINES', ('PHP',)),
    'PK': ('PAK', 'PAKISTAN', 'ISLAMIC REPUBLIC OF PAKISTAN', ('PKR',)),
    'PL': ('POL', 'POLAND', 'REPUBLIC OF POLAND', ('PLN',)),
    'PM': ('SPM', 'SAINT PIERRE AND MIQUELON', None, ('EUR',)),
    'PN': ('PCN', 'PITCAIRN', None, ('NZD',)),
    'PR': ('PRI', 'PUERTO RICO', None, ('USD',)),
    'PS': ('PSE', 'PALESTINE, STATE OF', 'THE STATE OF PALESTINE', ('ILS', 'JOD')),
    'PT': ('PRT', 'PORTUGAL', 'PORTUGUESE REPUBLIC', ('EUR',)),
    'PW': ('PLW', 'PALAU', 'REPUBLIC OF PALAU', ('USD',)),
    'PY': ('PRY', 'PARAGUAY', 'REPUBLIC OF PARAGUAY', ('PYG',)),
    'QA': ('QAT', 'QATAR', 'STATE OF QATAR', ('QAR',)),
    'RE': ('REU', 'RÉUNION', None, ('EUR',)),
    'RO': ('ROU', 'ROMANIA', None, ('RON',)),
    'RS': ('SRB', 'SERBIA', 'REPUBLIC OF SERBIA', ('RSD',)),
    'RU': ('RUS', 'RUSSIAN FEDERATION', None, ('RUB',)),
    'RW': ('RWA', 'RWANDA', 'RWANDESE REPUBLIC', ('RWF',)),
    'SA': ('SAU', 'SAUDI ARABIA', 'KINGDOM OF SAUDI ARABIA', ('SAR',)),
    'SB': ('SLB', 'SOLOMON ISLANDS', None, ('SBD',)),
    'SC': ('SYC', 'SEYCHELLES', 'REPUBLIC OF SEYCHELLES', ('SCR',)),
    'SD': ('SDN', 'SUDAN', 'REPUBLIC OF THE SUDAN', ('SDG',)),
    'SE': ('SWE', 'SWEDEN', 'KINGDOM OF SWEDEN', ('SEK',)),
    'SG': ('SGP', 'SINGAPORE', 'REPUBLIC OF SINGAPORE', ('SGD',)),
    'SH': ('SHN', 'SAINT HELENA, ASCENSION AND TRISTAN DA CUNHA', None, ('SHP',)),
    'SI': ('SVN', 'SLOVENIA', 'REPUBLIC OF SLOVENIA', ('EUR',)),
    'SJ': ('SJM', 'SVALBARD AND JAN MAYEN', None, ('NOK',)),
    'SK': ('SVK', 'SLOVAKIA', 'SLOVAK REPUBLIC', ('EUR',)),
    'SL': ('SLE', 'SIERRA LEONE', 'REPUBLIC OF SIERRA LEONE', ('SLE',)),
    'SM': ('SMR', 'SAN MARINO', 'REPUBLIC OF SAN MARINO', ('EUR',)),
    'SN': ('SEN', 'SENEGAL', 'REPUBLIC OF SENEGAL', ('XOF',)),
    'SO': ('SOM', 'SOMALIA', 'FEDERAL REPUBLIC OF SOMALIA', ('SOS',)),
    'SR': ('SUR', 'SURINAME', 'REPUBLIC OF SURINAME', ('SRD',)),
    'SS': ('SSD', 'SOUTH SUDAN', 'REPUBLIC OF SOUTH SUDAN', ('SSP',)),
    'ST': ('STP', 'SAO TOME AND PRINCIPE', 'DEMOCRATIC REPUBLIC OF SAO TOME AND PRINCIPE', ('STN',)),
    'SV': ('SLV', 'EL SALVADOR', 'REPUBLIC OF EL SALVADOR', ('USD',)),
    'SX': ('SXM', 'SINT MAARTEN (DUTCH PART)', 'SINT MAARTEN (DUTCH PART)', ('XCG',)),
    'SY': ('SYR', 'SYRIAN ARAB REPUBLIC', None, ('SYP',)),
    'SZ': ('SWZ', 'ESWATINI', 'KINGDOM OF ESWATINI', ('SZL',)),
    'TC': ('TCA', 'TURKS AND CAICOS ISLANDS', None, ('USD',)),
    'TD': ('TCD', 'CHAD', 'REPUBLIC OF CHAD', ('XAF',)),
    'TF': ('ATF', 'FRENCH SOUTHERN TERRITORIES', None, ('EUR',)),
    'TG': ('TGO', 'TOGO', 'TOGOLESE REPUBLIC', ('XOF',)),
    'TH': ('THA', 'THAILAND', 'KINGDOM OF THAILAND', ('THB',)),
    'TJ': ('TJK', 'TAJIKISTAN', 'REPUBLIC OF TAJIKISTAN', ('TJS',)),
    'TK': ('TKL', 'TOKELAU', None, ('NZD',)),
    'TL': ('TLS', 'TIMOR-LESTE', 'DEMOCRATIC REPUBLIC OF TIMOR-LESTE', ('USD',)),
    'TM': ('TKM', 'TURKMENISTAN', None, ('TMT',)),
    'TN': ('TUN', 'TUNISIA', 'REPUBLIC OF TUNISIA', ('TND',)),
    'TO': ('TON', 'TONGA', 'KINGDOM OF TONGA', ('TOP',)),
    'TR': ('TUR', 'TÜRKIYE', 'REPUBLIC OF TÜRKIYE', ('TRY',)),
    'TT': ('TTO', 'TRINIDAD AND TOBAGO', 'REPUBLIC OF TRINIDAD AND TOBAGO', ('TTD',)),
    'TV': ('TUV', 'TUVALU', None, ('AUD',)),
    'TW': ('TWN', 'TAIWAN, PROVINCE OF CHINA', 'TAIWAN, PROVINCE OF CHINA', ('TWD',)),
    'TZ': ('TZA', 'TANZANIA, UNITED REPUBLIC OF', 'UNITED REPUBLIC OF TANZANIA', ('TZS',)),
    'UA': ('UKR', 'UKRAINE', None, ('UAH',)),
    'UG': ('UGA', 'UGANDA', 'REPUBLIC OF UGANDA', ('UGX',)),
    'UM': ('UMI', 'UNITED STATES MINOR OUTLYING ISLANDS', None, ('USD',)),
    'US': ('USA', 'UNITED STATES', 'UNITED STATES OF AMERICA', ('USD',)),
    'UY': ('URY', 'URUGUAY', 'EASTERN REPUBLIC OF URUGUAY', ('UYU',)),
    'UZ': ('UZB', 'UZBEKISTAN', 'REPUBLIC OF UZBEKISTAN', ('UZS',)),
    'VA': ('VAT', 'HOLY SEE (VATICAN CITY STATE)', None, ('EUR',)),
    'VC': ('VCT', 'SAINT VINCENT AND THE GRENADINES', None, ('XCD',)),
    'VE': ('VEN', 'VENEZUELA, BOLIVARIAN REPUBLIC OF', 'BOLIVARIAN REPUBLIC OF VENEZUELA', ('VES',)),
    'VG': ('VGB', 'VIRGIN ISLANDS, BRITISH', 'BRITISH VIRGIN ISLANDS', ('USD',)),
    'VI': ('VIR', 'VIRGIN ISLANDS, U.S.', 'VIRGIN ISLANDS OF THE UNITED STATES', ('USD',)),
    'VN': ('VNM', 'VIET NAM', 'SOCIALIST REPUBLIC OF VIET NAM', ('VND',)),
    'VU': ('VUT', 'VANUATU', 'REPUBLIC OF VANUATU', ('VUV',)),
    'WF': ('WLF', 'WALLIS AND FUTUNA', None, ('XPF',)),
    'WS': ('WSM', 'SAMOA', 'INDEPENDENT STATE OF SAMOA', ('WST',)),
    'YE': ('YEM', 'YEMEN', 'REPUBLIC OF YEMEN', ('YER',)),
    'YT': ('MYT', 'MAYOTTE', None, ('EUR',)),
    'ZA': ('ZAF', 'SOUTH AFRICA', 'REPUBLIC OF SOUTH AFRICA', ('ZAR',)),
    'ZM': ('ZMB', 'ZAMBIA', 'REPUBLIC OF ZAMBIA', ('ZMW',)),
    'ZW': ('ZWE', 'ZIMBABWE', 'REPUBLIC OF ZIMBABWE', ('USD', 'ZWG')),
}

VALID_ALPHA2 = frozenset(COUNTRY_CURRENCY.keys())


# =====================================================================
# DuckDB table loader — mirrors uszips_reference.py exactly.
# =====================================================================

import os as _os

_DATA_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data")
COUNTRY_CURRENCY_PARQUET = _os.path.join(_DATA_DIR, "country_currency.parquet")

# The DuckDB table name the compiled currency rule references. Kept as a module
# constant so the compiler and the loader can never drift apart.
COUNTRY_CURRENCY_TABLE = "country_currency"

# Column-name tokens marking a currency column and a country column. A
# currency_country_consistency rule can only exist when the template carries
# BOTH, so the reference table loads only then — most validations skip it.
_CURRENCY_TOKENS = ("currenc",)   # matches "currency"/"currencies", word-boundary
                                  # filtering (vs "occurrence") is done by the caller
_COUNTRY_TOKENS = ("countr",)


def _sheet_column_names(records_by_sheet, schema_cols):
    """Every column name across the loaded schema (template columns first, then
    any keys present in the data), plus the set of (stripped, lowered) sheet
    names — used for the has-both-columns gate and the table-name collision guard.
    Identical helper to uszips_reference._sheet_column_names (kept local — a tiny
    duplicate is simpler than a cross-module import for two one-line loops)."""
    cols, sheets = [], set()
    for sh, cs in (schema_cols or {}).items():
        sheets.add(str(sh).strip().lower())
        cols.extend(cs or [])
    for block in (records_by_sheet or []):
        sheets.add(str(block.get("sheet") or "").strip().lower())
        for rec in (block.get("records") or []):
            cols.extend(rec.keys())
    return cols, sheets


def has_currency_and_country_columns(records_by_sheet, schema_cols) -> bool:
    """True when the loaded schema has BOTH a currency-looking column and a
    country-looking column — the only case a currency_country_consistency rule
    (and hence this reference table) is needed. Word-boundary safety against
    "occurrence" is handled by the derive-pairing regex at rule-generation time;
    this gate only needs to be a cheap, conservative superset."""
    cols, _ = _sheet_column_names(records_by_sheet, schema_cols)
    lowered = [str(c).lower() for c in cols]
    has_cur = any(tok in c for c in lowered for tok in _CURRENCY_TOKENS)
    has_country = any(tok in c for c in lowered for tok in _COUNTRY_TOKENS)
    return has_cur and has_country


# --- Self-heal: rebuild the bundled Parquet from its SOURCE when it is missing ---
# Here the SOURCE OF TRUTH is the COUNTRY_CURRENCY dict in THIS module, so the
# rebuild is fully self-contained (no external file needed) — if the cached Parquet
# is deleted (e.g. an untracked data/ dir wiped by `git clean`) it is regenerated on
# demand. Best-effort: a read-only filesystem just falls back to the existing
# "skip → not-validated" behaviour, so a rebuild attempt can never break a run.


def build_parquet(dest: str = COUNTRY_CURRENCY_PARQUET) -> bool:
    """(Re)build the country_currency Parquet by DENORMALIZING the COUNTRY_CURRENCY
    dict — every country spelling (alpha-2 / alpha-3 / name / official name) paired
    with every currency it accepts — applying the SAME normalization the compiled
    rule joins on (UPPER alias, UPPER currency). Writes ATOMICALLY (temp file +
    os.replace) so a concurrent run never reads a half-written file. Returns True on
    success, False on write failure. Nothing is hardcoded beyond the existing
    reference dict. Also usable as a deterministic build step (see
    scripts/build_reference_data.py)."""
    tmp = None
    try:
        import duckdb   # lazy: only when a rebuild is needed
        import uuid
        rows, seen = [], set()
        for a2, val in COUNTRY_CURRENCY.items():
            alpha3, name, official, currencies = val
            for al in [a2, alpha3, name] + ([official] if official else []):
                al = str(al).strip().upper()
                if not al:
                    continue
                for cur in currencies:
                    cur = str(cur).strip().upper()
                    if not cur:
                        continue
                    k = (al, cur)
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append(k)
        if not rows:
            return False
        _os.makedirs(_os.path.dirname(dest), exist_ok=True)
        # Unique per CALL (pid + uuid), so two concurrent rebuilds — even two
        # threads in one process — never share a temp file; os.replace is atomic.
        tmp = f"{dest}.tmp.{_os.getpid()}.{uuid.uuid4().hex[:8]}"
        con = duckdb.connect(":memory:")
        try:
            con.execute("CREATE TABLE _cc(alias VARCHAR, currency VARCHAR)")
            con.executemany("INSERT INTO _cc VALUES (?,?)", rows)
            con.execute(f"COPY _cc TO '{tmp.replace(chr(39), chr(39) * 2)}' "
                        f"(FORMAT PARQUET)")
        finally:
            con.close()
        _os.replace(tmp, dest)
        print(f"[country_currency] rebuilt reference parquet ({len(rows)} rows)")
        return True
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[country_currency] auto-rebuild failed ({exc}); "
              f"currency checks will report not-validated.")
        try:
            if tmp and _os.path.exists(tmp):
                _os.remove(tmp)
        except Exception:
            pass
        return False


def load_reference_table(con, records_by_sheet, schema_cols) -> list:
    """Create the `country_currency` reference table in `con` from the bundled
    Parquet, when the loaded schema has both a currency and a country column and
    no sheet already claims the table name.

    Idempotent and best-effort: any failure (missing file, older DuckDB) is
    swallowed and returns [] — a currency rule then simply reports "not
    validated" rather than crashing the whole run. Returns the list of reference
    table names created (for logging). MUST be called before the sandbox lock
    (like uszips_reference.load_reference_tables)."""
    created = []
    try:
        if not has_currency_and_country_columns(records_by_sheet, schema_cols):
            return created
        if not _os.path.exists(COUNTRY_CURRENCY_PARQUET):
            build_parquet()          # self-heal a deleted bundle (best-effort)
        if not _os.path.exists(COUNTRY_CURRENCY_PARQUET):
            return created
        _, sheets = _sheet_column_names(records_by_sheet, schema_cols)
        if COUNTRY_CURRENCY_TABLE.lower() in sheets:
            return created  # a real sheet already owns this name; don't clobber
        path = COUNTRY_CURRENCY_PARQUET.replace("'", "''")
        con.execute(
            f'CREATE TABLE {COUNTRY_CURRENCY_TABLE} AS '
            f"SELECT CAST(alias AS VARCHAR) AS alias, "
            f"CAST(currency AS VARCHAR) AS currency "
            f"FROM read_parquet('{path}')"
        )
        created.append(COUNTRY_CURRENCY_TABLE)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[country_currency] reference table not loaded ({exc}); "
              f"currency checks will report not-validated.")
    return created
