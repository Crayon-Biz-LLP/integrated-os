-- 103_doc_conformance.sql
-- Conformance for documents channel: fix owner_id default and add RLS policies

-- 1. Fix zero-UUID default on document_items
ALTER TABLE public.document_items 
ALTER COLUMN owner_id DROP DEFAULT;

-- 2. RLS Policies for documents
ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can only select their own documents"
    ON public.documents FOR SELECT
    USING (auth.uid() = owner_id);

CREATE POLICY "Users can only insert their own documents"
    ON public.documents FOR INSERT
    WITH CHECK (auth.uid() = owner_id);

CREATE POLICY "Users can only update their own documents"
    ON public.documents FOR UPDATE
    USING (auth.uid() = owner_id);

CREATE POLICY "Users can only delete their own documents"
    ON public.documents FOR DELETE
    USING (auth.uid() = owner_id);

-- 3. RLS Policies for document_items
ALTER TABLE public.document_items ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can only select their own document items"
    ON public.document_items FOR SELECT
    USING (auth.uid()::text = owner_id);

CREATE POLICY "Users can only insert their own document items"
    ON public.document_items FOR INSERT
    WITH CHECK (auth.uid()::text = owner_id);

CREATE POLICY "Users can only update their own document items"
    ON public.document_items FOR UPDATE
    USING (auth.uid()::text = owner_id);

CREATE POLICY "Users can only delete their own document items"
    ON public.document_items FOR DELETE
    USING (auth.uid()::text = owner_id);

