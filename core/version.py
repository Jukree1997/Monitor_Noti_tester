"""Single source of truth for app version + product name.

Bumped manually on each release (see RELEASING.md). Used by:
- MainWindow window title
- About dialog (Help menu)
- UpdateChecker for comparison against the latest GitHub release tag
"""
from __future__ import annotations


# ======================================
# -------- VERSION --------
# ======================================

__version__ = "1.0.1"
__product_name__ = "Baksters Notification Runner"
