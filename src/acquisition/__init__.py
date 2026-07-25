"""Source adapters (architecture Section 5.1).

Each adapter implements the `SourceAdapter` protocol (base.py) and knows
how to fetch exactly one external data source into a GeoDataFrame.
Downstream ETL/join/scoring code never imports a specific adapter class --
it only depends on the protocol (FR-1.2, FR-1.3, NFR-7).
"""
