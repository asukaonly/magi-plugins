"""Runtime continuations and UI envelopes are not authored user memories."""

from agent_history_core.adapters.user_content import authored_text


def test_generated_goal_continuation_is_not_user_authored():
    assert authored_text('<codex_internal_context source="goal">Continue the goal.</codex_internal_context>') == ""


def test_browser_context_preserves_the_actual_request():
    raw = '<in-app-browser-context source="ambient-ui-state">Current URL: private</in-app-browser-context>\n\n## My request:\nFix the editor.'
    assert authored_text(raw) == '## My request:\nFix the editor.'


def test_question_reply_keeps_answer_and_labels_the_assistant_question():
    raw = '<send_user_message_question_reply>[{"questionItemId":"internal-id","question":"Did the deployment finish?","answer":"No, it failed."}]</send_user_message_question_reply>'
    assert authored_text(raw) == 'Assistant question (context only, not a user statement):\nDid the deployment finish?\n\nUser answer:\nNo, it failed.'


def test_authored_discussion_of_runtime_tags_is_untouched():
    text = 'Explain this tag: <codex_internal_context>example</codex_internal_context>'
    assert authored_text(text) == text


def test_malformed_runtime_reply_does_not_become_a_user_fact():
    assert authored_text('<send_user_message_question_reply>not json</send_user_message_question_reply>') == ""
