"""idlixdownloader2 - Video downloader for IDLIX"""

__version__ = "2.0.0"

from . import tui
from .downloader import MajorPlayDownloader

__all__ = ["MajorPlayDownloader", "tui"]
