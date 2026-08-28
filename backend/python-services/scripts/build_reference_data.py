"""(Re)build the bundled reference-data Parquet files used by the data-quality
validation rules:

    contract_upload_services/data/uszips.parquet            (US ZIP -> state)
    contract_upload_services/data/intl_postal.parquet       (CA/GB code -> region)
    contract_upload_services/data/country_currency.parquet  (country -> currency)

All three are pre-built caches of an in-repo SOURCE of truth — `uszips.xlsx`, the
`zipcodes.ca.csv` / `zipcodes.gb.csv` pair, and the `COUNTRY_CURRENCY` dict — so
this script never invents data; it just regenerates the caches. The loaders
(`uszips_reference` / `intl_postal_reference` / `country_currency_reference`)
already self-heal at runtime if a file is missing; run this to build them
explicitly, e.g. as a deploy/CI step or after pulling a fresh checkout.

Usage:
    cd backend/python-services
    python -m scripts.build_reference_data
"""
from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)


def main() -> int:
    from contract_upload_services import uszips_reference as usz
    from contract_upload_services import intl_postal_reference as intl
    from contract_upload_services import country_currency_reference as ccy

    ok_cc = ccy.build_parquet()    # source: the in-module COUNTRY_CURRENCY dict
    ok_zip = usz.build_parquet()   # source: uszips.xlsx (a plain file or a dir wrapping it)
    ok_intl = intl.build_parquet()  # source: data/zipcodes.ca.csv + zipcodes.gb.csv

    print(f"country_currency.parquet: {'built' if ok_cc else 'FAILED'} -> "
          f"{ccy.COUNTRY_CURRENCY_PARQUET}")
    if ok_zip:
        print(f"uszips.parquet: built -> {usz.USZIPS_PARQUET}")
    else:
        print("uszips.parquet: FAILED (source uszips.xlsx not found or unreadable) "
              f"-> {usz.USZIPS_PARQUET}")
    if ok_intl:
        print(f"intl_postal.parquet: built -> {intl.INTL_POSTAL_PARQUET}")
    else:
        print("intl_postal.parquet: FAILED (source zipcodes.ca.csv / zipcodes.gb.csv "
              f"not found or unreadable) -> {intl.INTL_POSTAL_PARQUET}")
    # country_currency is fully self-contained, so its failure is the only hard error;
    # a missing uszips.xlsx / CSV source is a soft failure (the committed parquet is used).
    return 0 if ok_cc else 1


if __name__ == "__main__":
    raise SystemExit(main())
