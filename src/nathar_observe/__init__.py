"""Nathar Observe: portable context retrieval and skill routing."""
from .config import configure

__version__ = "0.1.0"


def observe(query, **kwargs):
    from .router import observe as route
    return route(query, **kwargs)


__all__ = ["configure", "observe", "__version__"]
