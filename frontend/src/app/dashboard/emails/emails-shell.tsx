'use client';

import { useState, useMemo } from 'react';
import { EmailTabs } from '@/components/emails/email-tabs';
import { EmailsInboxTable } from '@/components/emails/emails-inbox-table';
import { EmailFilters } from '@/components/emails/email-filters';
import { PendingTasksList } from '@/components/emails/pending-tasks-list';
import { DraftsList } from '@/components/emails/drafts-list';
import { EmailDetailSheet } from '@/components/emails/email-detail-sheet';
import { EmailStats } from '@/components/emails/email-stats';
import type { Email, EmailFilters as EmailFiltersType, EmailPendingTask, EmailDraft, EmailStats as EmailStatsData } from '@/lib/emails/types';

function filterEmails(emails: Email[], filters: EmailFiltersType): Email[] {
  return emails.filter((email) => {
    if (filters.classification !== 'all' && email.classification !== filters.classification) return false;
    if (filters.source !== 'all' && email.source !== filters.source) return false;
    if (filters.search) {
      const q = filters.search.toLowerCase();
      if (!email.subject?.toLowerCase().includes(q) &&
          !email.sender_email?.toLowerCase().includes(q) &&
          !email.sender?.toLowerCase().includes(q)) return false;
    }
    return true;
  });
}

export function EmailsShell({
  initialEmails,
  initialStats,
  initialPendingTasks,
  initialDrafts,
  initialStatsLoading,
}: {
  initialEmails: Email[];
  initialStats: EmailStatsData | null;
  initialPendingTasks: EmailPendingTask[];
  initialDrafts: EmailDraft[];
  initialStatsLoading: boolean;
}) {
  const [activeTab, setActiveTab] = useState<'inbox' | 'pending' | 'drafts'>('inbox');
  const [emailFilters, setEmailFilters] = useState<EmailFiltersType>({
    classification: 'all',
    source: 'all',
    search: '',
  });
  const [selectedEmail, setSelectedEmail] = useState<Email | null>(null);
  const [isSheetOpen, setIsSheetOpen] = useState(false);

  const filteredEmails = useMemo(
    () => filterEmails(initialEmails, emailFilters),
    [initialEmails, emailFilters]
  );

  const handleEmailClick = (email: Email) => {
    setSelectedEmail(email);
    setIsSheetOpen(true);
  };

  const handleSheetOpenChange = (open: boolean) => {
    setIsSheetOpen(open);
    if (!open) setSelectedEmail(null);
  };

  return (
    <div className="p-4 md:p-6">
      <h1 className="text-2xl font-bold tracking-tight">Emails</h1>
      <p className="text-sm text-muted-foreground/70 mt-0.5">Ingested from Gmail and Outlook</p>
      <EmailStats stats={initialStats} loading={initialStatsLoading} />
      <EmailTabs
        activeTab={activeTab}
        onTabChange={setActiveTab}
        inboxCount={initialStats?.total || 0}
        pendingCount={initialStats?.pending_tasks || 0}
        draftsCount={initialStats?.pending_drafts || 0}
      />
      {activeTab === 'inbox' && (
        <>
          <EmailFilters filters={emailFilters} onFiltersChange={setEmailFilters} />
          <EmailsInboxTable
            emails={filteredEmails}
            loading={false}
            onEmailClick={handleEmailClick}
          />
        </>
      )}
      {activeTab === 'pending' && (
        <PendingTasksList tasks={initialPendingTasks} loading={false} />
      )}
      {activeTab === 'drafts' && (
        <DraftsList drafts={initialDrafts} loading={false} />
      )}
      <EmailDetailSheet
        open={isSheetOpen}
        onOpenChange={handleSheetOpenChange}
        email={selectedEmail}
      />
    </div>
  );
}
