/**
 * A UUID v4 for durable request idempotency.
 *
 * Shared by the upload path (where it keys sessionStorage recovery) and the
 * Copernicus path (where it maps to scene_acquisitions.client_request_id, so a
 * retried POST replays the original acquisition instead of starting a second
 * ~1 GB download). The manual fallback covers browsers without randomUUID,
 * which is unavailable on insecure origins.
 */
export function createClientRequestId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID();
  const randomHex = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
  const first = randomHex();
  const second = randomHex();
  const third = randomHex();
  const fourth = randomHex();
  return `${first}-${second.slice(0, 4)}-4${second.slice(5, 8)}-${((8 + Math.floor(Math.random() * 4)).toString(16))}${third.slice(1, 4)}-${third.slice(4)}${fourth}`;
}

export function formatBytes(bytes, decimals = 2) {
  if (!+bytes) return '0 Bytes';

  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];

  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}
