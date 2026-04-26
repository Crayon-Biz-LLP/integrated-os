import { NextResponse } from 'next/server';
import { createServerClient } from '@supabase/ssr';

const AUTHORIZED_EMAIL = 'danielyashwant@gmail.com';

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get('code');

  if (!code) {
    return NextResponse.redirect(new URL('/login?error=auth_failed', url.origin));
  }

  const response = NextResponse.redirect(new URL('/dashboard/tasks', url.origin));

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.headers.get('cookie')?.split(';').map((c) => {
            const [name, ...rest] = c.trim().split('=');
            return { name, value: rest.join('=') };
          }) ?? [];
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options)
          );
        },
      },
    }
  );

  const { error: sessionError } = await supabase.auth.exchangeCodeForSession(code);
  if (sessionError) {
    return NextResponse.redirect(new URL('/login?error=auth_failed', url.origin));
  }

  const { data: { user }, error: userError } = await supabase.auth.getUser();
  if (userError || !user) {
    return NextResponse.redirect(new URL('/login?error=auth_failed', url.origin));
  }

  if (user.email !== AUTHORIZED_EMAIL) {
    await supabase.auth.signOut();
    return NextResponse.redirect(new URL('/login?error=unauthorized', url.origin));
  }

  return response;
}
