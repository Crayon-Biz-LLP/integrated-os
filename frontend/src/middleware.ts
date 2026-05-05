import { NextResponse, NextRequest } from 'next/server';

export function middleware(request: NextRequest) {
  const isProtected = request.nextUrl.pathname.startsWith('/dashboard');
  const isAuthRoute = request.nextUrl.pathname.startsWith('/login') || 
    request.nextUrl.pathname.startsWith('/auth');

  // Check for Supabase auth cookies (set after login)
  const hasAuthCookie = request.cookies.has('sb-access-token') || 
    request.cookies.has('sb-refresh-token');

  if (isProtected && !hasAuthCookie) {
    return NextResponse.redirect(new URL('/login', request.url));
  }

  if (isAuthRoute && hasAuthCookie) {
    return NextResponse.redirect(new URL('/dashboard/tasks', request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/dashboard/:path*', '/login', '/auth/callback'],
};
