const FULL_FILE_CHECKSUM_LIMIT_BYTES = 64 * 1024 * 1024;
const SIGN_BATCH_SIZE = 24;
const MAX_PARALLEL_PART_UPLOADS = 3;
const DIRECT_PART_TIMEOUT_MS = 10 * 60_000;

export class UploadCancelledError extends Error {
  constructor(message = 'Upload cancelled.') {
    super(message);
    this.name = 'UploadCancelledError';
  }
}

export class UploadTimeoutError extends Error {
  constructor(timeoutMs = DIRECT_PART_TIMEOUT_MS) {
    super(`The direct upload part timed out after ${Math.ceil(timeoutMs / 1000)} seconds.`);
    this.name = 'UploadTimeoutError';
    this.isTimeout = true;
    this.timeoutMs = timeoutMs;
  }
}

export function isUploadCancelled(error) {
  return error instanceof UploadCancelledError || error?.name === 'AbortError';
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw new UploadCancelledError();
}

function createLinkedAbortController(parentSignal) {
  const controller = new AbortController();
  const abortFromParent = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) {
    abortFromParent();
  } else {
    parentSignal?.addEventListener('abort', abortFromParent, { once: true });
  }
  return {
    controller,
    dispose: () => parentSignal?.removeEventListener('abort', abortFromParent),
  };
}

function requireWebCrypto() {
  if (!globalThis.crypto?.subtle) {
    throw new Error('This browser needs Web Crypto in a secure context to verify uploads.');
  }
  return globalThis.crypto.subtle;
}

function base64FromBuffer(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** Hash a Blob without sending it through FastAPI. */
export async function checksumSha256Base64(blob, { signal } = {}) {
  throwIfAborted(signal);
  const data = await blob.arrayBuffer();
  throwIfAborted(signal);
  const digest = await requireWebCrypto().digest('SHA-256', data);
  throwIfAborted(signal);
  return base64FromBuffer(digest);
}

/**
 * Small files carry a full SHA-256 in the initiation request. Large files do
 * not get read twice in the browser; their raw full-object checksum is
 * verified server-side after multipart completion. Every actual upload part
 * is still hashed with Web Crypto before it is signed and sent.
 */
export async function prepareUploadDescriptors(entries, { signal, onProgress } = {}) {
  const fullChecksumBytes = entries
    .filter(({ file }) => file.size <= FULL_FILE_CHECKSUM_LIMIT_BYTES)
    .reduce((total, { file }) => total + file.size, 0);
  let completedBytes = 0;

  const descriptors = [];
  for (const entry of entries) {
    throwIfAborted(signal);
    let checksumSha256;
    if (entry.file.size <= FULL_FILE_CHECKSUM_LIMIT_BYTES) {
      checksumSha256 = await checksumSha256Base64(entry.file, { signal });
      completedBytes += entry.file.size;
      onProgress?.({ completedBytes, totalBytes: fullChecksumBytes, filename: entry.filename });
    }

    descriptors.push({
      filename: entry.filename,
      content_type: entry.content_type,
      size_bytes: entry.size_bytes,
      ...(checksumSha256 ? { checksum_sha256: checksumSha256 } : {}),
    });
  }
  return descriptors;
}

function integer(value) {
  return Number.isSafeInteger(value) ? value : Number(value);
}

function normalisePlan(plan) {
  const partSizeBytes = integer(plan?.part_size_bytes);
  if (!plan?.id || !Number.isSafeInteger(partSizeBytes) || partSizeBytes < 1 || !Array.isArray(plan.files)) {
    throw new Error('The upload plan returned by the API is incomplete. Please try again.');
  }
  return {
    ...plan,
    part_size_bytes: partSizeBytes,
    multipart_checksum_mode: plan.multipart_checksum_mode || 'server_verified',
  };
}

function partCountFor(planFile, file, partSizeBytes) {
  const returned = integer(planFile?.part_count);
  const expected = Math.max(1, Math.ceil(file.size / partSizeBytes));
  if (!Number.isSafeInteger(returned) || returned < 1 || returned !== expected) {
    throw new Error(`The upload plan has an invalid part count for "${file.name}".`);
  }
  return returned;
}

function partSizeFor(planFile, fallbackPartSizeBytes) {
  const returned = integer(planFile?.part_size_bytes);
  if (returned === undefined || Number.isNaN(returned)) return fallbackPartSizeBytes;
  if (!Number.isSafeInteger(returned) || returned < 1) {
    throw new Error(`The upload plan has an invalid part size for "${planFile?.filename || 'a file'}".`);
  }
  return returned;
}

function planFilesForSelection(plan, entries) {
  const byFilename = new Map(plan.files.map((file) => [file.filename, file]));
  if (byFilename.size !== plan.files.length) {
    throw new Error('The upload plan has duplicate file names. Please start the upload again.');
  }
  if (plan.files.length !== entries.length) {
    throw new Error('The upload plan does not match the selected files. Please start again.');
  }

  return entries.map((entry) => {
    const planFile = byFilename.get(entry.filename);
    if (!planFile?.id || integer(planFile.size_bytes) !== entry.file.size) {
      throw new Error(`The upload plan does not match "${entry.filename}". Please start again.`);
    }
    const partSizeBytes = partSizeFor(planFile, plan.part_size_bytes);
    return {
      entry,
      planFile,
      partSizeBytes,
      partCount: partCountFor(planFile, entry.file, partSizeBytes),
    };
  });
}

function signedPartsFrom(response) {
  if (Array.isArray(response)) return response;
  if (Array.isArray(response?.parts)) return response.parts;
  if (Array.isArray(response?.data?.parts)) return response.data.parts;
  throw new Error('The API did not return signed upload part URLs.');
}

function signedPartNumber(part) {
  return integer(part?.part_number ?? part?.partNumber);
}

function uploadUrl(part) {
  const value = part?.url || part?.upload_url || part?.uploadUrl;
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) {
    throw new Error('The API returned an invalid signed upload URL.');
  }
  return value;
}

const DISALLOWED_UPLOAD_HEADERS = new Set([
  'authorization', 'content-length', 'host', 'origin', 'referer', 'user-agent',
]);

function normaliseUploadHeaders(headers) {
  if (!headers) return new Map();
  const pairs = headers instanceof Headers
    ? Array.from(headers.entries())
    : Array.isArray(headers)
      ? headers
      : Object.entries(headers);
  const result = new Map();
  for (const [rawName, rawValue] of pairs) {
    const name = String(rawName).trim().toLowerCase();
    const value = typeof rawValue === 'string' ? rawValue.trim() : '';
    if (!name || !value || /[\r\n]/.test(name) || /[\r\n]/.test(value) || DISALLOWED_UPLOAD_HEADERS.has(name)) {
      throw new Error('The API returned an unsafe signed upload header.');
    }
    result.set(name, value);
  }
  return result;
}

function mergeChecksumHeader(headers, checksumSha256, checksumMode) {
  const signedChecksum = headers.get('x-amz-checksum-sha256');
  if (signedChecksum && signedChecksum !== checksumSha256) {
    throw new Error('The signed part checksum does not match the locally verified part.');
  }
  if (checksumMode === 'sha256' && !signedChecksum) {
    headers.set('x-amz-checksum-sha256', checksumSha256);
  }
  return headers;
}

function uploadPartWithXhr({
  url,
  headers,
  blob,
  signal,
  onProgress,
  timeoutMs = DIRECT_PART_TIMEOUT_MS,
}) {
  return new Promise((resolve, reject) => {
    throwIfAborted(signal);
    const xhr = new XMLHttpRequest();
    let settled = false;

    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', abort);
      callback(value);
    };
    const abort = () => {
      try {
        xhr.abort();
      } finally {
        finish(reject, new UploadCancelledError());
      }
    };

    try {
      xhr.open('PUT', url, true);
      xhr.timeout = timeoutMs;
      for (const [name, value] of headers) xhr.setRequestHeader(name, value);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress?.(event.loaded);
      };
      xhr.onerror = () => finish(reject, new Error('Object storage rejected the upload part. Check the storage CORS policy and retry.'));
      xhr.onabort = () => finish(reject, new UploadCancelledError());
      xhr.ontimeout = () => finish(reject, new UploadTimeoutError(timeoutMs));
      xhr.onload = () => {
        if (xhr.status < 200 || xhr.status >= 300) {
          finish(reject, new Error(`Object storage rejected the upload part (${xhr.status}).`));
          return;
        }
        const etag = xhr.getResponseHeader('ETag') || xhr.getResponseHeader('etag');
        if (!etag) {
          finish(reject, new Error('Object storage did not expose the ETag header. Configure storage CORS to expose ETag.'));
          return;
        }
        onProgress?.(blob.size);
        finish(resolve, etag);
      };
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) {
        abort();
        return;
      }
      xhr.send(blob);
    } catch (error) {
      finish(reject, error instanceof Error ? error : new Error('Could not start the direct upload request.'));
    }
  });
}

async function concurrentMap(items, worker, {
  limit = MAX_PARALLEL_PART_UPLOADS,
  abort,
} = {}) {
  const results = new Array(items.length);
  let nextIndex = 0;
  let firstError = null;
  const runner = async () => {
    while (!firstError) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= items.length) return;
      try {
        results[index] = await worker(items[index]);
      } catch (error) {
        if (!firstError) {
          firstError = error;
          abort?.(error);
        }
        return;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, runner));
  if (firstError) throw firstError;
  return results;
}

function buildProgressReporter(mappings, onProgress) {
  const perFile = new Map(mappings.map(({ entry, planFile }) => [planFile.id, {
    filename: entry.filename,
    loaded_bytes: 0,
    total_bytes: entry.file.size,
  }]));
  const parts = new Map();
  const totalBytes = mappings.reduce((total, { entry }) => total + entry.file.size, 0);
  let loadedBytes = 0;

  return (uploadFileId, partNumber, loaded) => {
    const key = `${uploadFileId}:${partNumber}`;
    const previous = parts.get(key) || 0;
    const file = perFile.get(uploadFileId);
    if (!file) return;
    const safeLoaded = Math.max(previous, Math.min(loaded, file.total_bytes - file.loaded_bytes + previous));
    const delta = safeLoaded - previous;
    if (delta === 0) return;
    parts.set(key, safeLoaded);
    file.loaded_bytes += delta;
    loadedBytes += delta;
    onProgress?.({
      loaded_bytes: loadedBytes,
      total_bytes: totalBytes,
      files: Array.from(perFile.values()).map((value) => ({ ...value })),
    });
  };
}

async function hashBatchParts({ file, partNumbers, partSizeBytes, signal, onHashProgress }) {
  const chunks = [];
  for (const partNumber of partNumbers) {
    throwIfAborted(signal);
    const start = (partNumber - 1) * partSizeBytes;
    const end = Math.min(start + partSizeBytes, file.size);
    const blob = file.slice(start, end);
    const checksum_sha256 = await checksumSha256Base64(blob, { signal });
    chunks.push({ part_number: partNumber, blob, checksum_sha256 });
    onHashProgress?.({ partNumber, completedBytes: end, totalBytes: file.size });
  }
  return chunks;
}

function matchSignedParts(signedParts, chunks) {
  const byNumber = new Map(signedParts.map((part) => [signedPartNumber(part), part]));
  if (byNumber.size !== chunks.length) {
    throw new Error('The API returned an incomplete signed-part batch.');
  }
  return chunks.map((chunk) => {
    const signed = byNumber.get(chunk.part_number);
    if (!signed) throw new Error(`The API did not sign part ${chunk.part_number}.`);
    return { ...chunk, signed };
  });
}

/**
 * Upload selected browser File objects directly to signed object-storage URLs.
 * FastAPI only signs/revokes the plan and accepts multipart completion data;
 * no file bytes pass through the product API.
 */
export async function uploadMultipartPlan({
  plan: rawPlan,
  entries,
  signParts,
  signal,
  onProgress,
  onPhase,
}) {
  const linkedAbort = createLinkedAbortController(signal);
  const uploadSignal = linkedAbort.controller.signal;
  try {
    const plan = normalisePlan(rawPlan);
    if (!['sha256', 'server_verified'].includes(plan.multipart_checksum_mode)) {
      throw new Error('The upload plan has an unsupported checksum mode.');
    }
    const mappings = planFilesForSelection(plan, entries);
    const reportProgress = buildProgressReporter(mappings, onProgress);
    const completedFiles = [];

    for (const { entry, planFile, partCount, partSizeBytes } of mappings) {
      const completedParts = [];
      for (let firstPart = 1; firstPart <= partCount; firstPart += SIGN_BATCH_SIZE) {
        const partNumbers = Array.from(
          { length: Math.min(SIGN_BATCH_SIZE, partCount - firstPart + 1) },
          (_, index) => firstPart + index,
        );
        onPhase?.({ type: 'hashing_parts', filename: entry.filename, partNumber: firstPart, partCount });
        const chunks = await hashBatchParts({
          file: entry.file,
          partNumbers,
          partSizeBytes,
          signal: uploadSignal,
          onHashProgress: (progress) => onPhase?.({ type: 'hashing_parts', filename: entry.filename, partCount, ...progress }),
        });
        throwIfAborted(uploadSignal);
        onPhase?.({ type: 'signing_parts', filename: entry.filename, partNumber: firstPart, partCount });
        const signedResponse = await signParts(plan.id, {
          upload_file_id: planFile.id,
          parts: chunks.map(({ part_number, checksum_sha256 }) => ({ part_number, checksum_sha256 })),
        }, { signal: uploadSignal });
        const signedChunks = matchSignedParts(signedPartsFrom(signedResponse), chunks);

        onPhase?.({ type: 'uploading_parts', filename: entry.filename, partNumber: firstPart, partCount });
        const batchResults = await concurrentMap(signedChunks, async (chunk) => {
          throwIfAborted(uploadSignal);
          const headers = mergeChecksumHeader(
            normaliseUploadHeaders(chunk.signed.headers),
            chunk.checksum_sha256,
            plan.multipart_checksum_mode,
          );
          const etag = await uploadPartWithXhr({
            url: uploadUrl(chunk.signed),
            headers,
            blob: chunk.blob,
            signal: uploadSignal,
            onProgress: (loaded) => reportProgress(planFile.id, chunk.part_number, loaded),
          });
          return {
            part_number: chunk.part_number,
            etag,
            checksum_sha256: chunk.checksum_sha256,
          };
        }, {
          abort: (reason) => linkedAbort.controller.abort(reason),
        });
        completedParts.push(...batchResults);
      }
      completedParts.sort((left, right) => left.part_number - right.part_number);
      completedFiles.push({ upload_file_id: planFile.id, parts: completedParts });
    }

    return { files: completedFiles };
  } finally {
    linkedAbort.dispose();
  }
}
