"""Train the complete-state V12 policy on exact successful-run decisions."""

from __future__ import annotations

import argparse
import gzip
import json
import random
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset, Subset

from train_v10_combat_policy import ACTION_TYPE_MAP, CHAR_TO_IDX, normalize_id, open_text
from v12_combat_model import V12CombatPolicyNet


def entity(value: object) -> str:
    return normalize_id(str(value or "UNKNOWN"))


def read_rows(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        with open_text(path) as source:
            rows.extend(json.loads(line) for line in source if line.strip())
    return [row for row in rows if row.get("phase") == "combat"]


def build_vocab(rows: list[dict]) -> dict[str, int]:
    values = set()
    for row in rows:
        combat = (row.get("observation") or {}).get("combat") or {}
        values.update(entity(card.get("card_id")) for card in combat.get("hand") or [])
        values.update(entity(enemy.get("enemy_id")) for enemy in combat.get("enemies") or [])
        for item in combat.get("player_powers") or []:
            values.add(entity(item.get("model_id") or item.get("name")))
        for item in combat.get("relics") or []:
            values.add(entity(item.get("model_id") or item.get("name")))
        for item in combat.get("potions") or []:
            if item.get("occupied"):
                values.add(entity(item.get("model_id") or item.get("name")))
        orbs = combat.get("orbs") or {}
        values.add(entity("ORB_CAPACITY"))
        values.update(entity(orb.get("model_id")) for orb in orbs.get("entries") or [])
        for action in row.get("legal_actions") or []:
            values.add(entity((action.get("metadata") or {}).get("card_id")))
    values.discard("UNKNOWN")
    return {value: index + 1 for index, value in enumerate(sorted(values))}


class CombatDataset(Dataset):
    def __init__(self, rows: list[dict], vocab: dict[str, int]):
        self.samples = []
        idx = lambda value: vocab.get(entity(value), 0)
        for row in rows:
            legal = row.get("legal_actions") or []
            action_ids = [action.get("action_id") for action in legal]
            if row.get("action") not in action_ids or len(legal) > 32:
                continue
            obs = row.get("observation") or {}
            combat = obs.get("combat") or {}
            floor = float(row.get("floor", 1))
            hp = float(obs.get("player_hp", 1)); max_hp = max(1.0, float(obs.get("player_max_hp", 1)))
            enemies = combat.get("enemies") or []
            incoming = sum(
                float(enemy.get("damage", 0)) * max(1.0, float(enemy.get("repeats", 1)))
                for enemy in enemies if enemy.get("is_alive", True)
            )
            context = [
                hp / max_hp, float(obs.get("player_block", 0)) / 50.0,
                float(obs.get("player_energy", 0)) / 5.0, float(combat.get("turn", 1)) / 20.0,
                floor / 50.0, float(combat.get("draw_pile_size", 0)) / 40.0,
                float(combat.get("discard_pile_size", 0)) / 40.0,
                float(combat.get("exhaust_pile_size", 0)) / 40.0,
                float(combat.get("stars", 0)) / 10.0, incoming / 50.0,
                len(enemies) / 5.0, len(combat.get("hand") or []) / 10.0,
            ]
            hands = []
            hand_by_card = {}
            for card in (combat.get("hand") or [])[:10]:
                numeric = [
                    max(-1.0, min(5.0, float(card.get("cost", 0)))) / 5.0,
                    float(int(card.get("upgrades", 0)) > 0), float(card.get("damage", 0)) / 40.0,
                    float(card.get("block", 0)) / 40.0, float(card.get("playable", True)),
                ]
                hands.append((idx(card.get("card_id")), numeric))
                hand_by_card.setdefault(entity(card.get("card_id")), numeric)
            enemy_rows = []
            for slot, enemy in enumerate(enemies[:5], 1):
                enemy_rows.append((idx(enemy.get("enemy_id")), [
                    float(enemy.get("hp", 0)) / max(1.0, float(enemy.get("max_hp", 1))),
                    float(enemy.get("block", 0)) / 50.0, float(enemy.get("damage", 0)) / 40.0,
                    float(enemy.get("repeats", 1)) / 5.0, float(enemy.get("is_alive", True)), float(slot),
                ]))
            aux = []
            for kind, items in enumerate((combat.get("player_powers") or [], combat.get("relics") or [], combat.get("potions") or []), 1):
                for item in items:
                    if kind == 3 and not item.get("occupied"):
                        continue
                    aux.append((idx(item.get("model_id") or item.get("name")), [float(kind) / 3.0, float(item.get("amount") or item.get("stack") or 1) / 10.0]))
            orbs = combat.get("orbs") or {}
            entries = orbs.get("entries") or []
            aux.append((idx("ORB_CAPACITY"), [float(orbs.get("capacity", 0)) / 10.0, len(entries) / 10.0]))
            for orb in entries:
                aux.append((idx(orb.get("model_id")), [float(orb.get("passive", 0)) / 20.0, float(orb.get("evoke", 0)) / 30.0]))
            action_rows = []
            for action in legal:
                meta = action.get("metadata") or {}; card_id = meta.get("card_id")
                numeric = hand_by_card.get(entity(card_id), [0.0] * 5)
                action_rows.append((ACTION_TYPE_MAP.get(action.get("action_type"), 0), idx(card_id), int(meta.get("target_id", 0)), numeric))
            self.samples.append({
                "episode": row.get("episode_id"), "char": CHAR_TO_IDX.get(str(row.get("character", "SILENT")).upper(), 1),
                "context": context, "hands": hands, "enemies": enemy_rows, "aux": aux[:24],
                "actions": action_rows, "label": action_ids.index(row["action"]),
                "weight": float(row.get("advantage_weight", 1.0)),
            })

    def __len__(self): return len(self.samples)
    def __getitem__(self, index): return self.samples[index]


def collate(samples: list[dict]) -> dict[str, torch.Tensor]:
    batch = len(samples); max_hand = 10; max_enemy = 5; max_aux = 24; max_action = 32
    result = {
        "char_idx": torch.zeros(batch, dtype=torch.long), "context": torch.zeros(batch, 12),
        "hand_ids": torch.zeros(batch, max_hand, dtype=torch.long), "hand_numeric": torch.zeros(batch, max_hand, 5),
        "enemy_ids": torch.zeros(batch, max_enemy, dtype=torch.long), "enemy_numeric": torch.zeros(batch, max_enemy, 6),
        "aux_ids": torch.zeros(batch, max_aux, dtype=torch.long), "aux_numeric": torch.zeros(batch, max_aux, 2),
        "state_mask": torch.zeros(batch, 2 + max_hand + max_enemy + max_aux),
        "action_types": torch.zeros(batch, max_action, dtype=torch.long), "action_ids": torch.zeros(batch, max_action, dtype=torch.long),
        "action_slots": torch.zeros(batch, max_action, dtype=torch.long), "action_numeric": torch.zeros(batch, max_action, 5),
        "action_mask": torch.zeros(batch, max_action), "label": torch.zeros(batch, dtype=torch.long), "weight": torch.zeros(batch),
    }
    for b, sample in enumerate(samples):
        result["char_idx"][b] = sample["char"]; result["context"][b] = torch.tensor(sample["context"])
        result["state_mask"][b, :2] = 1
        offset = 2
        for name, rows, width in (("hand", sample["hands"], max_hand), ("enemy", sample["enemies"], max_enemy), ("aux", sample["aux"], max_aux)):
            for i, (item_id, numeric) in enumerate(rows[:width]):
                result[f"{name}_ids"][b, i] = item_id; result[f"{name}_numeric"][b, i] = torch.tensor(numeric)
                result["state_mask"][b, offset + i] = 1
            offset += width
        for i, (kind, item_id, slot, numeric) in enumerate(sample["actions"][:max_action]):
            result["action_types"][b, i] = min(3, kind); result["action_ids"][b, i] = item_id
            result["action_slots"][b, i] = min(9, slot); result["action_numeric"][b, i] = torch.tensor(numeric); result["action_mask"][b, i] = 1
        result["label"][b] = sample["label"]; result["weight"][b] = sample["weight"]
    return result


MODEL_KEYS = ("char_idx", "context", "hand_ids", "hand_numeric", "enemy_ids", "enemy_numeric", "aux_ids", "aux_numeric", "state_mask", "action_types", "action_ids", "action_slots", "action_numeric", "action_mask")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", nargs="+", required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=20); parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=3e-4); parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=0, help="data-loader worker processes")
    parser.add_argument("--amp", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(); torch.set_num_threads(args.threads)
    random.seed(args.seed); torch.manual_seed(args.seed)
    if torch.cuda.is_available(): torch.cuda.manual_seed_all(args.seed)
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("--device cuda requested, but torch.cuda.is_available() is false")
    device = torch.device("cuda" if args.device == "auto" and torch.cuda.is_available() else args.device if args.device != "auto" else "cpu")
    use_amp = bool(args.amp and device.type == "cuda")
    rows = read_rows(args.shards); vocab = build_vocab(rows); dataset = CombatDataset(rows, vocab)
    episodes = sorted({sample["episode"] for sample in dataset.samples}); random.Random(args.seed).shuffle(episodes)
    train_episodes = set(episodes[:max(1, int(len(episodes) * .8))])
    train_indices = [i for i, sample in enumerate(dataset.samples) if sample["episode"] in train_episodes]
    val_indices = [i for i, sample in enumerate(dataset.samples) if sample["episode"] not in train_episodes]
    loader_options = {"num_workers": args.workers, "pin_memory": device.type == "cuda", "persistent_workers": args.workers > 0}
    train = DataLoader(Subset(dataset, train_indices), args.batch_size, shuffle=True, collate_fn=collate, **loader_options)
    val = DataLoader(Subset(dataset, val_indices), args.batch_size, collate_fn=collate, **loader_options)
    model = V12CombatPolicyNet(len(vocab)).to(device); optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, args.epochs, 1e-5); best = -1.0
    print(json.dumps({"rows": len(dataset), "vocab": len(vocab), "train": len(train_indices), "validation": len(val_indices), "device": str(device), "amp": use_amp, "seed": args.seed}), flush=True)
    for epoch in range(1, args.epochs + 1):
        started = time.perf_counter(); model.train(); correct = count = 0
        for batch in train:
            batch = {key: value.to(device, non_blocking=True) for key, value in batch.items()}
            optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
                logits = model(**{key: batch[key] for key in MODEL_KEYS}); losses = F.cross_entropy(logits, batch["label"], reduction="none")
                loss = (losses * batch["weight"]).mean()
            scaler.scale(loss).backward(); scaler.unscale_(optimizer); torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            scaler.step(optimizer); scaler.update()
            correct += (logits.argmax(1) == batch["label"]).sum().item(); count += len(batch["label"])
        scheduler.step(); model.eval(); v1 = v3 = total = 0
        with torch.inference_mode():
            for batch in val:
                batch = {key: value.to(device, non_blocking=True) for key, value in batch.items()}
                with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=use_amp):
                    logits = model(**{key: batch[key] for key in MODEL_KEYS})
                labels = batch["label"]
                v1 += (logits.argmax(1) == labels).sum().item(); v3 += (logits.topk(3, 1).indices == labels[:, None]).any(1).sum().item(); total += len(labels)
        acc = v1 / max(1, total)
        print(f"epoch={epoch} train={correct/max(1,count):.4f} val_top1={acc:.4f} val_top3={v3/max(1,total):.4f} seconds={time.perf_counter()-started:.1f}", flush=True)
        if acc > best:
            best = acc; args.output.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"architecture": "v12", "model_state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()}, "entity_to_idx": vocab, "val_top1_acc": acc, "epoch": epoch, "seed": args.seed, "training_device": str(device), "amp": use_amp}, args.output)


if __name__ == "__main__": main()
