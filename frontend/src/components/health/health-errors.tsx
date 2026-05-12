'use client';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { AuditLogEntry } from '@/lib/health/types';

interface ErrorsTableProps {
  errors: AuditLogEntry[];
}

export function ErrorsTable({ errors }: ErrorsTableProps) {
  if (errors.length === 0) {
    return <p className="text-sm text-muted-foreground">No recent errors.</p>;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Service</TableHead>
          <TableHead>Message</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {errors.map((e, i) => (
          <TableRow key={i}>
            <TableCell className="text-xs whitespace-nowrap">{new Date(e.created_at).toLocaleString()}</TableCell>
            <TableCell className="text-xs font-mono">{e.service}</TableCell>
            <TableCell className="text-xs max-w-[500px] truncate">{e.message}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
