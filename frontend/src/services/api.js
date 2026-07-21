const configuredApiBaseUrl = (
  import.meta.env.VITE_API_URL
  || import.meta.env.REACT_APP_API_URL
  || ''
).replace(/\/$/, '');

// In production the frontend and API share an origin through Nginx.  A local
// development override must never make a visitor's browser call itself.
const API_BASE_URL = (
  import.meta.env.PROD && /^https?:\/\/localhost(?::\d+)?$/.test(configuredApiBaseUrl)
    ? ''
    : (configuredApiBaseUrl || (import.meta.env.DEV ? 'http://localhost:8000' : ''))
);

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const UPLOAD_COMPLETION_TIMEOUT_MS = 20 * 60_000;
const UPLOAD_REVOKE_TIMEOUT_MS = 15_000;

export const apiPaths = {
  projects: '/api/v1/projects',
  project: (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}`,
  projectWorkspace: (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}/workspace`,
  projectScenes: (projectId) => `/api/v1/projects/${encodeURIComponent(projectId)}/scenes`,
  scene: (sceneId) => `/api/v1/scenes/${encodeURIComponent(sceneId)}`,
  sceneWorkspace: (sceneId) => `/api/v1/scenes/${encodeURIComponent(sceneId)}/workspace`,
  sceneEvidence: (sceneId) => `/api/v1/scenes/${encodeURIComponent(sceneId)}/evidence-record`,
  sceneReprocess: (sceneId) => `/api/v1/scenes/${encodeURIComponent(sceneId)}/reprocess`,
  sceneArtifacts: (sceneId) => `/api/v1/scenes/${encodeURIComponent(sceneId)}/artifacts`,
  artifactPreview: (artifactId) => `/api/v1/artifacts/${encodeURIComponent(artifactId)}/preview`,
  patch: (patchId) => `/api/v1/patches/${encodeURIComponent(patchId)}`,
  evidenceSearch: '/api/v1/search',
  projectConversations: (projectId, sceneId) => {
    const query = sceneId ? `?scene_id=${encodeURIComponent(sceneId)}` : '';
    return `/api/v1/projects/${encodeURIComponent(projectId)}/conversations${query}`;
  },
  conversations: '/api/v1/conversations',
  conversationMessages: (conversationId) => `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
  conversationStream: (conversationId) => `/api/v1/conversations/${encodeURIComponent(conversationId)}/stream`,
  uploadsInitiate: '/api/v1/uploads/initiate',
  uploadInitiation: (clientRequestId) => `/api/v1/uploads/initiation/${encodeURIComponent(clientRequestId)}`,
  uploadPlan: (planId) => `/api/v1/uploads/${encodeURIComponent(planId)}`,
  uploadPlanStatus: (planId) => `/api/v1/uploads/${encodeURIComponent(planId)}/status`,
  uploadPartsSign: (planId) => `/api/v1/uploads/${encodeURIComponent(planId)}/parts/sign`,
  uploadComplete: (planId) => `/api/v1/uploads/${encodeURIComponent(planId)}/complete`,
  job: (jobId) => `/api/v1/jobs/${encodeURIComponent(jobId)}`,
  jobEvents: (jobId, beforeId) => `/api/v1/jobs/${encodeURIComponent(jobId)}/events${beforeId ? `?before_id=${encodeURIComponent(beforeId)}` : ''}`,
  jobCancel: (jobId) => `/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`,
  sceneJobs: (sceneId) => `/api/v1/jobs/scenes/${encodeURIComponent(sceneId)}`,
};

export class ApiError extends Error {
  constructor(message, { status, payload } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export class ApiTimeoutError extends ApiError {
  constructor(timeoutMs) {
    super(`The request timed out after ${Math.ceil(timeoutMs / 1000)} seconds.`, { status: 408 });
    this.name = 'ApiTimeoutError';
    this.isTimeout = true;
    this.timeoutMs = timeoutMs;
  }
}

function createTimeoutSignal(callerSignal, timeoutMs) {
  const boundedTimeout = Number(timeoutMs);
  if (!Number.isFinite(boundedTimeout) || boundedTimeout <= 0) {
    return {
      signal: callerSignal,
      timedOut: () => false,
      dispose: () => undefined,
    };
  }

  const controller = new AbortController();
  let didTimeout = false;
  const abortFromCaller = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
  }
  const timer = globalThis.setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, boundedTimeout);

  return {
    signal: controller.signal,
    timedOut: () => didTimeout,
    dispose: () => {
      globalThis.clearTimeout(timer);
      callerSignal?.removeEventListener('abort', abortFromCaller);
    },
  };
}

async function readResponse(response) {
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return response.json();

  const text = await response.text();
  return text || null;
}

function errorMessage(payload, fallback) {
  if (typeof payload === 'string') return payload;
  if (Array.isArray(payload?.detail)) {
    const messages = payload.detail
      .map((item) => (typeof item === 'string' ? item : item?.msg))
      .filter(Boolean);
    if (messages.length) return messages.join(' ');
  }
  if (payload?.detail && typeof payload.detail === 'object' && typeof payload.detail.message === 'string') {
    return payload.detail.message;
  }
  if (payload?.detail) return typeof payload.detail === 'string' ? payload.detail : fallback;
  if (payload?.message) return payload.message;
  return fallback;
}

function unwrapCollection(payload, keys) {
  if (Array.isArray(payload)) return payload;
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function unwrapEntity(payload, keys) {
  for (const key of keys) {
    if (payload?.[key] && !Array.isArray(payload[key])) return payload[key];
  }
  return payload;
}

/**
 * A single FastAPI client. The access-token callback is intentionally lazy so
 * token refreshes are reflected in every request.
 */
export function createApiClient(getAccessToken) {
  async function request(path, options = {}) {
    const {
      headers: suppliedHeaders,
      signal: callerSignal,
      timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
      ...fetchOptions
    } = options;
    const token = await getAccessToken?.();
    if (!token) {
      throw new ApiError('An active session is required to call the product API.', { status: 401 });
    }
    const headers = new Headers(suppliedHeaders || {});

    headers.set('Accept', 'application/json');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (fetchOptions.body && !(fetchOptions.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }

    const timeout = createTimeoutSignal(callerSignal, timeoutMs);
    try {
      const response = await fetch(`${API_BASE_URL}${path}`, {
        ...fetchOptions,
        headers,
        signal: timeout.signal,
      });
      const payload = await readResponse(response);

      if (!response.ok) {
        throw new ApiError(errorMessage(payload, `Request failed (${response.status})`), {
          status: response.status,
          payload,
        });
      }

      return payload;
    } catch (error) {
      if (timeout.timedOut()) throw new ApiTimeoutError(timeoutMs);
      throw error;
    } finally {
      timeout.dispose();
    }
  }

  /**
   * M5 intentionally uses NDJSON only. Stream frames are handled immediately
   * and never stored in the browser cache; completed history is reloaded from
   * the authenticated API instead.
   */
  async function streamNdjson(path, input, { signal, onEvent } = {}) {
    const token = await getAccessToken?.();
    if (!token) {
      throw new ApiError('An active session is required to call the product API.', { status: 401 });
    }
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'POST',
      headers: {
        Accept: 'application/x-ndjson',
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(input),
      signal,
      cache: 'no-store',
    });
    if (!response.ok) {
      const payload = await readResponse(response);
      throw new ApiError(errorMessage(payload, `Request failed (${response.status})`), {
        status: response.status,
        payload,
      });
    }
    if (!response.body) throw new ApiError('The chat stream was unavailable.', { status: 503 });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const emitLine = (line) => {
      if (!line.trim()) return;
      try {
        const event = JSON.parse(line);
        if (event && typeof event.type === 'string') onEvent?.(event);
      } catch {
        // A malformed partial frame is ignored; the server persists the final
        // assistant message so a reload remains the authoritative recovery.
      }
    };
    try {
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        lines.forEach(emitLine);
        if (done) break;
      }
      buffer += decoder.decode();
      emitLine(buffer);
    } finally {
      reader.releaseLock();
    }
  }

  return {
    request,
    projects: {
      async list() {
        return unwrapCollection(await request(apiPaths.projects), ['projects', 'items', 'data']);
      },
      async create(input) {
        return unwrapEntity(await request(apiPaths.projects, { method: 'POST', body: JSON.stringify(input) }), ['project', 'data']);
      },
      async get(projectId) {
        return unwrapEntity(await request(apiPaths.project(projectId)), ['project', 'data']);
      },
      async update(projectId, input) {
        return unwrapEntity(await request(apiPaths.project(projectId), { method: 'PATCH', body: JSON.stringify(input) }), ['project', 'data']);
      },
      remove(projectId) {
        return request(apiPaths.project(projectId), { method: 'DELETE' });
      },
      async workspace(projectId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.projectWorkspace(projectId), { signal }), ['workspace', 'data']);
      },
    },
    scenes: {
      async list(projectId) {
        return unwrapCollection(await request(apiPaths.projectScenes(projectId)), ['scenes', 'items', 'data']);
      },
      async create(projectId, input) {
        return unwrapEntity(await request(apiPaths.projectScenes(projectId), { method: 'POST', body: JSON.stringify(input) }), ['scene', 'data']);
      },
      async get(sceneId) {
        return unwrapEntity(await request(apiPaths.scene(sceneId)), ['scene', 'data']);
      },
      async update(sceneId, input) {
        return unwrapEntity(await request(apiPaths.scene(sceneId), { method: 'PATCH', body: JSON.stringify(input) }), ['scene', 'data']);
      },
      remove(sceneId) {
        return request(apiPaths.scene(sceneId), { method: 'DELETE' });
      },
      async workspace(sceneId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.sceneWorkspace(sceneId), { signal }), ['workspace', 'data']);
      },
      async evidence(sceneId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.sceneEvidence(sceneId), { signal }), ['evidence', 'data']);
      },
      async reprocess(sceneId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.sceneReprocess(sceneId), { method: 'POST', signal }), ['job', 'data']);
      },
    },
    uploads: {
      initiate(input, { signal, timeoutMs } = {}) {
        return request(apiPaths.uploadsInitiate, {
          method: 'POST',
          body: JSON.stringify(input),
          signal,
          timeoutMs,
        });
      },
      getInitiation(clientRequestId, { signal, timeoutMs } = {}) {
        return request(apiPaths.uploadInitiation(clientRequestId), { signal, timeoutMs });
      },
      getStatus(planId, { signal, timeoutMs } = {}) {
        return request(apiPaths.uploadPlanStatus(planId), { signal, timeoutMs });
      },
      signParts(planId, input, { signal, timeoutMs } = {}) {
        return request(apiPaths.uploadPartsSign(planId), {
          method: 'POST',
          body: JSON.stringify(input),
          signal,
          timeoutMs,
        });
      },
      complete(planId, input, { signal, timeoutMs = UPLOAD_COMPLETION_TIMEOUT_MS } = {}) {
        return request(apiPaths.uploadComplete(planId), {
          method: 'POST',
          body: JSON.stringify(input),
          signal,
          timeoutMs,
        });
      },
      abort(planId, { signal, timeoutMs = UPLOAD_REVOKE_TIMEOUT_MS } = {}) {
        return request(apiPaths.uploadPlan(planId), { method: 'DELETE', signal, timeoutMs });
      },
    },
    artifacts: {
      async listForScene(sceneId, { signal, timeoutMs } = {}) {
        return unwrapCollection(await request(apiPaths.sceneArtifacts(sceneId), { signal, timeoutMs }), ['artifacts', 'items', 'data']);
      },
      async preview(artifactId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.artifactPreview(artifactId), { method: 'POST', signal }), ['preview', 'data']);
      },
    },
    jobs: {
      async get(jobId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.job(jobId), { signal }), ['job', 'data']);
      },
      async listForScene(sceneId, { signal } = {}) {
        return unwrapCollection(await request(apiPaths.sceneJobs(sceneId), { signal }), ['jobs', 'items', 'data']);
      },
      async events(jobId, { beforeId, signal } = {}) {
        return unwrapEntity(await request(apiPaths.jobEvents(jobId, beforeId), { signal }), ['events', 'data']);
      },
      async cancel(jobId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.jobCancel(jobId), { method: 'POST', signal }), ['job', 'data']);
      },
    },
    patches: {
      async get(patchId, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.patch(patchId), { signal }), ['patch', 'data']);
      },
    },
    evidence: {
      search(input, { signal } = {}) {
        return request(apiPaths.evidenceSearch, { method: 'POST', body: JSON.stringify(input), signal });
      },
    },
    conversations: {
      async create(input, { signal } = {}) {
        return unwrapEntity(await request(apiPaths.conversations, { method: 'POST', body: JSON.stringify(input), signal }), ['conversation', 'data']);
      },
      async list(projectId, { sceneId, signal } = {}) {
        return unwrapCollection(await request(apiPaths.projectConversations(projectId, sceneId), { signal }), ['conversations', 'items', 'data']);
      },
      async messages(conversationId, { signal } = {}) {
        return unwrapCollection(await request(apiPaths.conversationMessages(conversationId), { signal }), ['items', 'messages', 'data']);
      },
      stream(conversationId, input, options = {}) {
        return streamNdjson(apiPaths.conversationStream(conversationId), input, options);
      },
    },
  };
}
