from .client import NativeSimError, NativeWorker, NativeWorkerPool
from .search import NativeSearchCoordinator
from .scoring import FEATURE_NAMES, NativeObservedMaterialScorer, NativeTorchValueScorer, encode_scoring_features
from .paths import DiscoveryError, find_game_assembly, find_game_root, find_godot
from .observations import extract_agent_observation, to_agent_observation
try:
    from .gym import Sts2NativeVectorEnv
except ImportError:
    Sts2NativeVectorEnv = None  # type: ignore

__all__ = [
    "NativeSimError",
    "NativeWorker",
    "NativeWorkerPool",
    "NativeSearchCoordinator",
    "FEATURE_NAMES",
    "NativeObservedMaterialScorer",
    "NativeTorchValueScorer",
    "encode_scoring_features",
    "DiscoveryError",
    "find_game_assembly",
    "find_game_root",
    "find_godot",
    "extract_agent_observation",
    "project_player_visible_card_state",
    "to_agent_observation",
    "Sts2NativeVectorEnv",
]
