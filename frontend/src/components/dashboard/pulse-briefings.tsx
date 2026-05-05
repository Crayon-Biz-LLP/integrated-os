'use client';

import { Message } from '@/lib/messages/types';
import { fetchMessages } from '@/lib/messages/api';
import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';

// Safe metadata parser
const parseMetadata = (meta: string | Record<string, any>): Record<string, any> => {
  if (typeof meta === 'object' && meta !== null) return meta;
  if (typeof meta !== 'string') return {};
  try {
    return JSON.parse(meta);
  } catch {
    return {};
  }
};

export function PulseBriefings() {
  const [briefings, setBriefings] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const loadBriefings = async () => {
      try {
        const allMessages = await fetchMessages(50);
        // Filter only briefings
        const briefingsOnly = allMessages.filter(
          (m) => m.message_type === 'briefing' || m.message_type === 'response'
        );
        setBriefings(briefingsOnly.slice(0, 3)); // Show last 3
      } catch (error) {
        console.error('Failed to load briefings:', error);
      } finally {
        setLoading(false);
      }
    };
    loadBriefings();
  }, []);

  if (loading) {
    return (
      <div className="card-premium p-6">
        <h2 className="text-xl font-semibold mb-4">📚 Pulse Briefings</h2>
        <div className="space-y-3">
          {[1, 2].map((i) => (
            <div key={i} className="h-16 rounded-lg border bg-muted/20 animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (briefings.length === 0) {
    return (
      <div className="card-premium p-6">
        <h2 className="text-xl font-semibold mb-4">📚 Pulse Briefings</h2>
        <p className="text-sm text-muted-foreground">No briefings yet.</p>
      </div>
    );
  }

  return (
    <div className="card-premium p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">📚 Pulse Briefings</h2>
        <a 
          href="/dashboard/messages?filter=briefing"
          className="inline-flex items-center justify-center rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted"
        >
          View All →
        </a>
      </div>
      
      <div className="space-y-3">
        {briefings.map((briefing) => {
          const metadata = parseMetadata(briefing.metadata);
          const isSystem = briefing.sender === 'system';
          const contentPreview = briefing.content.length > 150 
            ? briefing.content.substring(0, 150) + '...' 
            : briefing.content;
          
          return (
            <div key={briefing.id} className="p-4 bg-muted/30 rounded-lg">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[10px] font-medium text-muted-foreground">
                  {isSystem ? 'Rhodey' : 'You'}
                </span>
                <span className="text-[10px] font-mono text-muted-foreground/50">
                  {new Date(briefing.created_at).toLocaleTimeString('en-US', { 
                    hour: 'numeric', 
                    minute: '2-digit' 
                  })}
                </span>
              </div>
              <p className="text-sm whitespace-pre-wrap break-words leading-relaxed">
                {contentPreview}
              </p>
              {briefing.content.length > 150 && (
                <a 
                  href={`/dashboard/messages?filter=briefing`}
                  className="text-xs text-primary hover:underline mt-2 inline-block"
                >
                  Read more →
                </a>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
