export const dynamic = 'force-dynamic';
'use client';

import { useState, useEffect } from 'react';
import { EmailStats } from '@/components/emails/email-stats';
import { EmailTabs } from '@/components/emails/email-tabs';
import { EmailsInboxTable } from '@/components/emails/emails-inbox-table';
import { EmailFilters } from '@/components/emails/email-filters';
import { PendingTasksList } from '@/components/emails/pending-tasks-list';
import { DraftsList } from '@/components/emails/drafts-list';
import { EmailDetailSheet } from '@/components/emails/email-detail-sheet';
import { fetchEmails, fetchPendingTasks, fetchPendingDrafts, fetchEmailStats } from '@/lib/emails/api';
import type { Email, EmailFilters as EmailFiltersType, EmailPendingTask, EmailDraft, EmailStats as EmailStatsData } from '@/lib/emails/types';

export default function EmailsPage() {
  const [activeTab, setActiveTab] = useState<'inbox' | 'pending' | 'drafts'>('inbox');
  const [emails, setEmails] = useState<Email[]>([]);
  const [emailsLoading, setEmailsLoading] = useState(true);
  const [emailFilters, setEmailFilters] = useState<EmailFiltersType>({
    classification: 'all',
    source: 'all',
    search: '',
  });
  const [pendingTasks, setPendingTasks] = useState<EmailPendingTask[]>([]);
  const [pendingTasksLoading, setPendingTasksLoading] = useState(true);
  const [drafts, setDrafts] = useState<EmailDraft[]>([]);
  const [draftsLoading, setDraftsLoading] = useState(true);
  const [emailStats, setEmailStats] = useState<EmailStatsData | null>(null);
  const [selectedEmail, setSelectedEmail] = useState<Email | null>(null);
  const [isSheetOpen, setIsSheetOpen] = useState(false);

  // On mount: fetch non-filter dependent data (pending tasks, drafts, stats)
  useEffect(() => {
    const loadNonFilterData = async () => {
      try {
        const [tasksData, draftsData, statsData] = await Promise.all([
          fetchPendingTasks(),
          fetchPendingDrafts(),
          fetchEmailStats(),
        ]);
        setPendingTasks(tasksData);
        setDrafts(draftsData);
        setEmailStats(statsData);
      } catch (error) {
        console.error('Failed to load non-filter data:', error);
      } finally {
        setPendingTasksLoading(false);
        setDraftsLoading(false);
      }
    };
    loadNonFilterData();
  }, []);

  // On email filter change: fetch only emails
  useEffect(() => {
    const loadEmails = async () => {
      setEmailsLoading(true);
      try {
        const emailsData = await fetchEmails(emailFilters);
        setEmails(emailsData);
      } catch (error) {
        console.error('Failed to load emails:', error);
      } finally {
        setEmailsLoading(false);
      }
    };
    loadEmails();
  }, [emailFilters]);

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
      <EmailStats />
      <EmailTabs
        activeTab={activeTab}
        onTabChange={setActiveTab}
        inboxCount={emailStats?.total || 0}
        pendingCount={emailStats?.pending_tasks || 0}
        draftsCount={emailStats?.pending_drafts || 0}
      />
      {activeTab === 'inbox' && (
        <>
          <EmailFilters filters={emailFilters} onFiltersChange={setEmailFilters} />
          <EmailsInboxTable
            emails={emails}
            loading={emailsLoading}
            onEmailClick={handleEmailClick}
          />
        </>
      )}
      {activeTab === 'pending' && (
        <PendingTasksList tasks={pendingTasks} loading={pendingTasksLoading} />
      )}
      {activeTab === 'drafts' && (
        <DraftsList drafts={drafts} loading={draftsLoading} />
      )}
      <EmailDetailSheet
        open={isSheetOpen}
        onOpenChange={handleSheetOpenChange}
        email={selectedEmail}
      />
    </div>
  );
}
