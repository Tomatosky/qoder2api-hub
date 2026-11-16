"""Behavioral tests for hosted web tools and Responses wire semantics (offline)."""
import contextlib
import copy
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.TemporaryDirectory(prefix="qd-web-response-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_tmp.name, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_tmp.name, "usage")
import qoder_accounts as A
import qoder_official_web as O
import qoder_proxy as P
import qoder_web_response as R
import qoder_webtools as W


def reply(text="", calls=None, usage=None):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": text,
             **({"tool_calls": calls} if calls else {})},
             "finish_reason": "tool_calls" if calls else "stop"}], "usage": usage or {}}


def call(name="web_search", args=None, id="call-1"):
    return {"id": id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args or {"query": "news"})}}


def search_data():
    return {"pageItems": [{"title": "<em>Official</em> &amp; announcement",
                          "link": "https://example.com/news/announcement", "snippet": "<b>A fact</b>",
                          "publishedTime": "2026-11-16"}]}


class EvidenceTests(unittest.TestCase):
    def test_domain_matching_boundaries_and_idna(self):
        self.assertTrue(R.matches_domain("https://sub.example.com/x", ["example.com"]))
        self.assertFalse(R.matches_domain("https://evilexample.com/x", ["example.com"]))
        self.assertFalse(R.matches_domain("https://example.com.evil.test", ["example.com"]))
        self.assertFalse(R.matches_domain("https://a.example.com", ["example.com"], ["a.example.com"]))
        self.assertEqual(R.domains(["BÜCHER.de."]), ["xn--bcher-kva.de"])
        for value in [["https://example.com"], ["*.example.com"], ["example.com:443"], "example.com", [None]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                R.domains(value)

    def test_ambiguous_or_unsupported_request_options_rejected(self):
        for tools in [[{"type": "web_search", "filters": "bad"}],
                      [{"type": "web_search", "external_web_access": False}],
                      [{"type": "web_search", "search_content_types": ["image"]}],
                      [{"type": "web_search", "filters": {"allowed_domains": ["a.test"]}},
                       {"type": "web_search", "filters": {"allowed_domains": ["b.test"]}}]]:
            with self.assertRaises(ValueError):
                R.WebEvidence(tools)

    def test_source_ids_stable_and_request_scoped(self):
        e = R.WebEvidence()
        first = e.remember("https://example.com/x#one", "Title")
        second = e.remember("https://example.com/x#two", "Title", fetched=True)
        self.assertIs(first, second)
        self.assertTrue(first["fetched"])
        other = R.WebEvidence()
        self.assertEqual(other.sources, {})
        self.assertIsNone(e.remember("http://127.0.0.1/", "private"))

    def test_only_observed_sources_are_annotated(self):
        e = R.WebEvidence()
        e.remember("https://example.com/x", "原始来源")
        text = e.render("中文 [S1]；不要编造 [S9]。https://unseen.example/x")
        self.assertIn("[原始来源](https://example.com/x)", text)
        self.assertIn("[S9]", text)
        annotations = e.annotations(text)
        self.assertEqual(len(annotations), 1)
        a = annotations[0]
        self.assertEqual(text[a["start_index"]:a["end_index"]], "[原始来源](https://example.com/x)")
        self.assertEqual(a["url"], "https://example.com/x")

    def test_local_filter_never_expands_domain_scope(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        result = e.observe("web_search", {"ok": True, "results": [
            {"url": "https://example.com/x", "title": "ok"},
            {"url": "https://notexample.com/x", "title": "bad"}]})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(len(e.sources), 1)

    def test_redirect_cannot_bypass_domain_filter(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        result = e.observe("web_fetch", {"ok": True, "url": "https://example.com/x",
            "final_url": "https://outside.test/x", "text": "hidden"})
        self.assertFalse(result["ok"])
        self.assertNotIn("text", result)
        self.assertFalse(e.sources)

    def test_model_can_narrow_but_not_widen_request_domain_filters(self):
        e = R.WebEvidence([{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}])
        with mock.patch.object(W, "search", return_value={"ok": True, "engine": "local", "results": [
                {"url": "https://a.example.com/x"}, {"url": "https://b.example.com/x"},
                {"url": "https://outside.test/x"}]}), \
                mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "local"}):
            results = []
            W.run_tool("web_search", {"query": "test", "allowed_domains": ["a.example.com"]},
                       context=e, on_result=results.append)
        self.assertEqual([r["url"] for r in results[0]["results"]], ["https://a.example.com/x"])


class HostedWebHandlerTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"QD_WEB_TOOLS_BACKEND": "qoder"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.acc = SimpleNamespace(uid="test-scope", access_token="fake-test-token", realm="cn",
            realm_config=lambda: {"gateway": "https://gateway.qoder.com.cn", "vpc_instance": ""})
        self.transport = mock.patch.object(A, "http_json", return_value=search_data())
        self.http = self.transport.start(); self.addCleanup(self.transport.stop)

    def run_handler(self, responses, payload=None, protocol="responses", stream=False, upstream_error=None, rounds=3):
        frames = []; requests = []; first_model_observed = []
        handler = object.__new__(P.Handler)
        handler.headers = {}; handler.key_entry = None
        handler._model_precheck = lambda *a: None
        handler._request_realm = lambda: "cn"
        handler._key_id_for_usage = lambda: "test-key"
        handler._json = lambda status, obj: (status, obj)
        handler._error = lambda status, message, *a: (status, {"error": message})
        handler._sse_begin = lambda: None
        handler._sse_write = frames.append
        handler._rate_limited = lambda e: (429, {})
        def open_upstream(req, **kwargs):
            requests.append(copy.deepcopy(req))
            first_model_observed.append(len(frames))
            if upstream_error:
                raise upstream_error
            return contextlib.nullcontext(), self.acc, None
        request = {"model": "test-model", "stream": stream,
                   "tools": [{"type": "web_search"}],
                   "include": ["web_search_call.action.sources"], "metadata": {"test": "preserved"}}
        request.update(payload or {})
        path = "/v1/responses" if protocol == "responses" else "/v1/chat/completions"
        with mock.patch.object(P, "open_upstream", side_effect=open_upstream), \
                mock.patch.object(P, "aggregate_stream", side_effect=copy.deepcopy(responses)), \
                mock.patch.object(P, "record_usage"), mock.patch.object(P, "WEB_TOOLS_MAX_ROUNDS", rounds):
            result = handler._handle_web_tools(path, request, P.builtin_web_tool_names(request["tools"]))
        events = [json.loads(f.decode().split("\ndata: ", 1)[1]) for f in frames]
        return result, events, requests, first_model_observed

    def test_stream_emits_real_progress_before_final_and_preserves_ids(self):
        result, events, requests, first_model = self.run_handler(
            [reply(calls=[call()]), reply("中文，见 [S1]。")], stream=True)
        self.assertGreater(first_model[0], 0)  # created/in_progress before model inference
        kinds = [e["type"] for e in events]
        self.assertIn("response.web_search_call.searching", kinds)
        self.assertIn("response.web_search_call.completed", kinds)
        self.assertIn("response.output_text.annotation.added", kinds)
        self.assertLess(kinds.index("response.web_search_call.completed"), kinds.index("response.output_text.delta"))
        sequences = [e["sequence_number"] for e in events]
        self.assertEqual(sequences, list(range(1, len(events) + 1)))
        final = events[-1]["response"]
        self.assertEqual(final["id"], events[0]["response"]["id"])
        self.assertEqual([x["type"] for x in final["output"]], ["web_search_call", "message"])
        self.assertEqual(final["output"][0]["action"]["sources"], [
            {"type": "url", "url": "https://example.com/news/announcement"}])
        message = final["output"][1]
        self.assertEqual(message["content"][0]["annotations"][0]["type"], "url_citation")
        done = [e["item"] for e in events if e["type"] == "response.output_item.done"]
        self.assertEqual(done, final["output"])
        self.assertEqual(final["tools"], [{"type": "web_search"}])
        self.assertEqual(final["metadata"], {"test": "preserved"})
        self.assertEqual(events[-1]["type"], "response.completed")

    def test_nonstream_has_sources_citations_and_no_internal_function_calls(self):
        (status, obj), events, _, _ = self.run_handler([reply(calls=[call()]), reply("See [S1]")])
        self.assertEqual(status, 200)
        self.assertEqual(events, [])
        self.assertEqual([x["type"] for x in obj["output"]], ["web_search_call", "message"])
        self.assertEqual(len(obj["output"][1]["content"][0]["annotations"]), 1)

    def test_search_alone_allows_open_page_with_official_fetch(self):
        self.http.return_value = {"results": [{"statusCode": 200, "markdown": "Page contents",
                                               "metadata": {"title": "Primary source"}}]}
        with mock.patch.object(W, "check_url", return_value=(True, "")):
            (status, obj), _, requests, _ = self.run_handler([
                reply(calls=[call("web_fetch", {"url": "https://example.com/announcement"})]), reply("Read [S1]")])
        self.assertEqual(status, 200)
        self.assertIn("web_fetch", [t["function"]["name"] for t in requests[0]["tools"]])
        self.assertEqual(obj["output"][0]["action"]["type"], "open_page")
        self.assertIn(O.FETCH_PATH, self.http.call_args.args[0])

    def test_last_tool_round_is_followed_by_synthesis_and_usage_summed(self):
        u1 = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
              "prompt_tokens_details": {"cached_tokens": 4},
              "completion_tokens_details": {"reasoning_tokens": 1}}
        u2 = {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
              "prompt_tokens_details": {"cached_tokens": 5},
              "completion_tokens_details": {"reasoning_tokens": 2}}
        (status, obj), _, requests, _ = self.run_handler(
            [reply(calls=[call()], usage=u1), reply("Summary [S1]", usage=u2)], rounds=1)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["tools"], [])
        self.assertEqual(requests[1]["tool_choice"], "none")
        self.assertIn("Published: 2026-11-16", requests[1]["messages"][-1]["content"])
        self.assertEqual(obj["usage"]["input_tokens"], 30)
        self.assertEqual(obj["usage"]["output_tokens"], 5)
        self.assertEqual(obj["usage"]["input_tokens_details"]["cached_tokens"], 9)
        self.assertEqual(obj["usage"]["output_tokens_details"]["reasoning_tokens"], 3)

    def test_duplicate_calls_cached_only_within_request(self):
        calls = [call(id="one"), call(id="two")]
        self.run_handler([reply(calls=calls), reply("done")])
        self.assertEqual(self.http.call_count, 1)
        self.run_handler([reply(calls=[call()]), reply("done")])
        self.assertEqual(self.http.call_count, 2)

    def test_domain_filters_enforced_even_when_official_ignores_them(self):
        self.http.return_value = {"pageItems": [
            {"title": "bad", "link": "https://badexample.com/x", "snippet": "bad"},
            {"title": "ok", "link": "https://sub.example.com/x", "snippet": "good"}]}
        (status, obj), _, requests, _ = self.run_handler([reply(calls=[call()]), reply("[S1]")],
            {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}]})
        content = requests[1]["messages"][-1]["content"]
        self.assertNotIn("badexample.com", content)
        self.assertIn("sub.example.com", content)
        self.assertEqual(len(obj["output"][0]["action"]["sources"]), 1)

    def test_fetch_cannot_bypass_request_domain_scope(self):
        (status, obj), _, _, _ = self.run_handler([
            reply(calls=[call("web_fetch", {"url": "https://outside.test/x"})]), reply("Unable to verify")],
            {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}]})
        self.http.assert_not_called()
        self.assertEqual(obj["output"][0]["status"], "failed")

    def test_tool_budget_caps_calls_and_keeps_all_tool_call_ids_satisfied(self):
        (status, obj), _, requests, _ = self.run_handler(
            [reply(calls=[call(id="one"), call(args={"query": "different"}, id="two")]), reply("done")],
            {"max_tool_calls": 1})
        self.assertEqual(self.http.call_count, 1)
        tools = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["one", "two"])
        self.assertIn("budget exhausted", tools[1]["content"])
        self.assertEqual(obj["output"][1]["status"], "failed")

    def test_none_or_zero_budget_cannot_execute_tools(self):
        for options in [{"tool_choice": "none"}, {"max_tool_calls": 0}]:
            (status, obj), _, requests, _ = self.run_handler([reply("No search")], options)
            self.assertEqual(status, 200)
            self.assertEqual(requests[0]["tools"], [])
        self.http.assert_not_called()

    def test_upstream_ignoring_disabled_tools_fails_closed(self):
        (status, body), _, _, _ = self.run_handler([reply(calls=[call()])], {"tool_choice": "none"})
        self.assertEqual(status, 502)
        self.http.assert_not_called()

    def test_none_remains_none_with_client_tools(self):
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_tool", "parameters": {}}]
        (status, _), _, requests, _ = self.run_handler([reply("done")],
            {"tools": tools, "tool_choice": "none"})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["tool_choice"], "none")
        self.http.assert_not_called()

    def test_application_instructions_and_explicit_parallel_choice_preserved(self):
        instructions = "Use short French sentences. Search only if needed."
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_tool", "parameters": {}}]
        (status, _), _, requests, _ = self.run_handler([reply("Bonjour.")],
            {"instructions": instructions, "input": "Hello", "tools": tools,
             "parallel_tool_calls": True})
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["messages"], [
            {"role": "system", "content": instructions}, {"role": "user", "content": "Hello"}])
        self.assertTrue(requests[0]["parallel_tool_calls"])
        self.http.assert_not_called()

    def test_chat_completion_token_limit_translated(self):
        (status, _), _, requests, _ = self.run_handler([reply("done")],
            {"max_completion_tokens": 123}, protocol="chat")
        self.assertEqual(status, 200)
        self.assertEqual(requests[0]["max_tokens"], 123)
        self.assertNotIn("max_completion_tokens", requests[0])

    def test_forced_search_aliases_are_honored_on_first_turn(self):
        for kind in W.SEARCH_DECL_TYPES:
            (status, _), _, requests, _ = self.run_handler([reply(calls=[call()]), reply("done")],
                {"tools": [{"type": kind}], "tool_choice": {"type": kind}})
            self.assertEqual(status, 200)
            self.assertEqual(requests[0]["tool_choice"], "web_search")
            self.assertEqual(requests[1]["tool_choice"], "auto")

    def test_backend_changes_do_not_affect_an_in_flight_request(self):
        def remote_call(*args, **kwargs):
            os.environ["QD_WEB_TOOLS_BACKEND"] = "local"
            return search_data()
        with mock.patch.object(O, "_request", side_effect=remote_call) as official, \
                mock.patch.object(W, "search", side_effect=AssertionError("request backend changed")):
            (status, _), _, _, _ = self.run_handler([
                reply(calls=[call(args={"query": "one"})]),
                reply(calls=[call(args={"query": "two"})]), reply("done")])
        self.assertEqual(status, 200)
        self.assertEqual(official.call_count, 2)

    def test_upstream_error_after_sse_start_has_failed_terminal_only(self):
        _, events, _, _ = self.run_handler([], stream=True,
            upstream_error=urllib.error.HTTPError("https://upstream", 401, "denied", {}, None))
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertNotIn("response.completed", [e["type"] for e in events])

    def test_official_tool_error_is_not_reported_as_completed_search(self):
        self.http.side_effect = TimeoutError("private-token-should-never-leak")
        _, events, _, _ = self.run_handler([reply(calls=[call()]), reply("Could not verify")], stream=True)
        self.assertNotIn("response.web_search_call.completed", [e["type"] for e in events])
        self.assertEqual(events[-1]["response"]["output"][0]["status"], "failed")
        self.assertNotIn("private-token", str(events))

    def test_invalid_options_rejected_before_inference(self):
        for options in [{"max_tool_calls": -1}, {"max_tool_calls": True},
                        {"previous_response_id": "resp_old"}, {"background": True},
                        {"tools": [{"type": "web_search", "filters": {"allowed_domains": ["https://bad"]}}]}]:
            (status, obj), _, requests, _ = self.run_handler([], options)
            self.assertEqual(status, 400)
            self.assertEqual(requests, [])

    def test_ordinary_function_returned_to_client_without_execution(self):
        tools = [{"type": "web_search"}, {"type": "function", "name": "client_shell", "parameters": {}}]
        (status, obj), _, requests, _ = self.run_handler([
            reply(calls=[call("client_shell", {"command": "echo hi"})])], {"tools": tools})
        self.assertEqual(status, 200)
        self.assertEqual(obj["output"][0]["type"], "function_call")
        self.assertEqual(obj["output"][0]["name"], "client_shell")
        self.http.assert_not_called()

    def test_chat_annotations_use_chat_schema(self):
        (status, obj), _, _, _ = self.run_handler([reply(calls=[call()]), reply("[S1]")], protocol="chat")
        annotation = obj["choices"][0]["message"]["annotations"][0]
        self.assertEqual(annotation["type"], "url_citation")
        self.assertEqual(annotation["url_citation"]["url"], "https://example.com/news/announcement")


if __name__ == "__main__":
    unittest.main()
