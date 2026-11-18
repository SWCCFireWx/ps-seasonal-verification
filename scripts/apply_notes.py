#!/usr/bin/env python3
"""
Save review notes sent from the website (a short reason beside a PSA-month, e.g. why an Above
outlook didn't verify, or that a station's rain was bad).

The website opens a GitHub issue titled "[notes XX] ..." whose body holds a ```json block:
  { "gacc": "SW", "notes_add": [{"month": "2026-07", "psa": "SW05", "note": "..."}],
    "notes_remove": [{"month": "2026-07", "psa": "SW05"}] }
Anyone with a GitHub account can add notes; they only touch data/<CODE>/review_notes.csv.

Environment: ISSUE_BODY, ISSUE_AUTHOR. Writes notes_result.md for the reply. Exit 0 = saved, 1 = rejected.
"""

import json
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gaccs  # noqa: E402

RESULT = os.path.join(gaccs.ROOT, "notes_result.md")
COLS = ["month", "psa", "note", "by"]


def fail(msg):
    with open(RESULT, "w") as fh:
        fh.write(f"**Not saved.** {msg}\n\nNothing was changed.\n")
    print("REJECTED:", msg)
    sys.exit(1)


def clean(s, n):
    return re.sub(r"[\x00-\x1f<>`]", "", str(s or "")).strip()[:n].replace(",", ";")


def main():
    body, author = os.environ.get("ISSUE_BODY", ""), os.environ.get("ISSUE_AUTHOR", "").strip()
    m = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    if not m:
        fail("Couldn't find the notes block. Please save from the website rather than editing the issue text.")
    try:
        sub = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        fail(f"The notes block isn't valid ({e}).")
    g = str(sub.get("gacc", "")).upper()
    if not re.fullmatch(r"[A-Z]{2,4}", g) or not os.path.exists(gaccs.config_path(g)):
        fail(f"Unknown GACC '{g}'.")
    psas = set(gaccs.index_psas().get(g, []))
    this = gaccs.ym(date.today())

    def items(key):
        out = []
        for it in sub.get(key) or []:
            mo, psa = str((it or {}).get("month", "")), str((it or {}).get("psa", "")).upper()
            if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mo) or mo > gaccs.add_months(this, 3):
                fail(f"Month '{mo}' isn't valid.")
            if gaccs.gacc_of(psa) != g or (psas and psa not in psas):
                fail(f"{psa} isn't a {g} PSA.")
            note = clean(it.get("note"), 200)
            if key == "notes_add" and not note:
                fail(f"The note for {psa} in {mo} is empty.")
            out.append({"month": mo, "psa": psa, "note": note, "by": author})
        if len(out) > 300:
            fail("Too many notes in one save (300 at most).")
        return out

    add, rem = items("notes_add"), items("notes_remove")
    if not add and not rem:
        fail("No notes were included.")
    path = os.path.join(gaccs.gacc_dir(g), "review_notes.csv")
    key = lambda r: (r["month"], r["psa"].upper())
    gone = {key(r) for r in rem}
    keep = {key(r): r for r in gaccs.read_csv(path) if r.get("month") and key(r) not in gone}
    for r in add:
        keep[key(r)] = r
    gaccs.write_csv(path, sorted(keep.values(), key=key), COLS)
    lines = [f"**Review notes saved** for {g} by @{author}. The website shows them within a few minutes.", ""]
    lines += [f"- {r['month']} {r['psa']}: {r['note']}" for r in add] + [f"- Removed note: {r['month']} {r['psa']}" for r in rem]
    with open(RESULT, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"GACC={g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
