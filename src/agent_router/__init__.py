from importlib import metadata

try:
    __version__ = metadata.version("open-agent-router")
except metadata.PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"
