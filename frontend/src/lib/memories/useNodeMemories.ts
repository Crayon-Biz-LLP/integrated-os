'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import type { StreamItem } from './stream';

interface NodeMemoriesState {
  items: StreamItem[];
  loading: boolean;
  error: string | null;
}

const EMPTY: NodeMemoriesState = { items: [], loading: false, error: null };
const LOADING: NodeMemoriesState = { items: [], loading: true, error: null };

/**
 * Sequence-guarded hook for fetching the memory stream for a given graph node.
 *
 * Mirrors the abort-controller + sequence-guard pattern used in graph/page.tsx.
 * Fetching is triggered via `load(nodeId)` — the hook is not auto-triggered
 * so the parent controls timing (consistent with the page's async orchestration).
 *
 * Clears previous data immediately when a new load begins, so the panel
 * never shows stale memories from a prior node.
 */
export function useNodeMemories(): {
  state: NodeMemoriesState;
  load: (nodeId: string) => Promise<void>;
  clear: () => void;
} {
  const [state, setState] = useState<NodeMemoriesState>(EMPTY);
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  const clear = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setState(EMPTY);
  }, []);

  const load = useCallback(async (nodeId: string) => {
    // Abort any in-flight request
    if (abortRef.current) abortRef.current.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const seq = ++seqRef.current;

    setState(LOADING);

    try {
      const res = await fetch(
        `/api/memories/stream?node_id=${encodeURIComponent(nodeId)}&limit=20`,
        { cache: 'no-store', signal: controller.signal },
      );
      if (seq !== seqRef.current) return; // stale response

      if (!res.ok) {
        setState({ items: [], loading: false, error: 'Could not load memories.' });
        return;
      }

      const data = await res.json();
      if (seq !== seqRef.current) return;

      setState({ items: data.items ?? [], loading: false, error: null });
    } catch (err: unknown) {
      if (seq !== seqRef.current) return;
      if (err instanceof Error && err.name === 'AbortError') return;
      setState({ items: [], loading: false, error: 'Could not load memories.' });
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }, []);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (abortRef.current) abortRef.current.abort();
    };
  }, []);

  return { state, load, clear };
}
