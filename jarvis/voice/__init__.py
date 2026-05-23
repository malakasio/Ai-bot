"""Voice module - re-exports from voice/ directory."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from voice.pipeline import *
except ImportError:
    pass

__all__ = []
