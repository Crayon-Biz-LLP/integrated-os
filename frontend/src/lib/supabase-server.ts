import { createClient, type SupabaseClient } from '@supabase/supabase-js';

type QueryBuilder = ReturnType<SupabaseClient['from']>;

/**
 * createServerSupabaseClient — dashboard DB access, HARD-pinned to one tenant.
 *
 * The dashboard reads with SUPABASE_SERVICE_ROLE_KEY, which BYPASSES RLS, so
 * the owner_id filter is the ONLY thing separating tenants here. Instead of
 * trusting ~130 hand-written queries to each remember `.eq('owner_id', X)`,
 * we wrap the client so EVERY table access is auto-scoped:
 *
 *   - select  →  .select(...).eq("owner_id", OWNER_ID)
 *   - update  →  .update(...).eq("owner_id", OWNER_ID)
 *   - delete  →  .delete(...).eq("owner_id", OWNER_ID)
 *   - insert  →  owner_id stamped into the payload
 *
 * Config: DASHBOARD_OWNER_ID = the users.id of the tenant this dashboard
 * serves (fail-closed — if unset, the client refuses to build).
 */
export async function createServerSupabaseClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const ownerId = process.env.DASHBOARD_OWNER_ID?.trim();

  if (!url || !key) {
    throw new Error(
      'Missing Supabase environment variables: NEXT_PUBLIC_SUPABASE_URL and/or SUPABASE_SERVICE_ROLE_KEY'
    );
  }

  if (!ownerId) {
    throw new Error(
      'DASHBOARD_OWNER_ID is not set. The dashboard reads with the service-role key ' +
        '(bypasses RLS) and MUST be pinned to a single tenant. Set DASHBOARD_OWNER_ID to the ' +
        "users.id of the tenant this dashboard serves (e.g. Danny: c302706e-fe61-422a-b384-68e3bc8f6f8e)."
    );
  }

  const client = createClient(url, key, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
    },
  });

  // Preserve the client's prototype; only override `from` to inject the filter.
  // `typeof client` keeps the exact type inference the callers already rely on
  // (same as the pre-scoped client), so no call site changes type-wise.
  const scoped = Object.create(client) as typeof client;
  scoped.from = (table: string) => scopedBuilder(client.from(table), ownerId);
  return scoped;
}

/**
 * Wrap a PostgrestQueryBuilder so every terminal op carries the owner filter.
 */
function scopedBuilder(builder: QueryBuilder, ownerId: string): QueryBuilder {
  return new Proxy(builder, {
    get(target, prop, receiver) {
      const orig = Reflect.get(target, prop, receiver);

      if (prop === 'select') {
        return (columns?: string, options?: object) =>
          orig.call(target, columns, options).eq('owner_id', ownerId);
      }
      if (prop === 'update' || prop === 'delete') {
        return (...args: unknown[]) => orig.apply(target, args).eq('owner_id', ownerId);
      }
      if (prop === 'insert' || prop === 'upsert') {
        return (values: unknown, options?: object) =>
          orig.call(target, stampOwner(values, ownerId), options);
      }

      return typeof orig === 'function' ? orig.bind(target) : orig;
    },
  }) as QueryBuilder;
}

function stampOwner(values: unknown, ownerId: string): unknown {
  const stamp = (row: Record<string, unknown>) => ({ ...row, owner_id: ownerId });
  return Array.isArray(values) ? values.map(stamp) : stamp(values as Record<string, unknown>);
}
