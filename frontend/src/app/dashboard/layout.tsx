'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import {
  Cpu,
  CheckSquare,
  FolderOpen,
  Mail,
  Brain,
  Calendar,
  Users,
  BookOpen,
  LogOut,
  Menu,
} from 'lucide-react';
import { createClient } from '@/lib/supabase';
import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetTrigger,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';

const navItems = [
  { href: '/dashboard/tasks', label: 'Tasks', icon: CheckSquare },
  { href: '/dashboard/projects', label: 'Projects', icon: FolderOpen },
  { href: '/dashboard/emails', label: 'Emails', icon: Mail },
  { href: '/dashboard/memories', label: 'Memories', icon: Brain },
  { href: '/dashboard/calendar', label: 'Calendar', icon: Calendar },
  { href: '/dashboard/people', label: 'People', icon: Users },
  { href: '/dashboard/resources', label: 'Resources', icon: BookOpen },
];

const mobileItems = navItems.slice(0, 5);

const routeTitles: Record<string, string> = {
  '/dashboard/tasks': 'Tasks',
  '/dashboard/projects': 'Projects',
  '/dashboard/emails': 'Emails',
  '/dashboard/memories': 'Memories',
  '/dashboard/calendar': 'Calendar',
  '/dashboard/people': 'People',
  '/dashboard/resources': 'Resources',
};

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const [userEmail, setUserEmail] = useState<string | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const supabase = createClient();

  const pageTitle = routeTitles[pathname] || 'Dashboard';

  useEffect(() => {
    supabase.auth.getUser().then(({ data }) => {
      setUserEmail(data.user?.email || null);
    });
  }, [supabase]);

  const handleLogout = async () => {
    await supabase.auth.signOut();
    window.location.href = '/login';
  };

  const NavLink = ({ item, onClick }: { item: typeof navItems[0]; onClick?: () => void }) => {
    const Icon = item.icon;
    const isActive = pathname === item.href;
    return (
      <Link
        href={item.href}
        onClick={onClick}
        className={cn(
          'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
          isActive
            ? 'bg-accent text-accent-foreground'
            : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
        )}
      >
        <Icon className="h-5 w-5" />
        {item.label}
      </Link>
    );
  };

  return (
    <div className="min-h-screen bg-background">
      {/* Desktop Sidebar */}
      <aside className="hidden lg:fixed lg:inset-y-0 lg:z-50 lg:flex lg:w-64 lg:flex-col lg:bg-zinc-900 lg:text-zinc-100">
        {/* Logo */}
        <div className="flex h-16 items-center gap-2 border-b border-zinc-800 px-6">
          <Cpu className="h-5 w-5 text-primary" />
          <span className="text-lg font-bold">Rhodey OS</span>
        </div>

        {/* Nav Links */}
        <nav className="flex-1 space-y-1 p-4">
          {navItems.map((item) => (
            <NavLink key={item.href} item={item} />
          ))}
        </nav>

        {/* User Info & Logout */}
        <div className="border-t border-zinc-800 p-4">
          {userEmail && (
            <p className="mb-2 truncate text-xs text-zinc-400">{userEmail}</p>
          )}
          <Button
            variant="ghost"
            onClick={handleLogout}
            className="w-full justify-start gap-2 px-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
          >
            <LogOut className="h-4 w-4" />
            Logout
          </Button>
        </div>
      </aside>

      {/* Mobile Header */}
      <header className="fixed inset-x-0 top-0 z-40 flex h-14 items-center justify-between border-b bg-background px-4 lg:hidden">
        <h1 className="text-lg font-semibold">{pageTitle}</h1>
        <Sheet open={isOpen} onOpenChange={setIsOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon">
              <Menu className="h-5 w-5" />
            </Button>
          </SheetTrigger>
          <SheetContent side="right" className="w-64">
            <div className="flex flex-col gap-6 pt-6">
              {/* Mobile Nav */}
              <div className="flex flex-col gap-1">
                {navItems.map((item) => (
                  <NavLink key={item.href} item={item} onClick={() => setIsOpen(false)} />
                ))}
              </div>
              {/* Mobile Logout */}
              <div className="border-t pt-4">
                <Button
                  variant="ghost"
                  onClick={handleLogout}
                  className="w-full justify-start gap-2 text-muted-foreground"
                >
                  <LogOut className="h-4 w-4" />
                  Logout
                </Button>
              </div>
            </div>
          </SheetContent>
        </Sheet>
      </header>

      {/* Mobile Bottom Tab Bar */}
      <nav className="fixed inset-x-0 bottom-0 z-40 border-t bg-background pb-safe lg:hidden">
        <div className="flex h-16 items-center justify-around">
          {mobileItems.map((item) => {
            const Icon = item.icon;
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  'flex flex-col items-center gap-0.5 px-3 py-2 text-xs transition-colors',
                  isActive ? 'text-primary' : 'text-muted-foreground'
                )}
              >
                <Icon className="h-5 w-5" />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </div>
      </nav>

      {/* Main Content */}
      <main className="lg:pl-64">
        <div className="min-h-screen pt-14 pb-16 lg:pb-0">
          {children}
        </div>
      </main>
    </div>
  );
}