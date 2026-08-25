"""Backward compatibility wrapper for sts2_native_sim.gym."""
from sts2_native_sim.gym import Sts2NativeVectorEnv, DEFAULT_SCENARIO

__all__ = ["Sts2NativeVectorEnv", "DEFAULT_SCENARIO"]

if __name__ == "__main__":
    import sys
    print("Testing Sts2NativeVectorEnv compatibility import...")
    with Sts2NativeVectorEnv(num_workers=2, seed=123) as env:
        obs, infos = env.reset(seed=123)
        assert len(obs) == 2
        print("  [PASS] Sts2NativeVectorEnv import and reset verified.")
