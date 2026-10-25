"""Cheap keyword guards in front of rules/Jev for email triage (X3 red-team, docs/X3_SCENARIOS.md).

injection_hit: the email talks to the classifier (category names, confidence, "ignore previous").
security_veto: the email carries security/payment/identity language, so a sender-only IGNORE/JOB_ALERT
rule must not decide it (a spoofed From would otherwise bury a phishing or real security alert).
Either hit means "unsure": let the fallback LLM (fenced prompt) decide. Stdlib only, no I/O.
"""
from __future__ import annotations

import re

_INJECTION = re.compile(
    r"\b(?:ACTION_NEEDED|JOB_CONFIRMATION|JOB_REJECTION|RECRUITER_REPLY|JOB_ALERT)\b"
    r"|\bclassif(?:y|ied|ier|ication)\b|\bcategory\s*[:=]|\bconfidence\b|\bprobabilit(?:y|ies)\b"
    r"|\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\b|\bsystem\s*(?:prompt|message)\b"
    r"|\b(?:label|mark|tag|treat|route|file)\s+(?:this|it|me)\s+(?:email\s+|message\s+)?as\b"
    r"|\bdo\s+not\s+escalate\b|\bto\s+the\s+(?:classifier|model|assistant)\b|\"choice\"\s*:"
    r"|<\s*/?\s*email\s*>",
    re.I)
_SECURITY = re.compile(
    r"security\s+(?:alert|notice|warning|code)|verif(?:y|ication)\s+(?:your|the|code)|\bpassword\b"
    r"|sign[- ]?in\s+(?:attempt|from|to)|log[- ]?in\s+(?:attempt|from)|suspicious|unauthori[sz]ed"
    r"|one[- ]time\s+(?:code|passcode)|\b2fa\b|two[- ]factor|account\s+(?:suspended|locked|closed|compromised|"
    r"will\s+be\s+(?:closed|suspended))|payment\s+(?:failed|declined|overdue|due|required)|\boverdue\b"
    r"|final\s+(?:notice|reminder)|legal\s+action|unusual\s+activity|confirm\s+your\s+(?:identity|account|payment)"
    r"|\btax\s+(?:refund|return|notice)\b|\bwire\s+transfer\b|\binvoice\b",
    re.I)


def injection_hit(text: str) -> bool:
    return bool(_INJECTION.search(text or ""))


def security_veto(text: str) -> bool:
    return bool(_SECURITY.search(text or ""))
