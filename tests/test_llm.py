from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codebuddy.llm import FakeLLMClient, LLMResponse, Message, OpenAICompatibleClient, collect_sse_response, iter_sse_content
from codebuddy.errors import CodeBuddyError
from codebuddy.tool_calls import MALFORMED_TOOL_CALL_NAME, ParsedToolCall, parse_native_tool_calls


class LLMTests(unittest.TestCase):
    def test_sse_stream_parser_yields_content_deltas(self) -> None:
        lines = [
            b"data: {\"choices\":[{\"delta\":{\"content\":\"Hel\"}}]}\n",
            b"data: {\"choices\":[{\"delta\":{\"content\":\"lo\"}}]}\n",
            b"data: [DONE]\n",
        ]

        self.assertEqual("".join(iter_sse_content(lines)), "Hello")

    def test_sse_stream_collector_assembles_native_tool_calls(self) -> None:
        lines = [
            b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"function\":{\"name\":\"read_text\",\"arguments\":\"{\\\"path\\\"\"}}]}}]}\n",
            b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\":\\\"README.md\\\"}\"}}]}}]}\n",
            b"data: [DONE]\n",
        ]

        response = collect_sse_response(lines)

        self.assertEqual(response.tool_calls, [ParsedToolCall("read_text", {"path": "README.md"}, "call_1")])

    def test_native_tool_call_parser_accepts_openai_shape(self) -> None:
        calls = parse_native_tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_text", "arguments": "{\"path\":\"README.md\"}"},
                    }
                ]
            }
        )

        self.assertEqual(calls, [ParsedToolCall("read_text", {"path": "README.md"}, "call_1")])

    def test_native_tool_call_parser_accepts_raw_multiline_strings(self) -> None:
        calls = parse_native_tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "edit_exact_replace",
                            "arguments": '{"path":"agent.py","old":"def f():\n    return 1\n","new":"def f():\n    # Return one.\n    return 1\n"}',
                        },
                    }
                ]
            }
        )

        self.assertEqual(calls[0].arguments["old"], "def f():\n    return 1\n")
        self.assertEqual(calls[0].arguments["new"], "def f():\n    # Return one.\n    return 1\n")

    def test_native_tool_call_parser_can_return_malformed_marker(self) -> None:
        message = {
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "edit_exact_replace",
                        "arguments": '{"path":"agent.py","old":"return 1","new":"def f():\n    """Return one."""\n    return 1\n"}',
                    },
                }
            ]
        }

        with self.assertRaises(CodeBuddyError):
            parse_native_tool_calls(message)

        calls = parse_native_tool_calls(message, tolerate_malformed=True)

        self.assertEqual(calls[0].name, MALFORMED_TOOL_CALL_NAME)
        self.assertEqual(calls[0].arguments["name"], "edit_exact_replace")

    def test_llm_response_can_carry_native_tool_calls(self) -> None:
        response = LLMResponse("", tool_calls=[ParsedToolCall("validate", {})])

        self.assertEqual(response.tool_calls[0].name, "validate")

    def test_fake_llm_records_message_snapshots_per_call(self) -> None:
        fake = FakeLLMClient(["one", "two"])
        messages = [Message("user", "first")]

        fake.complete(messages)
        messages.append(Message("user", "second"))
        fake.complete(messages)

        self.assertEqual([message.content for message in fake.calls[0]], ["first"])
        self.assertEqual([message.content for message in fake.calls[1]], ["first", "second"])

    def test_openai_compatible_client_defaults_to_long_timeout(self) -> None:
        client = OpenAICompatibleClient.from_provider_config(
            {"api_key": "test-key", "base_url": "https://provider.example/v1"},
            "gpt-test",
        )

        self.assertEqual(client.timeout_seconds, 300)


if __name__ == "__main__":
    unittest.main()
