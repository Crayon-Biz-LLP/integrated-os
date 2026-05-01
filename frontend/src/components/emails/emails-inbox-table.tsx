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
  actionable: { className: 'bg-primary/10 text-primary border border-primary/20 text-xs px-2 py-0.5 rounded-full font-medium' },
  fyi: { className: 'bg-blue-500/10 text-blue-600 border border-blue-500/20 text-xs px-2 py-0.5 rounded-full font-medium' },
  ignored: { className: 'bg-muted text-muted-foreground border border-border text-xs px-2 py-0.5 rounded-full' },
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
    <span className="text-xs bg-muted/60 text-muted-foreground px-2 py-0.5 rounded font-mono">{source}</span>
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
      <div className="card-premium overflow-hidden mt-4">
        <Table>
          <TableHeader className="bg-muted/40 border-b border-border">
            <TableRow>
              {['Sender', 'Subject', 'Classification', 'Source', 'Project', 'Received'].map((col) => (
                <TableHead key={col} className="section-label py-3 px-4 text-left">{col}</TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {[1,2,3,4,5,6].map((_, i) => (
              <TableRow key={i} className="border-b border-border/40">
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
                <TableCell><Skeleton className="h-5 w-[80px] rounded-full" /></TableCell>
                <TableCell><Skeleton className="h-5 w-[60px] rounded-full" /></TableCell>
                <TableCell><Skeleton className="h-5 w-[90px] rounded-full" /></TableCell>
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
      <div className="card-premium p-8 text-center text-muted-foreground mt-4">
        No emails found. Adjust filters or wait for next ingest run.
      </div>
    );
  }

  return (
    <div className="card-premium overflow-hidden mt-4">
      <Table>
        <TableHeader className="bg-muted/40 border-b border-border">
          <TableRow>
            <TableHead className="section-label py-3 px-4 text-left">Sender</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Subject</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Classification</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Source</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Project</TableHead>
            <TableHead className="section-label py-3 px-4 text-left">Received</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {emails.map((email) => (
            <TableRow key={email.id} onClick={() => onEmailClick(email)} className="border-b border-border/40 transition-all duration-150 hover:bg-primary/3 cursor-pointer group">
              <TableCell>
                <div className="flex items-center gap-2">
                  <User className="h-4 w-4 text-muted-foreground" />
                  <div>
                    <div className="text-sm font-medium text-foreground">{email.sender || email.sender_email}</div>
                    {email.sender && <div className="text-xs text-muted-foreground/60 font-mono">{email.sender_email}</div>}
                  </div>
                </div>
              </TableCell>
              <TableCell className="max-w-[200px] truncate text-sm text-foreground/80">{email.subject}</TableCell>
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
              <TableCell className="text-xs text-muted-foreground/60 font-mono">{renderRelativeTime(email.received_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
