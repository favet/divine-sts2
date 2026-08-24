import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "python"))

from sts2_native_sim.full_app_client import FullAppBridgeClient, FullAppClientConfig
from pure_neural_agent import pure_neural_agent

cfg = FullAppClientConfig(worker_id=0)
client = FullAppBridgeClient(cfg)
client.launch(requested_character="DEFECT")
start_res = client.start_run(seed="WGWH1F64HB", character="DEFECT", ascension=1)
obs = start_res.get("observation", {})

print("=" * 80)
for step in range(250):
    if obs.get("is_terminal", False):
        print(f"Run Terminated at step {step}: Victory={obs.get('is_victory', False)}, Floor={obs.get('floor')}")
        break

    phase = obs.get("phase", "unknown")
    legal = client.legal_actions()
    if not legal:
        print(f"No legal actions at step {step}, phase={phase}")
        break

    legal_ids = [a["action_id"] for a in legal]
    action = pure_neural_agent.select_action(obs, legal)
    floor = obs.get("floor", 1)
    hp = obs.get("player_hp", 0)
    print(f"Step {step:2d} | Phase: {phase:12s} | Floor: {floor:2d} | HP: {hp:2d} | Action: {action:30s} | Legal: {legal_ids[:3]}")
    step_res = client.step(action)
    obs = step_res.get("observation", {})

client.close()
print("=" * 80)
