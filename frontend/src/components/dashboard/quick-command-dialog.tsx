'use client';

import { useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { sendMessage } from '@/lib/messages/api';
import { toast } from 'sonner';

const MODES = {
  query: {
    title: 'Ask a Question',
    description: 'Query your knowledge base — tasks, people, projects, and memories.',
    placeholder: 'e.g. What am I working on this week?',
    prefix: '',
  },
  note: {
    title: 'Quick Note',
    description: 'Save a note to your memory graph.',
    placeholder: 'e.g. Discussed Q3 roadmap with the team',
    prefix: 'N: ',
  },
  task: {
    title: 'New Task',
    description: 'Describe what needs to be done. Be as specific as you like.',
    placeholder: 'e.g. Review the Q3 budget by Friday high priority',
    prefix: '',
  },
} as const;

type CommandMode = keyof typeof MODES;

export function QuickCommandDialog({
  mode,
  open,
  onOpenChange,
}: {
  mode: CommandMode;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const config = MODES[mode];

  const handleSubmit = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setSending(true);
    try {
      const message = config.prefix + text;
      await sendMessage(message);
      toast.success(mode === 'task' ? 'Task created' : mode === 'note' ? 'Note saved' : 'Query sent');
      setInput('');
      onOpenChange(false);
    } catch {
      toast.error('Failed to send');
    } finally {
      setSending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton>
        <DialogHeader>
          <DialogTitle>{config.title}</DialogTitle>
          <DialogDescription>{config.description}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSubmit()}
            placeholder={config.placeholder}
            autoFocus
            className="w-full rounded-lg border border-border bg-background px-4 py-2.5 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/40 transition-all duration-150"
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button size="sm" onClick={handleSubmit} disabled={!input.trim() || sending}>
              {sending ? 'Sending...' : 'Send'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
