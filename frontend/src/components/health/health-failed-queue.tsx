'use client';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { FailedQueueItem } from '@/lib/health/types';

interface FailedQueueTableProps {
  items: FailedQueueItem[];
}

export function FailedQueueTable({ items }: FailedQueueTableProps) {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">No failed queue items.</p>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Source</TableHead>
          <TableHead>Operation</TableHead>
          <TableHead>Error</TableHead>
          <TableHead>Retries</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {items.map((item) => (
          <TableRow key={item.id}>
            <TableCell className="font-mono text-xs">{item.source_table}</TableCell>
            <TableCell className="text-xs">{item.operation}</TableCell>
            <TableCell className="text-xs max-w-[300px] truncate">{item.error_message}</TableCell>
            <TableCell className="text-xs">{item.retry_count}</TableCell>
            <TableCell className="text-xs">{new Date(item.created_at).toLocaleString()}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
