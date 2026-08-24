"""Package victorious combat decisions as a self-contained retrieval model."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import torch
from expert_combat_retriever import compile_examples
from train_v10_combat_policy import open_text

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--shards",nargs="+",required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    rows=[]
    for path in args.shards:
        with open_text(path) as source: rows.extend(json.loads(line) for line in source if line.strip())
    examples=compile_examples(rows);args.output.parent.mkdir(parents=True,exist_ok=True)
    torch.save({"architecture":"expert_retriever_v1","mechanics_source":"exact_victorious_game_trajectories","examples":examples},args.output)
    print(json.dumps({"examples":len(examples),"encounters":len({tuple(x['enemy_ids']) for x in examples}),"output":str(args.output.resolve())}))
if __name__=="__main__":main()
