import { NextResponse } from 'next/server';
import { createServerClient, type CookieOptions } from '@supabase/ssr';
import { cookies } from 'next/headers';

/**
 * Admin-only callback: after exchanging the OAuth code, verify the signed-in
 * email is in DASHBOARD_ADMIN_EMAILS (comma-separated, case-insensitive).
 * Non-admins are signed straight back out — they never see dashboard data.
 */
const ADMIN_EMAILS = (process.env.DASHBOARD_ADMIN_EMAILS ?? '')
  .split(',')
  .map((e) => e.trim().toLowerCase())
  .filter(Boolean);

export async function GET(request: Request) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get('code');
  const next = searchParams.get('next') ?? '/dashboard';

  if (code) {
    const cookieStore = await cookies();
    const supabase = createServerClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
      {
        cookies: {
          get(name: string) {
            return cookieStore.get(name)?.value;
          },
          set(name: string, value: string, options: CookieOptions) {
            cookieStore.set({ name, value, ...options });
          },
          remove(name: string, options: CookieOptions) {
            cookieStore.delete({ name, ...options });
          },
        },
      }
    );
    const { error } = await supabase.auth.exchangeCodeForSession(code);

    if (!error) {
      const {
        data: { user },
      } = await supabase.auth.getUser();
      const email = user?.email?.toLowerCase() ?? null;
      const isAdmin =
        email !== null && ADMIN_EMAILS.length > 0 && ADMIN_EMAILS.includes(email);

      if (!isAdmin) {
        // Fail closed: bounce non-admins and clear their session.
        await supabase.auth.signOut();
        return NextResponse.redirect(`${origin}/login?error=not-authorized`);
      }

      return NextResponse.redirect(`${origin}${next}`);
    }
  }

  return NextResponse.redirect(`${origin}/login?error=Could not authenticate user`);
}
