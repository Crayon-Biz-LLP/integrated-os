import { createClient } from '@/lib/supabase';
import { CanonicalPage, CanonicalPageListItem } from './types';

const PAGE_LIST_COLUMNS = 'id,title,project_id,source_count,last_synth_at,updated_at,is_sparse,category';
const PAGE_DETAIL_COLUMNS = 'id,title,content,project_id,source_count,last_synth_at,updated_at,is_sparse,category';

export async function fetchPagesList(): Promise<CanonicalPageListItem[]> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('canonical_pages')
    .select(PAGE_LIST_COLUMNS)
    .order('updated_at', { ascending: false });

  if (error) throw error;
  return data as CanonicalPageListItem[];
}

export async function fetchPageById(id: number): Promise<CanonicalPage | null> {
  const supabase = createClient();
  const { data, error } = await supabase
    .from('canonical_pages')
    .select(PAGE_DETAIL_COLUMNS)
    .eq('id', id)
    .single();

  if (error) throw error;
  return data as CanonicalPage;
}
