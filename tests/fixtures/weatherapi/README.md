# WeatherAPI fixtures

- `synthetic_minimal.json` is handcrafted synthetic data; its provenance states
  that no API or Bronze fetch supplied it.
- `bronze_success_*_derived_real.json` contains real weather values read from
  existing Bronze. Only hourly lists are reduced to their first two entries
  per day; JSON formatting also changed. No other values were corrected.
- Adjacent `.provenance.json` files distinguish the original blob hash from the
  derived fixture hash and record exact reduction, source, run and expectations.
- `real_bronze_profiles.json` describes the complete original responses before
  reduction (types, nulls, cardinalities, hashes and source paths).

The source query `Londres` actually resolved to Argentina on both captures.
It must be rejected by the city registry. Do not relabel it as London.
The full responses contain 3 days ×24 hours per city; the derived fixtures keep
3 days ×2 hours, so they deliberately do not test original full-hour coverage.
