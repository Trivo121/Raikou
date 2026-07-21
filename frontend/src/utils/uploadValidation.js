const ZIP_EXTENSIONS = new Set(['zip']);
const TIFF_EXTENSIONS = new Set(['tif', 'tiff']);
const JSON_EXTENSIONS = new Set(['json']);

const MIME_BY_KIND = {
  zip: new Set(['application/zip', 'application/x-zip-compressed', 'multipart/x-zip']),
  tiff: new Set(['image/tiff', 'image/x-tiff']),
  json: new Set(['application/json', 'text/json', 'application/geo+json']),
};

const CONTENT_TYPE_BY_KIND = {
  zip: 'application/zip',
  tiff: 'image/tiff',
  json: 'application/json',
};

function extensionOf(filename) {
  const lastDot = filename.lastIndexOf('.');
  return lastDot > -1 ? filename.slice(lastDot + 1).toLowerCase() : '';
}

function kindForFile(file) {
  const extension = extensionOf(file.name);
  if (ZIP_EXTENSIONS.has(extension)) return 'zip';
  if (TIFF_EXTENSIONS.has(extension)) return 'tiff';
  if (JSON_EXTENSIONS.has(extension)) return 'json';
  return null;
}

function isSafeBrowserFilename(filename) {
  return (
    typeof filename === 'string'
    && /^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$/.test(filename)
  );
}

function mimeLooksCompatible(file, kind) {
  // Browsers often provide an empty string or application/octet-stream for
  // geospatial TIFFs. The API remains authoritative and performs content
  // sniffing; reject only an explicitly incompatible browser MIME value.
  const reported = (file.type || '').toLowerCase().trim();
  return !reported || reported === 'application/octet-stream' || MIME_BY_KIND[kind].has(reported);
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / (1024 ** index);
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

/**
 * Validate the client-visible shape before any hashing or network work.
 * The server repeats every check (including archive expansion limits), so a
 * manipulated browser never becomes a validation bypass.
 */
export function validateUploadFiles(fileList) {
  const files = Array.from(fileList || []);
  const errors = [];

  if (files.length === 0) {
    errors.push('Choose a ZIP archive, or one or two TIFF files with an optional JSON sidecar.');
    return { files: [], errors };
  }
  if (files.length > 3) {
    errors.push('Choose at most three files: up to two TIFFs and one optional JSON sidecar.');
  }

  const seenNames = new Set();
  const describedFiles = files.map((file) => {
    const kind = kindForFile(file);
    const normalizedName = (file.name || '').toLocaleLowerCase();

    if (!isSafeBrowserFilename(file.name)) {
      errors.push(`"${file.name || 'Unnamed file'}" is not a safe filename.`);
    }
    if (seenNames.has(normalizedName)) {
      errors.push(`"${file.name}" appears more than once. Choose files with distinct names.`);
    }
    seenNames.add(normalizedName);
    if (!Number.isSafeInteger(file.size) || file.size <= 0) {
      errors.push(`"${file.name}" is empty or has an unsupported size.`);
    }
    if (!kind) {
      errors.push(`"${file.name}" is unsupported. Use ZIP, TIFF (.tif/.tiff), or JSON.`);
    } else if (!mimeLooksCompatible(file, kind)) {
      errors.push(`"${file.name}" reports ${file.type}; expected a ${kind.toUpperCase()} MIME type.`);
    }

    return {
      file,
      kind,
      filename: file.name,
      content_type: kind ? CONTENT_TYPE_BY_KIND[kind] : file.type || 'application/octet-stream',
      size_bytes: file.size,
    };
  });

  const zips = describedFiles.filter(({ kind }) => kind === 'zip');
  const tiffs = describedFiles.filter(({ kind }) => kind === 'tiff');
  const json = describedFiles.filter(({ kind }) => kind === 'json');

  if (zips.length > 0 && (tiffs.length > 0 || zips.length > 1)) {
    errors.push('Choose one ZIP archive or GeoTIFF input; do not mix them.');
  }
  if (zips.length === 0 && (tiffs.length < 1 || tiffs.length > 2)) {
    errors.push('Choose one or two TIFF files when not uploading a ZIP archive.');
  }
  if (json.length > 1) {
    errors.push('Only one optional JSON sidecar is supported.');
  }

  return { files: describedFiles, errors };
}

export function supportedInputDescription() {
  return 'One ZIP archive, or one or two TIFF files with one optional JSON sidecar.';
}
