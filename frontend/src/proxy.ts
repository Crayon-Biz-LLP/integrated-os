import { NextResponse, NextRequest } from 'next/server';

export function proxy(request: NextRequest) {
  try {
    const { pathname } = request.nextUrl;
    
    // Check for Supabase auth cookies
    const hasAuthCookie = request.cookies.has('sb-access-token') || 
      request.cookies.has('sb-refresh-token');

    // Protected routes - redirect to login if not authenticated
    if (pathname.startsWith('/dashboard') && !hasAuthCookie) {
      return NextResponse.redirect(new URL('/login', request.url));
    }

    // Auth routes - redirect to dashboard if already authenticated
    if ((pathname.startsWith('/login') || pathname.startsWith('/auth')) && hasAuthCookie) {
      return NextResponse.redirect(new URL('/dashboard/tasks', request.url));
    }

    return NextResponse.next();
  } catch (error) {
    // Log error and allow request to proceed
    console.error('Proxy error:', error);
    return NextResponse.next();
  }
}

export const config = {
  matcher: ['/dashboard/:path*', '/login', '/auth/callback'],
};
