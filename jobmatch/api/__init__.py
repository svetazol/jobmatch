"""The HTTP API. A thin caller of the same core `cli.py` calls.

Nothing here queries the database directly — `repository.py` stays the only
module writing SQL, and these modules shape what it returns.
"""
