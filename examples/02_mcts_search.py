"""
Example 02: High-Throughput Tree Search / MCTS Branching
Demonstrates multi-worker branch identity deduplication and search.
"""

from sts2_native_sim import NativeSearchCoordinator, NativeWorkerPool

def main():
    print("Starting 2-worker pool for tree search...")
    with NativeWorkerPool(workers=2) as pool:
        coordinator = NativeSearchCoordinator(pool)
        reset_request = {
            "seed": "EXAMPLE_SEARCH_SEED_123",
            "character": "IRONCLAD",
            "ascension": 1,
            "encounter": "first",
        }
        print("Evaluating 2-ply search on root state...")
        search_result = coordinator.search(
            reset_request=reset_request,
            max_depth=2,
            max_nodes=8,
        )
        print("Search completed!")
        print(f"  Best Action: {search_result.get('best_action')}")
        print(f"  Nodes Evaluated: {search_result.get('nodes_evaluated')}")
        print(f"  Duplicate Branch Hits: {search_result.get('duplicate_branch_hits')}")

if __name__ == "__main__":
    main()
