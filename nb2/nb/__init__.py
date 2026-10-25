"""Package nb.

The ultimate tool to automate custom telegram message forwarding.
"""

try:
    from importlib.metadata import version
    __version__ = version(__package__)
except Exception:
    __version__ = "2.0.0"
