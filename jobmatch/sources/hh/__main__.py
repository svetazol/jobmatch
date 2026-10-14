"""`python -m jobmatch.sources.hh [country...]`; not `-m ...hh.areas`, which warns as already imported."""
from .areas import main

raise SystemExit(main())
