using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2.NativeSim.FullAppBridge;

public static class FullAppStateTracker
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = false };

    public static (ObservationDto Observation, List<LegalActionDto> LegalActions) CreateStateSnapshot(
        string phase,
        bool isTerminal,
        bool isVictory,
        object? contextObject = null)
    {
        RunManager? runManager = RunManager.Instance;
        CombatManager? combatManager = CombatManager.Instance;
        RunState? runState = runManager?.DebugOnlyGetState();
        Player? player = runState is not null ? LocalContext.GetMe(runState) : null;

        var obs = new ObservationDto
        {
            Phase = phase,
            IsTerminal = isTerminal,
            IsVictory = isVictory,
            Seed = runState?.Rng.StringSeed ?? "",
            Character = player?.Character.Id.Entry ?? "",
            Ascension = runState?.AscensionLevel ?? 0,
            Act = (runState?.CurrentActIndex ?? 0) + 1,
            Floor = runState?.TotalFloor ?? 0,
            Gold = player?.Gold ?? 0,
            PlayerHp = player?.Creature.CurrentHp ?? 0,
            PlayerMaxHp = player?.Creature.MaxHp ?? 0,
            PlayerBlock = player?.Creature.Block ?? 0,
            PlayerEnergy = player?.PlayerCombatState?.Energy ?? 0,
        };

        if (player is not null)
        {
            foreach (var power in player.Creature.Powers)
            {
                obs.PlayerPowers[power.Id.Entry] = power.Amount;
            }

            foreach (var card in player.Deck.Cards)
            {
                obs.DeckCards.Add(card.Id.Entry);
            }
            obs.DeckCards.Sort();

            foreach (var relic in player.Relics)
            {
                obs.Relics.Add(relic.Id.Entry);
            }

            for (int slot = 0; slot < player.PotionSlots.Count; slot++)
            {
                var pot = player.PotionSlots[slot];
                if (pot is not null)
                {
                    obs.Potions.Add(pot.Id.Entry);
                }
            }
        }

        var legalActions = new List<LegalActionDto>();

        if (phase == "combat" && combatManager is not null && combatManager.IsInProgress && player is not null)
        {
            var combatObs = new CombatObservationDto
            {
                Turn = player.PlayerCombatState?.TurnNumber ?? 1,
                DrawPileCount = PileType.Draw.GetPile(player).Cards.Count,
                DiscardPileCount = PileType.Discard.GetPile(player).Cards.Count,
                ExhaustPileCount = PileType.Exhaust.GetPile(player).Cards.Count,
            };

            var handCards = PileType.Hand.GetPile(player).Cards;
            for (int i = 0; i < handCards.Count; i++)
            {
                CardModel card = handCards[i];
                bool canPlay = card.CanPlay();
                combatObs.Hand.Add(new CardObservationDto
                {
                    Index = i,
                    CardId = card.Id.Entry,
                    Cost = card.EnergyCost.Canonical,
                    CanPlay = canPlay,
                    TargetType = card.TargetType.ToString(),
                });

                if (canPlay)
                {
                    if (card.TargetType.IsSingleTarget() && card.TargetType != TargetType.Self)
                    {
                        var hittable = card.CombatState?.HittableEnemies.OrderBy(c => c.CombatId) ?? Enumerable.Empty<Creature>();
                        foreach (var enemy in hittable)
                        {
                            ulong enemyId = enemy.CombatId ?? 0;
                            legalActions.Add(new LegalActionDto
                            {
                                ActionId = $"play_card:{i}:target:{enemyId}",
                                ActionType = "play_card",
                                Description = $"Play {card.Id.Entry} targeting enemy {enemyId} ({enemy.CurrentHp}/{enemy.MaxHp})",
                                Metadata = new Dictionary<string, object?> { ["card_index"] = i, ["target_id"] = enemyId, ["card_id"] = card.Id.Entry }
                            });
                        }
                    }
                    else
                    {
                        legalActions.Add(new LegalActionDto
                        {
                            ActionId = $"play_card:{i}",
                            ActionType = "play_card",
                            Description = $"Play {card.Id.Entry}",
                            Metadata = new Dictionary<string, object?> { ["card_index"] = i, ["card_id"] = card.Id.Entry }
                        });
                    }
                }
            }

            ICombatState? combatState = combatManager.DebugOnlyGetState();
            if (combatState is not null)
            {
                foreach (var enemy in combatState.Enemies.OrderBy(e => e.CombatId))
                {
                    var enemyDto = new EnemyObservationDto
                    {
                        CombatId = enemy.CombatId ?? 0,
                        ModelId = enemy.ModelId.Entry,
                        Hp = enemy.CurrentHp,
                        MaxHp = enemy.MaxHp,
                        Block = enemy.Block,
                        IsAlive = enemy.IsAlive,
                        Intent = enemy.Monster?.NextMove.Id ?? "",
                    };
                    foreach (var p in enemy.Powers)
                    {
                        enemyDto.Powers[p.Id.Entry] = p.Amount;
                    }
                    combatObs.Enemies.Add(enemyDto);
                }
            }

            for (int slot = 0; slot < player.PotionSlots.Count; slot++)
            {
                PotionModel? potion = player.PotionSlots[slot];
                if (potion is null) continue;
                if (potion.TargetType == TargetType.AnyEnemy)
                {
                    var hittable = player.Creature.CombatState?.HittableEnemies.OrderBy(c => c.CombatId) ?? Enumerable.Empty<Creature>();
                    foreach (var enemy in hittable)
                    {
                        ulong enemyId = enemy.CombatId ?? 0;
                        legalActions.Add(new LegalActionDto
                        {
                            ActionId = $"use_potion:{slot}:target:{enemyId}",
                            ActionType = "use_potion",
                            Description = $"Use potion {potion.Id.Entry} on enemy {enemyId}",
                            Metadata = new Dictionary<string, object?> { ["potion_index"] = slot, ["potion_id"] = potion.Id.Entry, ["target_id"] = enemyId }
                        });
                    }
                }
                else if (potion.TargetType is TargetType.AnyAlly or TargetType.AnyPlayer or TargetType.Self)
                {
                    ulong playerId = player.Creature.CombatId ?? 0;
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"use_potion:{slot}:target:{playerId}",
                        ActionType = "use_potion",
                        Description = $"Use potion {potion.Id.Entry} on player {playerId}",
                        Metadata = new Dictionary<string, object?> { ["potion_index"] = slot, ["potion_id"] = potion.Id.Entry, ["target_id"] = playerId }
                    });
                }
                else
                {
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"use_potion:{slot}",
                        ActionType = "use_potion",
                        Description = $"Use potion {potion.Id.Entry}",
                        Metadata = new Dictionary<string, object?> { ["potion_index"] = slot, ["potion_id"] = potion.Id.Entry }
                    });
                }
            }

            legalActions.Add(new LegalActionDto
            {
                ActionId = "end_turn",
                ActionType = "end_turn",
                Description = "End player turn",
            });

            obs.Combat = combatObs;
        }
        else if (phase == "map")
        {
            var roomObs = new RoomObservationDto { RoomType = "Map" };
            if (contextObject is List<MapPoint> reachablePoints)
            {
                for (int i = 0; i < reachablePoints.Count; i++)
                {
                    MapPoint point = reachablePoints[i];
                    string typeName = point.PointType.ToString();
                    int branch1Indexed = i + 1;
                    roomObs.Options.Add($"{branch1Indexed}:{typeName}");
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_map:{branch1Indexed}:{typeName}",
                        ActionType = "choose_map",
                        Description = $"{branch1Indexed} ({typeName})",
                        Metadata = new Dictionary<string, object?> { ["branch_choice"] = branch1Indexed, ["node_index"] = i, ["room_type"] = typeName }
                    });
                }
            }
            obs.Room = roomObs;
        }
        else if (phase == "card_reward")
        {
            var roomObs = new RoomObservationDto { RoomType = "CardReward" };
            if (contextObject is IReadOnlyList<CardModel> cardOptions)
            {
                for (int i = 0; i < cardOptions.Count; i++)
                {
                    CardModel card = cardOptions[i];
                    roomObs.Options.Add(card.Id.Entry);
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_card:{i}:{card.Id.Entry}",
                        ActionType = "choose_card",
                        Description = $"Choose card reward {card.Id.Entry} (Cost: {card.EnergyCost.Canonical})",
                        Metadata = new Dictionary<string, object?> { ["card_index"] = i, ["card_id"] = card.Id.Entry, ["upgrades"] = card.CurrentUpgradeLevel }
                    });
                }
                legalActions.Add(new LegalActionDto
                {
                    ActionId = "skip_card",
                    ActionType = "skip_card",
                    Description = "Skip card reward selection",
                });
            }
            obs.Room = roomObs;
        }
        else if (phase == "rewards")
        {
            var roomObs = new RoomObservationDto { RoomType = "Rewards" };
            if (contextObject is IEnumerable<object> rewards)
            {
                int idx = 0;
                foreach (var r in rewards)
                {
                    string rewardType = r.GetType().Name.Replace("Reward", "");
                    roomObs.Options.Add($"{idx}:{rewardType}");
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_reward:{idx}:{rewardType}",
                        ActionType = "choose_reward",
                        Description = $"Claim {rewardType} reward",
                        Metadata = new Dictionary<string, object?> { ["reward_index"] = idx, ["reward_type"] = rewardType }
                    });
                    idx++;
                }

            }

            legalActions.Add(new LegalActionDto
            {
                ActionId = "proceed",
                ActionType = "proceed",
                Description = "Proceed to next screen / room",
            });
            obs.Room = roomObs;
        }
        else if (phase == "rest_site")
        {
            var roomObs = new RoomObservationDto { RoomType = "RestSite" };
            if (contextObject is IReadOnlyList<RestSiteOption> restOptions)
            {
                for (int i = 0; i < restOptions.Count; i++)
                {
                    RestSiteOption opt = restOptions[i];
                    string key = opt.OptionId;
                    roomObs.Options.Add(key);
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_rest:{key}",
                        ActionType = "choose_rest",
                        Description = $"Rest Site Option: {key}",
                        Metadata = new Dictionary<string, object?> { ["option_key"] = key }
                    });
                }
            }
            obs.Room = roomObs;
        }
        else if (phase == "deck_upgrade")
        {
            var roomObs = new RoomObservationDto { RoomType = "DeckUpgrade" };
            if (contextObject is IReadOnlyList<CardModel> upgradableCards)
            {
                for (int i = 0; i < upgradableCards.Count; i++)
                {
                    CardModel card = upgradableCards[i];
                    roomObs.Options.Add(card.Id.Entry);
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_upgrade:{i}:{card.Id.Entry}",
                        ActionType = "choose_upgrade",
                        Description = $"Upgrade {card.Id.Entry}",
                        Metadata = new Dictionary<string, object?> { ["card_index"] = i, ["card_id"] = card.Id.Entry }
                    });
                }
            }
            obs.Room = roomObs;
        }
        else if (phase == "deck_card_select")
        {
            var roomObs = new RoomObservationDto { RoomType = "DeckCardSelect" };
            if (contextObject is IReadOnlyList<CardModel> selectableCards)
            {
                for (int i = 0; i < selectableCards.Count; i++)
                {
                    CardModel card = selectableCards[i];
                    roomObs.Options.Add(card.Id.Entry);
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_card_select:{i}:{card.Id.Entry}",
                        ActionType = "choose_card_select",
                        Description = $"Select {card.Id.Entry}",
                        Metadata = new Dictionary<string, object?> { ["card_index"] = i, ["card_id"] = card.Id.Entry }
                    });
                }
            }
            obs.Room = roomObs;
        }
        else if (phase == "shop")
        {
            var roomObs = new RoomObservationDto { RoomType = "Shop" };
            if (contextObject is IEnumerable<object> merchantEntries)
            {
                int idx = 0;
                foreach (var entry in merchantEntries)
                {
                    string entryType = entry.GetType().Name.Replace("MerchantEntry", "");
                    string itemId = entry switch
                    {
                        MerchantCardEntry cardEntry => cardEntry.CreationResult?.Card?.Id.Entry ?? "UNKNOWN_CARD",
                        MerchantRelicEntry relicEntry => relicEntry.Model?.Id.Entry ?? "UNKNOWN_RELIC",
                        MerchantPotionEntry potionEntry => potionEntry.Model?.Id.Entry ?? "UNKNOWN_POTION",
                        _ => entryType,
                    };
                    int? price = entry is MerchantEntry merchantEntry ? merchantEntry.Cost : null;
                    bool affordable = entry is not MerchantEntry pricedEntry || pricedEntry.EnoughGold;
                    bool stocked = entry is not MerchantEntry stockEntry || stockEntry.IsStocked;
                    roomObs.Options.Add($"{idx}:{entryType}:{itemId}:{price}");
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"shop_buy:{idx}:{entryType}",
                        ActionType = "shop_buy",
                        Description = $"Buy {itemId} ({entryType}) for {price} gold",
                        Metadata = new Dictionary<string, object?>
                        {
                            ["slot_index"] = idx,
                            ["entry_type"] = entryType,
                            ["item_id"] = itemId,
                            ["price"] = price,
                            ["affordable"] = affordable,
                            ["stocked"] = stocked,
                        }
                    });
                    idx++;
                }
            }

            legalActions.Add(new LegalActionDto
            {
                ActionId = "shop_leave",
                ActionType = "shop_leave",
                Description = "Leave merchant shop",
            });
            obs.Room = roomObs;
        }
        else if (phase == "event")
        {
            var roomObs = new RoomObservationDto { RoomType = "Event" };
            if (contextObject is IEnumerable<object> eventOptions)
            {
                int idx = 0;
                foreach (var opt in eventOptions)
                {
                    roomObs.Options.Add(idx.ToString());
                    legalActions.Add(new LegalActionDto
                    {
                        ActionId = $"choose_event:{idx}",
                        ActionType = "choose_event",
                        Description = $"Choose event option {idx}",
                        Metadata = new Dictionary<string, object?> { ["option_index"] = idx }
                    });
                    idx++;
                }
            }

            legalActions.Add(new LegalActionDto
            {
                ActionId = "proceed",
                ActionType = "proceed",
                Description = "Proceed with event",
            });
            obs.Room = roomObs;
        }
        else if (phase == "treasure")
        {
            var roomObs = new RoomObservationDto { RoomType = "Treasure" };
            legalActions.Add(new LegalActionDto
            {
                ActionId = "proceed",
                ActionType = "proceed",
                Description = "Open chest, collect relics and proceed",
            });
            obs.Room = roomObs;
        }
        else if (phase == "victory" || phase == "game_over")
        {
            var roomObs = new RoomObservationDto { RoomType = phase == "victory" ? "Victory" : "GameOver" };
            legalActions.Add(new LegalActionDto
            {
                ActionId = "proceed",
                ActionType = "proceed",
                Description = phase == "victory" ? "Victory! Proceed to next Act" : "Game Over",
            });
            obs.Room = roomObs;
        }

        obs.StateHash = ComputeHash(obs);
        return (obs, legalActions);
    }

    private static string ComputeHash(ObservationDto obs)
    {
        string json = JsonSerializer.Serialize(obs, JsonOptions);
        byte[] bytes = SHA256.HashData(Encoding.UTF8.GetBytes(json));
        return Convert.ToHexString(bytes);
    }
}
