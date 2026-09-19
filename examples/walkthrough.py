"""Scripted agent workflow through real MCP, using an isolated temporary database.

The clarification text below represents prescribed scenario input, not answers
obtained from a real user or a free-form model evaluation. No external LLM is used.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from tracker.db import Database
from tracker.seed import seed_demo
from tracker.service import Tracker


async def demo(path):
    tracker = Tracker(Database(str(path)), "demo", "walkthrough-agent")
    ids = seed_demo(tracker)
    params = StdioServerParameters(command=sys.executable, args=["-m", "tracker.server"],
        env={**os.environ, "TRACKER_DB_PATH": str(path), "TRACKER_WORKSPACE_ID": "demo",
             "TRACKER_ACTOR_ID": "walkthrough-agent"})
    async with Client(params, read_timeout_seconds=10) as client:
        async def call(tool_name, **args):
            result = await client.call_tool(tool_name, args)
            assert not result.is_error, result
            envelope = result.structured_content
            assert envelope["ok"], envelope
            return envelope["result"]

        async def review(record_id):
            return (await call("resolve_record", query=record_id))["resolution_id"]

        async def write(operation, arguments, reviews, reason):
            action = await call("prepare_write", operation=operation, arguments=arguments,
                                review_ids=reviews, decision_reason=reason)
            return await call("commit_write", action_id=action["action_id"])

        catalog = await call("discover_collections", name="assessments",
                             purpose="One author's assessment of an interview")
        assert any(c["id"] == ids["collections"]["feedback"] for c in catalog["catalog"])
        print("Collection decision: reuse Feedback; it already stores one assessment per author/interview.")
        person = await call("resolve_record", query="Kriat", collection_id=ids["collections"]["people"])
        assert person["status"] == "needs_clarification"
        print("User scenario: 'Add Priya's feedback for Kriat's backend coding interview.'")
        print("Agent: 'Did you mean Kirat, who is being considered for Junior Backend?' ")
        print("Prescribed scenario answer: 'Yes, Kirat. This is an additional assessment.'")
        app = await call("resolve_record", query="Kirat Backend", collection_id=ids["collections"]["applications"],
                         context_record_ids=[ids["kirat"], ids["junior_backend"]])
        assert app["selected_record_id"] == ids["kirat_backend"]
        interview = await call("resolve_record", query="Kirat backend coding interview",
                               collection_id=ids["collections"]["interviews"], context_record_ids=[ids["kirat_backend"]])
        assert interview["selected_record_id"] == ids["coding_interview"]
        title = "Priya follow-up: edge-case discussion"
        duplicate_check = await call("resolve_record", query=title, collection_id=ids["collections"]["feedback"])
        action = await call("prepare_write", operation="create_record", arguments={
            "collection_id": ids["collections"]["feedback"], "title": title,
            "data": {"author": "Priya", "assessment": "Handled edge cases clearly."}},
            review_ids=[duplicate_check["resolution_id"]],
            decision_reason="Reuse Feedback for the explicitly requested additional assessment",
            clarification="Scenario user confirmed Kirat and said this assessment is additional")
        feedback = await call("commit_write", action_id=action["action_id"])
        assert (await call("commit_write", action_id=action["action_id"]))["id"] == feedback["id"]
        await write("link_records", {"source_id": feedback["id"], "relationship_type": "interview",
                                    "target_id": ids["coding_interview"]},
                    [await review(feedback["id"]), await review(ids["coding_interview"])],
                    "Attach the assessment to the explicitly resolved backend coding interview")
        print("Saved: Priya's additional feedback on Kirat's Junior Backend coding interview.")
        print("Retry check: committing the same create action returned the same feedback UUID.")
        # Deliberately add then correct a wrong link to demonstrate retained history.
        wrong = await write("link_records", {"source_id": feedback["id"], "relationship_type": "interview",
                                              "target_id": ids["design_interview"]},
                            [await review(feedback["id"]), await review(ids["design_interview"])],
                            "Demonstration only: deliberately add an incorrect design-interview link")
        await write("unlink_records", {"relationship_id": wrong["id"]},
                    [await review(feedback["id"]), await review(ids["design_interview"])],
                    "Correct the deliberate wrong link; keep the coding-interview link")
        history = await call("get_record_history", record_id=feedback["id"])
        assert any(e["operation"] == "unlink" and e["before"]["id"] == wrong["id"] for e in history["events"])
        print("Correction check: wrong relationship removed; its details remain in source history.")
    async with Client(params, read_timeout_seconds=10) as client:
        result = await client.call_tool("get_record_context", {"record_id": feedback["id"]})
        context = result.structured_content["result"]
        assert len(context["outgoing"]) == 1
        assert context["outgoing"][0]["target_id"] == ids["coding_interview"]
        print("New conversation check: restarted server retrieved the correct feedback and interview link.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="tracker-walkthrough-") as directory:
        asyncio.run(demo(Path(directory) / "demo.db"))
