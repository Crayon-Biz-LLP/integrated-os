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
  actionable: { className: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
  fyi: { className: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  ignored: { className: 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30' },
} as const;

export function EmailDetailSheet({ open, onOpenChange, email }: EmailDetailSheetProps) {
  if (!email) return null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[600px] sm:max-w-[600px]">
        <SheetHeader>
          <SheetTitle className="truncate">{email.subject}</SheetTitle>
          <SheetDescription>
            Received on {format(new Date(email.received_at), 'PPpp')}
          </SheetDescription>
        </SheetHeader>
        <div className="mt-6 space-y-6">
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm">
              <User className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">From:</span>
              <span>{email.sender_name || email.sender_email}</span>
              {email.sender_name && <span className="text-muted-foreground">({email.sender_email})</span>}
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Mail className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">Subject:</span>
              <span>{email.subject}</span>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Tag className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">Classification:</span>
              <Badge variant="outline" className={cn('text-xs', CLASSIFICATION_CONFIG[email.classification].className)}>
                {email.classification}
              </Badge>
            </div>
            <div className="flex items-center gap-2 text-sm">
              <Globe className="h-4 w-4 text-muted-foreground" />
              <span className="font-medium">Source:</span>
              <Badge variant="outline" className="text-xs capitalize">{email.source}</Badge>
            </div>
            {(email.linked_project || email.linked_person) && (
              <div className="flex items-center gap-2 text-sm">
                <Building className="h-4 w-4 text-muted-foreground" />
                <span className="font-medium">Linked:</span>
                <div className="flex gap-2">
                  {email.linked_project && (
                    <Badge variant="outline" className="text-xs">{email.linked_project.name}</Badge>
                  )}
                  {email.linked_person && (
                    <Badge variant="outline" className="text-xs">{email.linked_person.name}</Badge>
                  )}
                </div>
              </div>
            )}
          </div>
          <div className="border-t pt-4">
            <pre className="whitespace-pre-wrap text-sm text-muted-foreground max-h-[400px] overflow-y-auto">
              {email.body_preview || 'No preview available.'}
            </pre>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
