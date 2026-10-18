"""Packet-level MMU package — EPOCH_FOLD + COW_FORK (Build-Plan §44, Phase 2)."""

from kernel.mmu.cow_forker import fork_packet
from kernel.mmu.epoch_folder import FoldResult, fold_raw_events

__all__ = ["FoldResult", "fold_raw_events", "fork_packet"]
