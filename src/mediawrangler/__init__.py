"""MediaWrangler: compare and sync media and watch state across media apps."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mediawrangler")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+dev"
