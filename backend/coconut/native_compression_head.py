"""Bounded preservation of a stale visible reply in the native protected head."""
from functools import wraps


def _closed_tool_groups(messages):
    from agent.message_sanitization import tool_call_id_variants, tool_result_id_variants
    pending = []
    for message in messages:
        if message.get('role') == 'tool':
            variants = tool_result_id_variants(message.get('tool_call_id'))
            matches = [index for index, identities in enumerate(pending) if variants & identities]
            if len(matches) != 1:
                return False
            pending.pop(matches[0])
            continue
        if pending:
            return False
        calls = message.get('tool_calls') or ()
        if calls and message.get('role') != 'assistant':
            return False
        for call in calls:
            identities = tool_call_id_variants(call)
            if not identities or any(identities & other for other in pending):
                return False
            pending.append(identities)
    return not pending


def install(agent, config):
    """Change only this built-in engine's head seam; native tail/fences own the rest.

    A resumed handoff can be the entire middle when an early visible reply pins
    the tail over the context limit. Preserve that reply and its complete prefix
    verbatim in a small head instead. Never relax a configured user-tail promise.
    """
    context = config.get('context', {})
    if not isinstance(context, dict) or context.get('engine', 'compressor') != 'compressor':
        return False
    compressor = getattr(agent, 'context_compressor', None)
    original = getattr(compressor, '_protect_head_size', None)
    if not callable(original):
        return False
    from agent.context_compressor import ContextCompressor, _estimate_msg_budget_tokens
    from agent.conversation_compression import _is_real_user_message
    if type(compressor) is not ContextCompressor:
        return False
    original = compressor._protect_head_size
    if (getattr(original, '_coconut_stale_head', False)
            or getattr(original, '__func__', None) is not ContextCompressor._protect_head_size):
        return False

    @wraps(original)
    def protected_head(messages):
        head = original(messages)
        start = compressor._align_boundary_forward(messages, head)
        cut = compressor._find_tail_cut_by_tokens(messages, start)
        middle = messages[start:cut]
        # The only eligible native no-progress window contains standalone prior
        # summaries. Merged handoffs can contain live text; leave those alone.
        if not middle or not all(compressor._is_context_summary_message(row)
                                 and compressor._strip_context_summary_handoff_message(dict(row)) is None
                                 for row in middle):
            return head
        anchor = compressor._find_last_assistant_message_idx(messages, start)
        if anchor < cut or not isinstance(messages[anchor].get('content'), str) or not messages[anchor]['content'].strip():
            return head
        end = compressor._align_boundary_forward(messages, anchor + 1)
        cap = min(8192, compressor.context_length // 8)
        if sum(_estimate_msg_budget_tokens(row) for row in messages[start:end]) > cap:
            return head
        if sum(_estimate_msg_budget_tokens(row) for row in messages) <= compressor.context_length:
            return head
        latest = compressor._find_last_user_message_idx(messages, end)
        if latest < end or not _is_real_user_message(messages[latest]):
            return head
        users = compressor._real_user_indices_desc(messages, start)
        count = compressor.min_tail_user_messages
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return head
        if any(index < end for index in users[:count]):
            return head
        # Re-run the unchanged native algorithm with the proposed head. Accept
        # only a non-summary middle and a verbatim current request in the tail.
        recovered_cut = compressor._find_tail_cut_by_tokens(messages, end)
        if recovered_cut > latest or not any(not compressor._is_context_summary_message(row)
                                             for row in messages[end:recovered_cut]):
            return head
        # An unresolved effect must remain verbatim, never entrusted to a
        # generated summary. Leave native protection unchanged in that case.
        if any(row.get('effect_disposition') == 'unknown' for row in messages[end:recovered_cut]):
            return head
        # Summarized groups must be complete. An older unanswered call wholly
        # inside the unchanged tail remains native replay's responsibility, but
        # do not intervene during an unfinished current-request tool group.
        if (not _closed_tool_groups(messages[:recovered_cut])
                or not _closed_tool_groups(messages[latest:])):
            return head
        return end

    setattr(protected_head, '_coconut_stale_head', True)
    compressor._protect_head_size = protected_head
    return True
