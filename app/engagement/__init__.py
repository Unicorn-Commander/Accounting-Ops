"""Engagement policy — the per-workspace "is a professional available?" switch.

This is the keystone of the professional-substitute model: it turns the classic
CPA-review hard wall into an OPTIONAL gate. When no CPA / EA / tax attorney is
available, a workspace can be set to ``self_attest_allowed`` so a user can still
self-certify a finished work product and complete it. It also governs local-first
inference and the dormant submission rail (all transmission channels default-OFF).
"""
