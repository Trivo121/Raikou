"""Which projects are optical.

There is no project-type column. An optical project carries a marker in its
description, which is how the optical page's default project has always been
found, and how the dashboard's SAR-or-optical choice records the user's pick.
Standard library only.
"""

from __future__ import annotations

from typing import Any

PROJECT_NAME = "Optical land cover"
PROJECT_MARKER = "raikou:optical-workspace"
PROJECT_DESCRIPTION = f"Sentinel-2 land-cover runs from the Optical page. ({PROJECT_MARKER})"


def is_optical_project(project: Any) -> bool:
    """True for a project row (or API object) whose description carries the optical marker."""
    description = project.get("description") if isinstance(project, dict) else None
    return isinstance(description, str) and PROJECT_MARKER in description
