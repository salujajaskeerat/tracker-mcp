"""Explicit example data; refuses any existing file, including an empty database."""
import json
import os
from pathlib import Path
import sys

from .db import Database
from .service import Tracker


def seed_demo(tracker):
    descriptions = {
        "people": "One person, including candidates. Optional contact details and confirmed aliases; names are not unique. Applications link to people.",
        "openings": "One job opening. Optional seniority, team and status. Applications link to an opening; do not infer hired status.",
        "applications": "One person's consideration for one opening. Optional status and application date. Links to both person and opening; one person may have several applications.",
        "interviews": "One interview session for one application. Optional date, format and outcome. Links to its application; an application may have multiple interviews.",
        "feedback": "One assessment by one author about one interview. Optional assessment text and date. Links to its interview. Author is distinct from the local actor entering it.",
    }
    collections = {name: tracker.create_collection(name, description)["id"]
                   for name, description in descriptions.items()}
    ids = {"collections": collections}

    def add(key, collection, title, data):
        ids[key] = tracker.create_record(collections[collection], title, data)["id"]
        return ids[key]

    add("kirat", "people", "Kirat", {})
    add("abhishek", "people", "Abhishek", {})
    add("junior_backend", "openings", "Junior Backend", {"status": "open"})
    add("senior_frontend", "openings", "Senior Frontend", {"status": "open"})
    for key, person, opening in (("kirat_backend", "kirat", "junior_backend"),
                                 ("kirat_frontend", "kirat", "senior_frontend"),
                                 ("abhishek_backend", "abhishek", "junior_backend")):
        app = add(key, "applications", key.replace("_", " ").title(), {"status": "considering"})
        tracker.link_records(app, "applicant", ids[person])
        tracker.link_records(app, "opening", ids[opening])
    for key, title in (("coding_interview", "Kirat backend coding interview"),
                       ("design_interview", "Kirat backend design interview")):
        interview = add(key, "interviews", title, {"status": "completed"})
        tracker.link_records(interview, "application", ids["kirat_backend"])
    for author, assessment in (("Priya", "Clear reasoning; improve edge-case tests."),
                                ("Rahul", "Good implementation; discuss complexity.")):
        feedback = add(author.lower() + "_feedback", "feedback", f"{author}: coding feedback",
                       {"author": author, "assessment": assessment})
        tracker.link_records(feedback, "interview", ids["coding_interview"])
    return ids


def main():
    path = Path(os.getenv("TRACKER_DB_PATH", "tracker.db")).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Atomic exclusive reservation; never silently append demo rows to user data.
        with path.open("xb"):
            pass
    except FileExistsError:
        print(f"Refusing to seed existing database: {path}", file=sys.stderr)
        raise SystemExit(1)
    tracker = Tracker(Database(str(path)), os.getenv("TRACKER_WORKSPACE_ID", "local"),
                      os.getenv("TRACKER_ACTOR_ID", "local-agent"))
    print(json.dumps(seed_demo(tracker), indent=2))


if __name__ == "__main__":
    main()
