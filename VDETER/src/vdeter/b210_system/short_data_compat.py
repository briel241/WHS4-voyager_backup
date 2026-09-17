"""Shared launcher D-10 compatibility, not a change to the frozen core policy.

Only an already selected Short Data candidate is considered. The existing
three-fragment promotion ceiling remains 641 presentation bytes; native Data
Sessions are unaffected. No process-global patch happens merely on import.
"""
from functools import wraps
import sys

PROMOTION_PRESENTATION_LIMIT = 3 * 215 - 4


def wrap_short_data_submission(original):
    """Wrap the authoritative MAC method once, for launcher and RF-off harness."""
    if getattr(original, '_d10_compatibility', False) is True:
        return original

    @wraps(original)
    def submit_short_or_session(self_c, state, requirement, causation_event_id):
        # Preserve the launcher D-10 behavior, including its frozen fallback.
        try:
            link11 = next((p for p in requirement.profiles
                           if getattr(p.link_id, 'value', None) == 11), None)
            if link11 is None or not link11.short_data_eligible:
                if requirement.presentation_size > PROMOTION_PRESENTATION_LIMIT:
                    print('[launcher] 승격 거부 — 파편 상한 3 초과 '
                          f'(presentation={requirement.presentation_size} > 641, 세그먼트 > 645)',
                          file=sys.stderr, flush=True)
                    return original(self_c, state, requirement, causation_event_id)
                print('[launcher] short->session 승격 '
                      f'(link11 미적합, size={requirement.presentation_size})',
                      file=sys.stderr, flush=True)
                return self_c._submit_data_session(state, requirement, causation_event_id)
        except Exception:
            # Deliberately unchanged D-10 fallback. Core/contract redesign is separate.
            pass
        return original(self_c, state, requirement, causation_event_id)

    submit_short_or_session._d10_compatibility = True
    return submit_short_or_session
