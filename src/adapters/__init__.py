"""Messaging adapters: Telegram, Discord, LINE, and extensible interface.

Adapters connect the Kuro engine to messaging platforms.
Each adapter implements the BaseAdapter interface.
"""

from src.adapters.base import BaseAdapter
from src.adapters.manager import AdapterManager

__all__ = ["BaseAdapter", "AdapterManager"]
