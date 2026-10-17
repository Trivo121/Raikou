"""How a project is recognised as optical. Standard library only."""

from __future__ import annotations

import pytest

from app.services.optical.project import PROJECT_DESCRIPTION, PROJECT_MARKER, PROJECT_NAME, is_optical_project


def test_the_description_the_backend_writes_marks_a_project_as_optical():
    assert is_optical_project({"name": PROJECT_NAME, "description": PROJECT_DESCRIPTION})


def test_the_description_the_dashboard_writes_marks_a_project_as_optical():
    # frontend/src/utils/projects.js builds this string for a project created with the optical choice.
    assert is_optical_project({"name": "Visakhapatnam coast study", "description": f"Sentinel-2 land-cover runs. ({PROJECT_MARKER})"})


@pytest.mark.parametrize("project", [
    {"name": "Rotterdam port review", "description": ""},
    {"name": "Rotterdam port review", "description": "SAR change review for the port"},
    {"name": "No description at all"},
    {"name": "Null description", "description": None},
    {"description": 7},
    None,
    "raikou:optical-workspace",
])
def test_everything_else_is_a_sar_project(project):
    assert not is_optical_project(project)
