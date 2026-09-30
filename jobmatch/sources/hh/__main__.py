"""`python -m jobmatch.sources.hh [country...]` -- what a country name means.

Its own module because `-m ...hh.areas` warns: the package's `__init__` has
already imported it.
"""
from .areas import main

raise SystemExit(main())
