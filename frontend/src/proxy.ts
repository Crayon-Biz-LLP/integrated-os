import { NextResponse, NextRequest } from 'next/server';

export function proxy(request: NextRequest) {
  try {
    const { pathname } = request.nextUrl;
    
    // Check for Supabase auth cookies (try multiple possible names)
    const cookies = request.cookies.getAll();
    const hasAuthCookie = cookies.some(c => 
      c.name.startsWith('sb-') && 
      (c.name.includes('auth-token') || c.name.includes('access-token') || c.name.includes('refresh-token'))
    );

    // Protected routes - redirect to login if not authenticated
    if (pathname.startsWith('/dashboard') && !hasAuthCookie) {
      return NextResponse.redirect(new URL('/login', request.url));
    }

    // Auth routes - redirect to dashboard if already authenticated
    if ((pathname.startsWith('/login') || pathname.startsWith('/auth/callback')) && hasAuthCookie) {
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
