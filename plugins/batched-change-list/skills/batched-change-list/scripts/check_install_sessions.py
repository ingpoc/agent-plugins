#!/usr/bin/env python3
"""Validate per-group Codex sessions before a BCL review; install readiness is bcl release-check."""

REVIEW_STATUSES = ("pending", "running", "passed", "failed", "changes_requested", "blocked", "skipped_one_file")


def validate_group_codex(group):
    codex = group.get("codex", {})
    if not isinstance(codex, dict):
        raise ValueError(f"{group.get('id', 'group')}: codex must be an object")
    one_file = codex.get("one_file", False)
    if type(one_file) is not bool:
        raise ValueError(f"{group.get('id', 'group')}: codex.one_file must be a boolean")
    for name in ("implementer_session", "reviewer_session"):
        if name in codex and (not isinstance(codex[name], str) or not codex[name].strip()):
            raise ValueError(f"{group.get('id', 'group')}: codex.{name} must be a nonempty string when present")
    if "review_status" in codex and codex["review_status"] not in REVIEW_STATUSES:
        raise ValueError(f"{group.get('id', 'group')}: codex.review_status must be one of {', '.join(REVIEW_STATUSES)}")


def install_groups(plan, finding_ids=None):
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError("install requires plan groups")
    if any(not isinstance(group, dict) or not isinstance(group.get("finding_ids", []), list)
           or any(not isinstance(identifier, str) for identifier in group.get("finding_ids", []))
           for group in groups):
        raise ValueError("install plan groups must have finding_ids arrays")
    if finding_ids is not None:
        if any(not isinstance(identifier, str) or not identifier.strip() for identifier in finding_ids) or len(finding_ids) != len(set(finding_ids)):
            raise ValueError("install IDs must be unique nonempty strings")
        ids = set(finding_ids)
        selected = [group for group in groups if ids.intersection(group.get("finding_ids", []))]
        covered = {identifier for group in selected for identifier in group.get("finding_ids", [])}
        if not selected or not ids.issubset(covered):
            raise ValueError("install IDs must be covered by current plan groups")
        return selected
    return [group for group in groups if group.get("status") not in {"parked", "deferred", "done", "verified"}
            and group.get("phase") not in {"parked", "deferred", "done"}]


def validate_review_sessions(plan, finding_ids=None):
    selected = install_groups(plan, finding_ids)
    for group in selected:
        validate_group_codex(group)
        codex = group.get("codex", {})
        name = group.get("id", "group")
        if not isinstance(codex.get("implementer_session"), str) or not codex["implementer_session"].strip():
            raise ValueError(f"{name}: review requires codex.implementer_session; record the session with bcl plan")
        if codex.get("fix_status") != "passed":
            raise ValueError(f"{name}: review requires codex.fix_status='passed'")
        if codex.get("one_file"):
            continue
        if codex.get("review_status") == "passed":
            raise ValueError(f"{name}: review has already passed")
    return [group.get("id", "group") for group in selected]
