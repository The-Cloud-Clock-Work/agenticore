"""Agenticore — Claude Code runner and orchestrator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agenticore")
except PackageNotFoundError:
    __version__ = "dev"
