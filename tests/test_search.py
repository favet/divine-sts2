"""Offline tests for search branch identity deduplication."""
from sts2_native_sim.search import _branch_identity

def test_branch_identity_consistency():
    """Verify branch identity hashing produces identical keys for equivalent paths."""
    branch_1 = {
        "reset_request": {"character": "IRONCLAD", "seed": "TEST"},
        "history": ["play_card_0", "end_turn"],
        "expected_hash": "HASH_ABC",
    }
    branch_2 = {
        "reset_request": {"seed": "TEST", "character": "IRONCLAD"},
        "history": ["play_card_0", "end_turn"],
        "expected_hash": "HASH_ABC",
    }
    assert _branch_identity(branch_1) == _branch_identity(branch_2)

def test_branch_identity_divergence():
    """Verify distinct histories produce distinct branch identities."""
    branch_1 = {
        "reset_request": {"character": "IRONCLAD", "seed": "TEST"},
        "history": ["play_card_0", "end_turn"],
        "expected_hash": "HASH_ABC",
    }
    branch_2 = {
        "reset_request": {"character": "IRONCLAD", "seed": "TEST"},
        "history": ["play_card_1", "end_turn"],
        "expected_hash": "HASH_ABC",
    }
    assert _branch_identity(branch_1) != _branch_identity(branch_2)
