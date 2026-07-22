"""Backward-compatible import location for project API schemas."""

from app.schemas.projects import ProjectBase, ProjectCreate, ProjectRead, ProjectUpdate

# Existing imports can continue to use ``Project`` while new code uses the more
# explicit ``ProjectRead`` name.
Project = ProjectRead

__all__ = ["Project", "ProjectBase", "ProjectCreate", "ProjectRead", "ProjectUpdate"]
