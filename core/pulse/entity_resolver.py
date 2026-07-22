"""DEPRECATED: Use core.lib.entity_detector.detect_entities() instead.

This file is kept for backward compatibility. All logic has been moved to
core.lib.entity_detector which provides deterministic entity detection
without LLM or prompt bias.
"""

import warnings
from typing import Tuple, Optional

from core.lib.entity_detector import resolve_org_and_project


def resolve_entities_from_text(text: str
                              ) -> Tuple[Optional[str], Optional[int], str]:
    """DEPRECATED: Use core.lib.entity_detector.detect_entities() instead.

    Returns (organization_id, project_id, reason_log_string).
    Delegates to entity_detector.resolve_org_and_project().
    """
    warnings.warn(
        "resolve_entities_from_text is deprecated. "
        "Use core.lib.entity_detector.detect_entities() instead.",
        DeprecationWarning, stacklevel=2,
    )
    return resolve_org_and_project(text)
