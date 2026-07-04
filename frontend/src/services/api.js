const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export async function fetchProjects() {
  const response = await fetch(`${API_URL}/api/v1/projects`);
  return response.json();
}

export async function uploadSARFile(formData) {
  const response = await fetch(`${API_URL}/api/v1/ingestion/upload`, {
    method: 'POST',
    body: formData,
  });
  return response.json();
}