# Architectural Guardrails & Blind Spot Protections

This document establishes permanent architectural invariants and mathematical specifications to prevent the five common failure modes in roguelike deckbuilder AI. All Phase 2 (Tactical Expert) and Phase 3 (Holistic Run Solver) models must satisfy these specifications.

Implementation status, evidence precedence, and corrections to external proposals live in `project-status-and-review-guide.md`. These are design invariants, not claims that the current system has passed them.

---

## 1. The Clairvoyance Trap (Information-Set POMDP Search)

### Problem
In Slay the Spire, unrevealed cards in the draw pile are hidden random variables. If multi-ply search evaluates future turns using a single fixed pseudo-random shuffle, the agent will make moves that exploit knowing the exact draw sequence. In real gameplay, this leads to fatal blunders when the actual shuffle differs.

### Architectural Invariant
1. **Intra-Turn Exactness:** Search within the current turn is fully observable (hand cards, energy, board) and operates with 100% determinism.
2. **Inter-Turn Determinization Resampling:** Whenever multi-ply search crosses the `END_TURN` boundary:
   - Sample $K \ge 5$ independent random permutations of the unrevealed draw pile.
   - Expand the search tree across all $K$ determinized worlds.
   - Rank and select the root action with the highest expected value averaged across all $K$ permutations:
     $$\text{Action}^* = \arg\max_{a \in \mathcal{A}} \frac{1}{K} \sum_{k=1}^K Q_k(s, a)$$
3. **No hidden-order leakage:** The scorer and search policy must not observe the canonical order of unrevealed cards. Resample lazily only when draws, shuffles, or other hidden-information transitions make the order relevant. Zero root variance is neither expected nor required; the promotion gate is stability below the declared tolerance.

---

## 2. The "Cowardly Agent" Trap (Multi-Task Decoupled Reward Heads)

### Problem
If the value network is trained solely on short-term HP preservation, the policy becomes pathologically cowardly: it avoids Elites, takes weak paths, and rejects high-EV events (e.g. *Apparitions*, *Vampires*, *Golden Idol*), leading to inevitable deaths in Act 2/3 due to insufficient relic and card scaling.

### Architectural Invariant
The neural critic must not collapse state evaluation into a single scalar HP head. The Set Transformer must output **four decoupled multi-task heads**:

$$\mathbf{V}(s) = \Big[ V_{\text{win}}(s),\ V_{\text{hp\_loss}}(s),\ V_{\text{relic\_ev}}(s),\ V_{\text{boss\_readiness}}(s) \Big]$$

1. **$V_{\text{win}}(s) \in [0, 1]$:** Calibrated probability of winning the entire run.
2. **$V_{\text{hp\_loss}}(s) \ge 0$:** Expected HP loss in the immediate combat or event.
3. **$V_{\text{relic\_ev}}(s) \ge 0$:** Marginal long-term win contribution of acquired relics.
4. **$V_{\text{boss\_readiness}}(s) \in [0, 1]$:** Deck capability score against the specific upcoming Act Boss mechanics (e.g. AoE burst for Slime Boss, scaling for Hexaghost, sustain for Time Eater).

**Evaluation Gate:** The macro planner must verify that taking ~15–25 HP damage at an Act 1 Elite is positive net-EV when the expected relic + rare card acquisition increases $V_{\text{win}}(s)$ by $\ge 15\%$.

---

## 3. Draw Dilution & The "Skip" Relative Advantage

### Problem
Neural networks naturally evaluate cards with positive numbers (e.g. "Deal 10 damage") as beneficial in isolation. In Grandmaster play, drafting a mediocre card has an invisible negative cost: **Draw Dilution** (it replaces a powerhouse card in your 5-card turn draw).

### Architectural Invariant
1. **Unchanged Deck Baseline:** `SKIP` is modeled as the unchanged deck state $s_0 = (\text{Deck}, \text{Relics}, \text{Boss})$.
2. **Pairwise Marginal Loss:** The card valuation loss function must be trained on the relative advantage over Skip:
   $$\text{Advantage}(C_i) = Q(s_0 \cup \{C_i\}) - Q(s_0)$$
   $$\mathcal{L}_{\text{rank}} = -\sum_{i} \log \sigma\Big( \text{Advantage}(C_{\text{chosen}}) - \text{Advantage}(C_{\text{rejected}}) \Big)$$
3. If adding card $C_i$ decreases draw consistency or dilutes key engine loops, $\text{Advantage}(C_i) < 0$, making `SKIP` the mathematically optimal output.

---

## 4. Policy Monoculture & Strategy Diversity

### Problem
Self-play RL algorithms frequently collapse into a single dominant archetype (e.g. Ironclad Strength scaling) and fail when RNG does not offer those specific cards.

### Architectural Invariant
1. **7-Archetype Stratification Matrix:** Rollouts and training corpora must maintain balanced coverage across all 5 characters:
   - **Ironclad:** Strength Scaling, Exhaust/Dark Embrace Engine.
   - **Silent:** Poison Stacking, Shiv / Finisher Engine.
   - **Defect:** Orb Cycling / Focus Scaling.
   - **Necrobinder:** Minion Summon Ranks, Doom Burst.
   - **Regent:** Stars Accumulation / Sovereign Blade.
2. **Archetype Restriction Masks:** Phase 2 training must include rollout batches where specific card families are banned during drafting, forcing the model to learn alternative win conditions and high skill floors under bad card RNG.

---

## 5. Potion Scarcity & Reservation Value

### Problem
Potions are high-leverage emergency assets in A18–A20. A bot that treats potions as free resources will squander rare potions (*Ghost in a Jar*, *Cultist Potion*, *Fairy in a Bottle*) in easy hallway fights to save minor chip damage.

### Architectural Invariant
1. **Counterfactual reservation value:** Compare the native outcome distribution after using potion $P_i$ with the outcome distribution after preserving it. The value includes immediate survival/HP benefit and the future opportunity cost of losing that exact potion instance.
2. **Threat-sensitive prior, not a fixed law:** Encounter tier, current HP, potion rarity, replacement probability, upcoming threats, and whether use is necessary to survive are inputs. Fixed tier multipliers may seed a heuristic baseline but cannot become final ground truth. Boss status does not by itself make wasting a potion optimal.

---

## Summary of Verification Gates

| Guardrail | Required Verification Gate | Target Metric |
| :--- | :--- | :--- |
| **1. POMDP Search** | Determinization invariance test across 5 random draw shuffles | Variance in root choice $< 5\%$ |
| **2. Multi-Task Heads** | Elite pathing selection rate on healthy states ($\ge 65\%$ HP) | Elite path chosen $\ge 70\%$ in Act 1 |
| **3. Skip Baseline** | Pairwise ranking accuracy against Skip on bloated 30-card decks | Skip selected on mediocre offerings $\ge 80\%$ |
| **4. Strategy Diversity** | Stratified validation accuracy across all 7 archetypes | $\ge 85\%$ accuracy in every individual archetype |
| **5. Potion Scarcity** | Native counterfactual suites for lethal pressure and avoidable use | Use a potion when needed to survive; $0\%$ avoidable rare-potion waste in covered states |

Ranking gates are necessary but not sufficient for promotion. Every critic must also beat greedy and heuristic baselines with statistically significant lift on a fresh native seed namespace; simulator certification remains a separate exact-differential gate.
