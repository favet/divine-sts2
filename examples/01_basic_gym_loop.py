"""
Example 01: Basic Gymnasium Vectorized Rollout Loop
Demonstrates multi-worker headless stepping with action masks.
"""

from sts2_native_sim.gym import Sts2NativeVectorEnv

def main():
    print("Initializing 2-worker vectorized environment (Ascension 1)...")
    with Sts2NativeVectorEnv(workers=2, ascension=1, seed=42) as env:
        obs, info = env.reset()
        print(f"Environment reset. Initial HP: {[o['player']['current_hp'] for o in obs]}")

        for step_idx in range(10):
            # Select the first legal action for each worker
            actions = [legals[0] for legals in info["legal_action_ids"]]
            obs, rewards, terminations, truncations, info = env.step(actions)

            print(f"Step {step_idx+1:02d} | Rewards: {rewards} | Terminations: {terminations}")
            if any(terminations):
                print("Episode reached terminal outcome.")
                break

if __name__ == "__main__":
    main()
