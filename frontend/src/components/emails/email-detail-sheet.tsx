'use client';

import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet';
import { Badge } from '@/components/ui/badge';
import type { Email } from '@/lib/emails/types';
import { format } from 'date-fns';
import { User, Building, Mail, Tag, Globe } from 'lucide-react';
import { cn } from '@/lib/utils';

interface EmailDetailSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  email: Email | null;
}

const CLASSIFICATION_CONFIG = {
  actionable: { className: 'bg-primary/10 text-primary border border-primary/20 text-xs px-2 py-0.5 rounded-full font-medium' },
  fyi: { className: 'bg-blue-500/10 text-blue-600 border border-blue-500/20 text-xs px-2 py-0.5 rounded-full font-medium' },
  ignored: { className: 'bg-muted text-muted-foreground border border-border text-xs px-2 py-0.5 rounded-full' },
} as const;

export function EmailDetailSheet({ open, onOpenChange, email }: EmailDetailSheetProps) {
  if (!email) return null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[600px] sm:max-w-[600px]">
        <SheetHeader>
          <SheetTitle className="text-lg font-semibold tracking-tight truncate">{email.subject}</SheetTitle>
          <SheetDescription className="section-label mb-1">
            Received on {format(new Date(email.received_at), 'PPpp')}
          </SheetDescription>
        </SheetHeader>
        <div className="mt-6 space-y-6">
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm">
              <User className="h-4 w-4 text-muted-foreground" />
              <span className="section-label mb-1">From:</span>
              <span className="text-sm text-foreground">{email.sender || email.sender_email}</span>
              {email.sender && <span className="text-sm text-muted-foreground">({email.sender_email})</span>}
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Mail className="h-4 w-4 text-muted-foreground" />
              <span className="section-label mb-1">Subject:</span>
              <span className="text-base font-semibold tracking-tight">{email.subject}</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Tag className="h-4 w-4 text-muted-foreground" />
              <span className="section-label mb-1">Classification:</span>
              <span className={cn('text-xs', (CLASSIFICATION_CONFIG as Record<string, { className: string }>)[email.classification]?.className || '')}>
                {email.classification}
              </span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Globe className="h-4 w-4 text-muted-foreground" />
              <span className="section-label mb-1">Source:</span>
              <span className="text-xs bg-muted/60 text-muted-foreground px-2 py-0.5 rounded font-mono">{email.source}</span>
            </div>
            {(email.linked_project || email.linked_person) && (
              <div className="flex items-center gap-2 text-sm">
                <Building className="h-4 w-4 text-muted-foreground" />
                <span className="section-label mb-1">Linked:</span>
                <div className="flex gap-2">
                  {email.linked_project && (
                    <span className="text-xs bg-muted text-muted-foreground/80 px-2.5 py-1 rounded-full border border-border/60 font-medium">{email.linked_project.name}</span>
                  )}
                  {email.linked_person && (
                    <span className="text-xs bg-muted text-muted-foreground/80 px-2.5 py-1 rounded-full border border-border/60 font-medium">{email.linked_person.name}</span>
                  )}
                </div>
              </div>
            )}
          </div>
          <div className="border-t border-border/40 pt-4">
            <div className="prose prose-sm max-w-none prose-p:text-muted-foreground">
              <pre className="whitespace-pre-wrap text-sm leading-relaxed">
                {email.body_summary || 'No preview available.'}
              </pre>
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
