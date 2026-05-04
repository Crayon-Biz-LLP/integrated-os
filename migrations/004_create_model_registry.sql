-- Migration: Create model_registry table
-- Purpose: Track LLM model versions and performance metrics
-- Run this in Supabase Dashboard > SQL Editor

CREATE TABLE IF NOT EXISTS model_registry (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    model_name TEXT NOT NULL,
    provider TEXT NOT NULL,  -- 'gemini', 'openrouter', 'gemma'
    version TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Index for quick lookups
CREATE INDEX IF NOT EXISTS idx_model_registry_model 
ON model_registry(model_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_model_registry_provider 
ON model_registry(provider, created_at DESC);

-- Comments
COMMENT ON TABLE model_registry IS 'Tracks LLM model usage, performance, and versions';
COMMENT ON COLUMN model_registry.model_name IS 'e.g., gemini-3-flash-preview, gemma-3-27b-it';
COMMENT ON COLUMN model_registry.provider IS 'gemini, openrouter, gemma';
COMMENT ON COLUMN model_registry.version IS 'Model version tag if available';
COMMENT ON COLUMN model_registry.input_tokens IS 'Tokens in prompt';
COMMENT ON COLUMN model_registry.output_tokens IS 'Tokens in response';
COMMENT ON COLUMN model_registry.latency_ms IS 'Response time in milliseconds';
COMMENT ON COLUMN model_registry.success IS 'Whether the call succeeded';
COMMENT ON COLUMN model_registry.error_message IS 'Error details if failed';
