// Project types. There is no type column: an optical project carries a marker in
// its description (the backend finds the optical workspace the same way, see
// backend/app/services/optical/project.py), and every other project is SAR.

export const OPTICAL_MARKER = 'raikou:optical-workspace';
export const OPTICAL_DESCRIPTION = `Sentinel-2 land-cover runs. (${OPTICAL_MARKER})`;

export function isOpticalProject(project) {
  return String(project?.description || '').includes(OPTICAL_MARKER);
}

// Where a project opens: optical projects get the optical page scoped to
// themselves, SAR projects get the SAR workspace.
export function projectPath(project) {
  return isOpticalProject(project) ? `/optical?project=${project.id}` : `/projects/${project.id}`;
}
