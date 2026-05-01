"use client";

import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "sonner";
import type { EmailDraft } from "@/lib/emails/types";
import { approveDraft, rejectDraft, updateDraftBody } from "@/lib/emails/api";
import { Loader2, Send, X, Edit, Globe } from "lucide-react";

interface DraftsListProps {
  drafts: EmailDraft[];
  loading: boolean;
}

export function DraftsList({
  drafts: initialDrafts,
  loading,
}: DraftsListProps) {
  const [drafts, setDrafts] = useState<EmailDraft[]>(initialDrafts);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editBody, setEditBody] = useState("");
  const [sendingId, setSendingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDrafts(initialDrafts);
  }, [initialDrafts]);

  const startEditing = (draft: EmailDraft) => {
    setEditingId(draft.id);
    setEditBody(draft.draft_body);
  };

  const saveEdit = async (id: number) => {
    try {
      await updateDraftBody(id, editBody);
      setDrafts((prev) =>
        prev.map((d) => (d.id === id ? { ...d, draft_body: editBody } : d)),
      );
      setEditingId(null);
    } catch (err) {
      console.error("Failed to update draft:", err);
    }
  };

  const handleReject = async (id: number) => {
    setDrafts((prev) => prev.filter((d) => d.id !== id));
    try {
      await rejectDraft(id);
    } catch (err) {
      console.error("Failed to reject draft:", err);
    }
  };

  const handleSend = async (id: number) => {
    setSendingId(id);
    setError(null);
    try {
      const res = await approveDraft(id);
      if (res.success) {
        const draft = drafts.find((d) => d.id === id);
        setDrafts((prev) => prev.filter((d) => d.id !== id));
        toast.success("Draft sent", {
          description: `Sent via ${draft?.email?.source === "gmail" ? "Gmail" : "Outlook"}`,
        });
      } else {
        setError(res.error || "Failed to send draft");
      }
    } catch (err) {
      setError("Failed to send draft");
    } finally {
      setSendingId(null);
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        {[1, 2, 3].map((_, i) => (
          <Skeleton key={i} className="h-48 rounded-lg" />
        ))}
      </div>
    );
  }

  if (drafts.length === 0) {
    return (
      <div className="rounded-md border p-8 text-center text-muted-foreground">
        No drafts awaiting review.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/20 p-3 text-sm text-destructive">
          {error}
        </div>
      )}
      {drafts.map((draft) => (
        <div
          key={draft.id}
          className="card-premium p-4 flex flex-col gap-2 cursor-pointer"
        >
          <div className="pb-2">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium tracking-tight">
                To: {draft.email?.sender || draft.email?.sender_email}
              </div>
              <span className="text-xs bg-muted/60 text-muted-foreground px-2 py-0.5 rounded font-mono">
                <Globe className="h-3 w-3 mr-1" />
                {draft.email?.source}
              </span>
            </div>
            <div className="text-sm text-muted-foreground/80">
              Re: {draft.email?.subject}
            </div>
          </div>
          <div className="border border-border/40 rounded-md p-3 mb-4 max-h-48 overflow-y-auto text-sm">
            {editingId === draft.id ? (
              <Textarea
                value={editBody}
                onChange={(e) => setEditBody(e.target.value)}
                className="font-mono text-sm min-h-[100px]"
              />
            ) : (
              <div className="whitespace-pre-wrap text-sm leading-relaxed text-muted-foreground/80">
                {draft.draft_body}
              </div>
            )}
          </div>
          {editingId === draft.id ? (
            <div className="flex gap-2 mb-4">
              <Button size="sm" onClick={() => saveEdit(draft.id)}>
                Save
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setEditingId(null)}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              className="mb-4"
              onClick={() => startEditing(draft)}
            >
              <Edit className="h-4 w-4 mr-1" />
              Edit
            </Button>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              className="bg-primary hover:bg-primary/90"
              onClick={() => handleSend(draft.id)}
              disabled={sendingId === draft.id}
            >
              {sendingId === draft.id ? (
                <Loader2 className="h-4 w-4 mr-1 animate-spin" />
              ) : (
                <Send className="h-4 w-4 mr-1" />
              )}
              Send
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive hover:bg-destructive/10"
              onClick={() => handleReject(draft.id)}
              disabled={sendingId === draft.id}
            >
              <X className="h-4 w-4 mr-1" />
              Reject
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
