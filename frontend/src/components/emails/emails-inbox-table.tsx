'use client';

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import type { Email } from '@/lib/emails/types';
import { formatDistanceToNow, parseISO } from 'date-fns';
import { User, Building } from 'lucide-react';
import { cn } from '@/lib/utils';

interface EmailsInboxTableProps {
  emails: Email[];
  loading: boolean;
  onEmailClick: (email: Email) => void;
}

const CLASSIFICATION_CONFIG = {
  actionable: { className: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
  fyi: { className: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  ignored: { className: 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30' },
} as const;

export function EmailsInboxTable({ emails, loading, onEmailClick }: EmailsInboxTableProps) {
  const renderClassification = (classification: Email['classification']) => {
    const config = CLASSIFICATION_CONFIG[classification as keyof typeof CLASSIFICATION_CONFIG];
    if (!config) {
      return <Badge variant="outline" className="text-xs">Unknown</Badge>;
    }
    return (
      <Badge variant="outline" className={cn('text-xs', config.className)}>
        {classification}
      </Badge>
    );
  };

  const renderSource = (source: Email['source']) => (
    <Badge variant="outline" className="text-xs capitalize">{source}</Badge>
  );

  const renderRelativeTime = (dateStr: string) => {
    try {
      return formatDistanceToNow(parseISO(dateStr), { addSuffix: true });
    } catch {
      return 'Invalid date';
    }
  };

  if (loading) {
    return (
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {['Sender', 'Subject', 'Classification', 'Source', 'Project', 'Received'].map((col) => (
                <TableHead key={col}>{col}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {[...Array(6)].map((_, i) => (
              <TableRow key={i}>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <Skeleton className="h-4 w-4 rounded-full" />
                    <div className="space-y-1">
                      <Skeleton className="h-4 w-[140px]" />
                      <Skeleton className="h-3 w-[100px]" />
                    </div>
                  </div>
                </TableCell>
                <TableCell><Skeleton className="h-4 w-[200px]" /></TableCell>
                <TableCell><Skeleton className="h-5 w-[80px] rounded-4xl" /></TableCell>
                <TableCell><Skeleton className="h-5 w-[60px] rounded-4xl" /></TableCell>
                <TableCell><Skeleton className="h-5 w-[90px] rounded-4xl" /></TableCell>
                <TableCell><Skeleton className="h-4 w-[80px]" /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    );
  }

  if (emails.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        No emails found. Adjust filters or wait for next ingest run.
      </div>
    );
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Sender</TableHead>
            <TableHead>Subject</TableHead>
            <TableHead>Classification</TableHead>
            <TableHead>Source</TableHead>
            <TableHead>Project</TableHead>
            <TableHead>Received</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {emails.map((email) => (
            <TableRow key={email.id} onClick={() => onEmailClick(email)} className="cursor-pointer hover:bg-zinc-800/50">
              <TableCell>
                <div className="flex items-center gap-2">
                  <User className="h-4 w-4 text-muted-foreground" />
                  <div>
<div className="text-sm font-medium">{email.sender || email.sender_email}</div>
                     {email.sender && <div className="text-xs text-muted-foreground">{email.sender_email}</div>}
                  </div>
                </div>
              </TableCell>
              <TableCell className="max-w-[200px] truncate">{email.subject}</TableCell>
              <TableCell>{renderClassification(email.classification)}</TableCell>
              <TableCell>{renderSource(email.source)}</TableCell>
              <TableCell>
                {email.linked_project?.name ? (
                  <Badge variant="outline" className="text-xs">
                    <Building className="h-3 w-3 mr-1" />
                    {email.linked_project.name}
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-sm text-muted-foreground">{renderRelativeTime(email.received_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
