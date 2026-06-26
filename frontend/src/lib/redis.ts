import { Redis } from '@upstash/redis';

// Use a singleton instance to prevent multiple client creations
let redisClient: Redis | null = null;

export function getRedisClient(): Redis | null {
  if (redisClient) return redisClient;
  
  // Graceful fallback if env vars are missing
  if (!process.env.UPSTASH_REDIS_REST_URL || !process.env.UPSTASH_REDIS_REST_TOKEN) {
    console.warn('⚠️ UPSTASH_REDIS_REST_URL or UPSTASH_REDIS_REST_TOKEN not set. Caching disabled.');
    return null;
  }
  
  try {
    redisClient = Redis.fromEnv();
    return redisClient;
  } catch (error) {
    console.error('Failed to initialize Redis client:', error);
    return null;
  }
}

/**
 * Wraps an async function with Redis caching.
 * If Redis is not configured or fails, gracefully falls back to just calling fetchFn().
 */
export async function getCachedOrFetch<T>(
  key: string,
  ttlSeconds: number,
  fetchFn: () => Promise<T>
): Promise<T> {
  const redis = getRedisClient();
  
  if (!redis) {
    return fetchFn();
  }

  try {
    const cached = await redis.get<T>(key);
    if (cached !== null && cached !== undefined) {
      return cached;
    }
  } catch (error) {
    console.warn(`Redis get failed for key ${key}:`, error);
    // On read failure, proceed to fetch fresh data
  }

  const data = await fetchFn();

  try {
    await redis.set(key, data, { ex: ttlSeconds });
  } catch (error) {
    console.warn(`Redis set failed for key ${key}:`, error);
  }

  return data;
}
