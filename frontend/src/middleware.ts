import { NextResponse, type NextRequest } from 'next/server';
import { createServerClient } from '@supabase/ssr';

/**
 * Admin-only gate for the Rhodey OS dashboard.
 *
 * WHY: dashboard pages and /api/* proxy routes read the DB with the
 * SUPABASE_SERVICE_ROLE_KEY (bypasses RLS) and intentionally carry NO
 * owner_id filter — the dashboard is the *operator's* cross-tenant view, not a
 * per-tenant surface. So it must be locked to an explicit admin allowlist
 * rather than "any Supabase-authenticated Google account".
 *
 * Config: set DASHBOARD_ADMIN_EMAILS in the frontend environment
 * (comma-separated, case-insensitive). If the variable is empty, the
 * dashboard is completely locked (fail-closed).
 */
const ADMIN_EMAILS = (process.env.DASHBOARD_ADMIN_EMAILS ?? '')
  .split(',')
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean);

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Health probes (uptime monitors) stay open.
  if (pathname === '/api/health') {
    return NextResponse.next();
  }

  // Session-cookie client (browser-context, anon key — safe for auth only).
  let supabaseResponse = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          supabaseResponse = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options)
          );
        },
      },
    }
  );

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const email = user?.email?.toLowerCase() ?? null;
  const isAdmin = email !== null && ADMIN_EMAILS.length > 0 && ADMIN_EMAILS.includes(email);

  if (!isAdmin) {
    if (pathname.startsWith('/api/')) {
      return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
    }
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.searchParams.set('error', 'not-authorized');
    url.search = url.search; // keep any existing params (e.g. ?error=)
    return NextResponse.redirect(url);
  }

  return supabaseResponse;
}

export const config = {
  // NOTE: /auth/callback is intentionally NOT gated here — the user has no
  // session cookie yet at that point (they just returned from Google with a
  // `code`). The allowlist check for that route lives inside the callback
  // handler itself, after the code exchange.
  matcher: ['/dashboard/:path*', '/api/:path*'],
};
