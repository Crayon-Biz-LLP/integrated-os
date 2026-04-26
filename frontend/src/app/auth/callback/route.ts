import { NextResponse } from 'next/server';
import { createServerSupabaseClient } from '@/lib/supabase';

const AUTHORIZED_EMAIL = 'danielyashwant@gmail.com';

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get('code');

  if (code) {
    const supabase = await createServerSupabaseClient();
    const { data: { user }, error } = await supabase.auth.getUser();

    if (error || !user) {
      return NextResponse.redirect(new URL('/login?error=auth_failed', url.origin));
    }

    const email = user.email;
    if (email !== AUTHORIZED_EMAIL) {
      await supabase.auth.signOut();
      return NextResponse.redirect(new URL('/login?error=unauthorized', url.origin));
    }

    return NextResponse.redirect(new URL('/dashboard/tasks', url.origin));
  }

  return NextResponse.redirect(new URL('/login?error=auth_failed', url.origin));
}