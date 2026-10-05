/**
 * CloudStream WebDAV Bridge - Mount Manager
 * Manages virtual stream mounts backed by Cloudflare KV and in-memory isolate caching.
 */

export interface StreamMount {
  id: string;               // Unique mount ID: "mount_8f3a9e2c"
  filename: string;         // Virtual filename: "Oppenheimer.2023.2160p.UHD.Remux.mkv"
  title: string;            // Human-readable title
  upstream_url: string;     // Direct CDN presigned streaming URL
  size_bytes: number;       // Exact file size in bytes (e.g. 64424509440)
  content_type: string;     // MIME: "video/x-matroska", "video/mp4"
  created_at: number;       // Epoch timestamp ms
  etag: string;             // Deterministic ETag: W/"8f3a9e2c-64424509440"
  custom_headers?: Record<string, string>; // Preserved auth/range headers
}

export interface UserMountsRecord {
  user_id: string;
  updated_at: number;
  mounts: StreamMount[];
}

export interface Env {
  MOUNTS_KV?: KVNamespace;
  [key: string]: unknown;
}

export type MountManagerEnv = Env;

interface CacheEntry {
  record: UserMountsRecord;
  cachedAt: number;
}

// 15-second in-memory isolate cache to reduce KV reads by 90%+
const isolateMountsCache = new Map<string, CacheEntry>();
const ISOLATE_CACHE_TTL_MS = 15_000;
const KV_TTL_SECONDS = 86_400; // 24 hours (86400s)

// Fallback in-memory storage when KV is not bound (unit testing, local simulation)
const fallbackMemoryStore = new Map<string, UserMountsRecord>();

/**
 * Retrieve mounts for a user from isolate memory cache or Cloudflare KV.
 * Uses 15s in-memory isolate cache.
 */
export async function getUserMounts(
  env: Env,
  userId: string,
  forceRefresh = false
): Promise<StreamMount[]> {
  const now = Date.now();
  const cached = isolateMountsCache.get(userId);

  if (!forceRefresh && cached && now - cached.cachedAt < ISOLATE_CACHE_TTL_MS) {
    return [...cached.record.mounts];
  }

  if (env?.MOUNTS_KV) {
    try {
      const data = await env.MOUNTS_KV.get(`mounts:${userId}`, { type: "json" });
      if (data) {
        const record = data as UserMountsRecord;
        const mounts = Array.isArray(record?.mounts) ? record.mounts : [];
        const validRecord: UserMountsRecord = {
          user_id: userId,
          updated_at: record.updated_at || now,
          mounts,
        };
        isolateMountsCache.set(userId, { record: validRecord, cachedAt: now });
        return [...mounts];
      }
        isolateMountsCache.set(userId, {
          record: { user_id: userId, updated_at: now, mounts: [] },
          cachedAt: now,
        });
        return [];
    } catch (_err) {
      if (!forceRefresh && cached) {
        return [...cached.record.mounts];
      }
      return [];
    }
  }

  // Fallback to in-memory store
  const memRecord = fallbackMemoryStore.get(userId) || null;
  if (memRecord) {
    isolateMountsCache.set(userId, { record: memRecord, cachedAt: now });
    return [...memRecord.mounts];
  }

  isolateMountsCache.delete(userId);
  return [];
}

/**
 * Save or replace a user mount in Cloudflare KV (key mounts:{userId}, TTL 86400s)
 * and update the in-memory isolate cache. Returns updated list of StreamMounts.
 */
export async function saveUserMount(
  env: Env,
  userId: string,
  mount: StreamMount
): Promise<StreamMount[]> {
  const existing = await getUserMounts(env, userId, true);
  const now = Date.now();

  const remaining = existing.filter(
    (m) => m.filename !== mount.filename && m.id !== mount.id
  );
  const updatedMounts = [mount, ...remaining];

  const record: UserMountsRecord = {
    user_id: userId,
    updated_at: now,
    mounts: updatedMounts,
  };

  isolateMountsCache.set(userId, { record, cachedAt: now });
  fallbackMemoryStore.set(userId, record);

  if (env?.MOUNTS_KV) {
    await env.MOUNTS_KV.put(
      `mounts:${userId}`,
      JSON.stringify(record),
      { expirationTtl: KV_TTL_SECONDS }
    );
  }

  return [...updatedMounts];
}

/**
 * Delete a user mount by filename or mount ID.
 * Returns true if found and removed, false otherwise.
 */
export async function deleteUserMount(
  env: Env,
  userId: string,
  filenameOrId: string
): Promise<boolean> {
  const existing = await getUserMounts(env, userId, true);
  if (!existing || existing.length === 0) {
    return false;
  }

  const cleanTarget = decodeURIComponent(filenameOrId).trim().toLowerCase();
  const initialCount = existing.length;
  const remaining = existing.filter(
    (m) =>
      m.id !== filenameOrId &&
      m.filename !== filenameOrId &&
      m.filename.toLowerCase() !== cleanTarget &&
      encodeURIComponent(m.filename).toLowerCase() !== cleanTarget
  );

  if (remaining.length === initialCount) {
    return false;
  }

  const now = Date.now();
  const updatedRecord: UserMountsRecord = {
    user_id: userId,
    updated_at: now,
    mounts: remaining,
  };

  isolateMountsCache.set(userId, { record: updatedRecord, cachedAt: now });
  fallbackMemoryStore.set(userId, updatedRecord);

  if (env?.MOUNTS_KV) {
    if (remaining.length === 0) {
      await env.MOUNTS_KV.delete(`mounts:${userId}`);
    } else {
      await env.MOUNTS_KV.put(
        `mounts:${userId}`,
        JSON.stringify(updatedRecord),
        { expirationTtl: KV_TTL_SECONDS }
      );
    }
  }

  return true;
}

/**
 * Clear all mounts for a given user from KV and memory cache.
 */
export async function clearUserMounts(
  env: Env,
  userId: string
): Promise<void> {
  isolateMountsCache.delete(userId);
  fallbackMemoryStore.delete(userId);

  if (env?.MOUNTS_KV) {
    await env.MOUNTS_KV.delete(`mounts:${userId}`);
  }
}

/**
 * Invalidate the isolate memory cache for a given user (or all if omitted).
 */
export function invalidateCache(userId?: string): void {
  if (userId) {
    isolateMountsCache.delete(userId);
  } else {
    isolateMountsCache.clear();
  }
}

/**
 * Retrieve user mounts record from isolate memory cache or Cloudflare KV.
 */
export async function getMountsRecord(
  env: Env,
  userId: string,
  forceRefresh = false
): Promise<UserMountsRecord | null> {
  const mounts = await getUserMounts(env, userId, forceRefresh);
  const cached = isolateMountsCache.get(userId);
  if (cached) {
    return cached.record;
  }
  if (mounts.length > 0) {
    return {
      user_id: userId,
      updated_at: Date.now(),
      mounts,
    };
  }
  return null;
}

/**
 * Save user mounts record to Cloudflare KV and update isolate memory cache.
 */
export async function saveMountsRecord(
  env: Env,
  record: UserMountsRecord
): Promise<void> {
  const now = Date.now();
  isolateMountsCache.set(record.user_id, { record, cachedAt: now });
  fallbackMemoryStore.set(record.user_id, record);

  if (env?.MOUNTS_KV) {
    await env.MOUNTS_KV.put(
      `mounts:${record.user_id}`,
      JSON.stringify(record),
      { expirationTtl: KV_TTL_SECONDS }
    );
  }
}

/**
 * Add or replace a stream mount for a given user.
 */
export async function addMount(
  env: Env,
  userId: string,
  mount: StreamMount
): Promise<UserMountsRecord> {
  const updatedMounts = await saveUserMount(env, userId, mount);
  return {
    user_id: userId,
    updated_at: Date.now(),
    mounts: updatedMounts,
  };
}

/**
 * Remove a specific mount by filename or ID for a given user.
 */
export async function removeMount(
  env: Env,
  userId: string,
  filename: string
): Promise<boolean> {
  return deleteUserMount(env, userId, filename);
}

/**
 * Clear all mounts for a given user and return count of deleted mounts.
 */
export async function clearMounts(
  env: Env,
  userId: string
): Promise<number> {
  const existing = await getUserMounts(env, userId, true);
  const count = existing ? existing.length : 0;
  await clearUserMounts(env, userId);
  return count;
}

/**
 * Look up a specific mount by filename or id for a user.
 */
export async function findMount(
  env: Env,
  userId: string,
  filename: string
): Promise<StreamMount | null> {
  const mounts = await getUserMounts(env, userId);
  if (!mounts || mounts.length === 0) {
    return null;
  }

  const cleanTarget = decodeURIComponent(filename).trim().toLowerCase();
  const match = mounts.find(
    (m) =>
      m.filename.toLowerCase() === cleanTarget ||
      encodeURIComponent(m.filename).toLowerCase() === cleanTarget ||
      m.id === filename
  );

  return match || null;
}

/**
 * MountManager class encapsulating all mount operations.
 */
export class MountManager {
  static getUserMounts = getUserMounts;
  static saveUserMount = saveUserMount;
  static deleteUserMount = deleteUserMount;
  static clearUserMounts = clearUserMounts;
  static invalidateCache = invalidateCache;
  static getMountsRecord = getMountsRecord;
  static saveMountsRecord = saveMountsRecord;
  static addMount = addMount;
  static removeMount = removeMount;
  static clearMounts = clearMounts;
  static findMount = findMount;

  getUserMounts(env: Env, userId: string, forceRefresh = false): Promise<StreamMount[]> {
    return getUserMounts(env, userId, forceRefresh);
  }
  saveUserMount(env: Env, userId: string, mount: StreamMount): Promise<StreamMount[]> {
    return saveUserMount(env, userId, mount);
  }
  deleteUserMount(env: Env, userId: string, filenameOrId: string): Promise<boolean> {
    return deleteUserMount(env, userId, filenameOrId);
  }
  clearUserMounts(env: Env, userId: string): Promise<void> {
    return clearUserMounts(env, userId);
  }
  invalidateCache(userId?: string): void {
    invalidateCache(userId);
  }
}

export default MountManager;
