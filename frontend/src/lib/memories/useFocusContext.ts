'use client';

import { useState, useCallback, useRef } from 'react';

/**
 * Focus mode state machine for the NeuralDisc graph view.
 *
 * Modes:
 *   overview    – ambient graph, no recentering, ranked anchor labels only
 *   soft-focus  – single-click; dim non-connected; highlight neighbourhood;
 *                 open panel; DO NOT recenter (spatial continuity preserved)
 *   ego-focus   – double-click same node OR explicit mode action; recenters
 *                 selected node for deep exploration
 *   detail      – panel fully open, same spatial context as entry mode
 *
 * Transitions:
 *   overview  → soft-focus   single click on any node
 *   soft-focus → ego-focus   double click same node (or triggerEgoFocus())
 *   soft-focus → soft-focus  click different node (neighborhood swap)
 *   soft-focus → detail      repeat click on focused node, or openDetail()
 *   ego-focus  → detail      repeat click on focused node, or openDetail()
 *   detail → soft-focus      closeDetail() if entered via soft-focus
 *   detail → ego-focus       closeDetail() if entered via ego-focus
 *   any → overview           reset() or background click
 */

export type ViewMode = 'overview' | 'soft-focus' | 'ego-focus' | 'detail';

export interface FocusState {
  viewMode: ViewMode;
  focusedNodeId: string | null;
  /** true when the panel is showing full memory detail */
  isPanelOpen: boolean;
  /** mode that was active before entering detail (for clean exit) */
  preFocusMode: 'soft-focus' | 'ego-focus' | null;
}

export interface FocusDerived {
  /** Should the graph recenter on focusedNodeId? */
  shouldRecenter: boolean;
  /** Alpha for non-connected nodes (0–1) */
  dimAlpha: number;
  /** Alpha for non-connected edges (0–1) */
  dimEdgeAlpha: number;
  /** Max labels to show at current zoom thresholds */
  labelScope: 'anchor' | 'neighborhood' | 'all';
  /** Whether particle effects should run */
  showParticles: boolean;
  /** Whether edge curves should be used */
  useCurvedEdges: boolean;
}

export interface FocusContextValue {
  state: FocusState;
  derived: FocusDerived;
  /** Call when user single-clicks a node */
  clickNode: (nodeId: string) => void;
  /** Call when user double-clicks a node (explicit ego-focus) */
  doubleClickNode: (nodeId: string) => void;
  /** Call when panel "Focus" button is used for ego-focus */
  triggerEgoFocus: (nodeId: string) => void;
  /** Open full memory detail for current focused node */
  openDetail: () => void;
  /** Close panel, return to prior focus mode */
  closeDetail: () => void;
  /** Return to overview (background click, Escape, reset) */
  reset: () => void;
}

function derivedFromState(state: FocusState): FocusDerived {
  const { viewMode } = state;
  switch (viewMode) {
    case 'overview':
      return {
        shouldRecenter: false,
        dimAlpha: 1.0,       // no dimming in overview – ambient field
        dimEdgeAlpha: 1.0,
        labelScope: 'anchor',
        showParticles: false,
        useCurvedEdges: false,
      };
    case 'soft-focus':
      return {
        shouldRecenter: false,   // KEY: no teleport on single click
        dimAlpha: 0.15,
        dimEdgeAlpha: 0.04,
        labelScope: 'neighborhood',
        showParticles: false,    // quiet in soft-focus
        useCurvedEdges: true,
      };
    case 'ego-focus':
      return {
        shouldRecenter: true,    // explicit user intent → recenter
        dimAlpha: 0.12,
        dimEdgeAlpha: 0.03,
        labelScope: 'neighborhood',
        showParticles: true,     // full effects in ego-focus
        useCurvedEdges: true,
      };
    case 'detail':
      return {
        shouldRecenter: false,   // preserve spatial context from entry mode
        dimAlpha: 0.12,
        dimEdgeAlpha: 0.03,
        labelScope: 'neighborhood',
        showParticles: false,    // calm while reading panel
        useCurvedEdges: true,
      };
  }
}

const INITIAL_STATE: FocusState = {
  viewMode: 'overview',
  focusedNodeId: null,
  isPanelOpen: false,
  preFocusMode: null,
};

export function useFocusContext(): FocusContextValue {
  const [state, setState] = useState<FocusState>(INITIAL_STATE);
  /** Track timestamp of last click per node for double-click detection */
  const lastClickRef = useRef<{ nodeId: string; time: number } | null>(null);
  const DOUBLE_CLICK_MS = 400;

  const clickNode = useCallback((nodeId: string) => {
    const now = Date.now();
    const last = lastClickRef.current;

    // Double-click detection
    if (last && last.nodeId === nodeId && now - last.time < DOUBLE_CLICK_MS) {
      lastClickRef.current = null;
      // Promote to ego-focus
      setState((prev) => ({
        viewMode: 'ego-focus',
        focusedNodeId: nodeId,
        isPanelOpen: prev.isPanelOpen,
        preFocusMode: null,
      }));
      return;
    }

    lastClickRef.current = { nodeId, time: now };

    setState((prev) => {
      // Already focused on this node → open detail
      if (
        prev.focusedNodeId === nodeId &&
        (prev.viewMode === 'soft-focus' || prev.viewMode === 'ego-focus')
      ) {
        return {
          ...prev,
          viewMode: 'detail',
          isPanelOpen: true,
          preFocusMode: prev.viewMode as 'soft-focus' | 'ego-focus',
        };
      }

      // New node single-click → soft-focus (no recenter)
      return {
        viewMode: 'soft-focus',
        focusedNodeId: nodeId,
        isPanelOpen: true,
        preFocusMode: null,
      };
    });
  }, []);

  const doubleClickNode = useCallback((nodeId: string) => {
    lastClickRef.current = null;
    setState((prev) => ({
      viewMode: 'ego-focus',
      focusedNodeId: nodeId,
      isPanelOpen: prev.isPanelOpen,
      preFocusMode: null,
    }));
  }, []);

  const triggerEgoFocus = useCallback((nodeId: string) => {
    lastClickRef.current = null;
    setState((prev) => ({
      viewMode: 'ego-focus',
      focusedNodeId: nodeId,
      isPanelOpen: prev.isPanelOpen,
      preFocusMode: null,
    }));
  }, []);

  const openDetail = useCallback(() => {
    setState((prev) => {
      if (prev.viewMode === 'detail') return prev;
      return {
        ...prev,
        viewMode: 'detail',
        isPanelOpen: true,
        preFocusMode: prev.viewMode === 'soft-focus' || prev.viewMode === 'ego-focus'
          ? prev.viewMode
          : null,
      };
    });
  }, []);

  const closeDetail = useCallback(() => {
    setState((prev) => {
      if (prev.viewMode !== 'detail') return { ...prev, isPanelOpen: false };
      return {
        ...prev,
        viewMode: prev.preFocusMode ?? 'soft-focus',
        isPanelOpen: false,
        preFocusMode: null,
      };
    });
  }, []);

  const reset = useCallback(() => {
    lastClickRef.current = null;
    setState(INITIAL_STATE);
  }, []);

  return {
    state,
    derived: derivedFromState(state),
    clickNode,
    doubleClickNode,
    triggerEgoFocus,
    openDetail,
    closeDetail,
    reset,
  };
}
