import json
from dotenv import load_dotenv

load_dotenv()

from scripts.concept_sweep_batch import fetch_unprocessed_memories, extract_concepts_batch  # noqa: E402

def test():
    print("Fetching memories...")
    mems = fetch_unprocessed_memories()
    if not mems:
        print("No memories to process.")
        return
        
    batch = mems[:5]
    print(f"Testing with batch of {len(batch)} memories...")
    
    print("\n--- Extracting Concepts ---")
    results = extract_concepts_batch(batch)
    print("\n--- Raw Result Object ---")
    print(json.dumps(results, indent=2))
    
    print("\n--- Processed ---")
    results_list = results if isinstance(results, list) else (results.get("results", []) if isinstance(results, dict) else [])
    
    res_by_id = {str(r.get("memory_id")): r for r in results_list if isinstance(r, dict)}
    
    for mem in batch:
        mem_id = str(mem['id'])
        r = res_by_id.get(mem_id, {})
        nodes = r.get("nodes", [])
        edges = r.get("edges", [])
        print(f"Memory {mem_id}: {len(nodes)} nodes, {len(edges)} edges")
        if nodes:
            for n in nodes:
                print(f"  Node: {n.get('label')}")

if __name__ == "__main__":
    test()