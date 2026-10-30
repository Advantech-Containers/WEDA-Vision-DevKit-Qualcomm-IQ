"""The question and what becomes of the answer: the PPE prompt, the strict check of the model's JSON answer (no repair of
bad answers), and the verdict computed from it (the model only supplies per-person answers; the counts and `compliant`
are ours)."""
import json
import unicodedata

# --- prompt ---------------------------------------------------------------------------------------------------------

# ppe-v3, the default. Its last sentence is the point: with only the JSON template, which shows one person, the small model copied that person into
# empty scenes (7 of 9 synthetic empty walls); told the exact empty answer, it reported none (0 of 9) and still found the people in real pictures.
PPE_V3 = (
    "Look at the image. Count only people clearly visible; if nobody is visible the list must be empty and you must not invent people. "
    "For each person say whether they wear a hard hat (helmet) and a high-visibility vest: yes, no or unclear. "
    "Reply with ONLY one line of compact JSON, no code fence, no spaces or line breaks inside it: "
    '{"people":[{"helmet":"yes|no|unclear","vest":"yes|no|unclear"}],"scene":"one short sentence"} '
    'If nobody is visible, reply exactly {"people":[],"scene":"no person"}.'
)

# ppe-v4 asks the model to say, for each person it lists, whether the head is visible, and the judge leaves out everyone whose head is not (see
# validate). A hand or a forearm in a picture of a machine was listed as a person with no hard hat and no vest on 10 of 10 test frames; with this
# wording 3 of 10 stay, and on a labelled photo set it did not lose violations.
PPE_V4 = (
    "You are a safety inspector looking at one camera picture. Count a person only if their head and torso are clearly visible. "
    "Do not count hands, arms, legs, reflections, pictures, posters or very small or blurred figures. "
    "For each counted person say whether they wear a hard hat (helmet) and a high-visibility vest: yes, no or unclear. "
    "Also say whether the head is visible (yes or no). "
    'If nobody qualifies, reply exactly {"people":[],"scene":"no person"}. '
    "Reply with ONLY one line of compact JSON, no code fence, no spaces or line breaks inside it: "
    '{"people":[{"head_visible":"yes|no","helmet":"yes|no|unclear","vest":"yes|no|unclear"}],"scene":"one short sentence"}'
)

# ppe-v5 builds the question from the items that are required (REQUIRED_PPE): the same wording as ppe-v3, with the item list and the JSON template
# made for those items. It is the prompt to use when gloves or goggles are required.
PPE_PHRASES = {"helmet": "a hard hat", "vest": "a high-visibility vest", "gloves": "protective gloves", "goggles": "safety goggles or glasses"}
NEEDS_VISIBLE = {"gloves": "hands", "goggles": "eyes"}      # items that cannot be judged when that part of the person is hidden


def build_prompt(required_ppe) -> str:
    """The ppe-v5 question for these items, in the order given."""
    names = [f"{PPE_PHRASES[i]} ({i})" for i in required_ppe]
    wear = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    hidden = " ".join(f"If their {NEEDS_VISIBLE[i]} cannot be seen, answer unclear for {i}." for i in required_ppe if i in NEEDS_VISIBLE)
    template = ",".join(f'"{i}":"yes|no|unclear"' for i in required_ppe)
    return (
        "Look at the image. Count only people clearly visible; if nobody is visible the list must be empty and you must not invent people. "
        f"For each person say whether they wear {wear}: yes, no or unclear. " + (hidden + " " if hidden else "") +
        "Reply with ONLY one line of compact JSON, no code fence, no spaces or line breaks inside it: "
        '{"people":[{' + template + '}],"scene":"one short sentence"} '
        'If nobody is visible, reply exactly {"people":[],"scene":"no person"}.'
    )


PROMPTS = {"ppe-v3": PPE_V3, "ppe-v4": PPE_V4, "ppe-v5": build_prompt(("helmet", "vest"))}


def prompt_for(version, required_ppe, custom="") -> str:
    """The question the model gets: the custom text, or the built-in prompt (ppe-v5 is made from the required items)."""
    if custom:
        return custom
    return build_prompt(required_ppe) if version == "ppe-v5" else PROMPTS[version]


# --- check of the answer --------------------------------------------------------------------------------------------
ANSWERS = ("yes", "no", "unclear")
MAX_PEOPLE = 50
MAX_SCENE = 160


class Rejected(Exception):
    """The answer is not valid; the reason is counted and logged, nothing is published."""


def extract_json(text: str) -> dict:
    """First {...} block (models add code fences); raises Rejected when there is none or it is not JSON."""
    if not isinstance(text, str):
        raise Rejected("answer is not text")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise Rejected("no JSON object in the answer")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise Rejected(f"invalid JSON: {e.msg}")
    except (RecursionError, ValueError):      # absurdly nested, or a number with thousands of digits: still just a bad answer
        raise Rejected("invalid JSON: too deeply nested or too large to read")
    if not isinstance(obj, dict):
        raise Rejected("JSON is not an object")
    return obj


def validate(text: str, required_ppe) -> dict:
    """Return {'people': [{item: answer}], 'scene': str}; raise Rejected otherwise."""
    obj = extract_json(text)
    people = obj.get("people")
    if not isinstance(people, list):
        raise Rejected("'people' is missing or not a list")
    if len(people) > MAX_PEOPLE:
        raise Rejected(f"more than {MAX_PEOPLE} people")
    clean = []
    for i, p in enumerate(people):
        if not isinstance(p, dict):
            raise Rejected(f"person {i} is not an object")
        if "head_visible" in p:                     # ppe-v4: someone whose head cannot be seen is not judged (a forearm is not a person)
            if p["head_visible"] not in ("yes", "no"):
                raise Rejected(f"person {i}: 'head_visible' must be yes or no")
            if p["head_visible"] == "no":
                continue
        row = {}
        for item in required_ppe:
            ans = p.get(item)
            if ans not in ANSWERS:
                raise Rejected(f"person {i}: '{item}' must be one of {'/'.join(ANSWERS)}")
            row[item] = ans
        clean.append(row)
    scene = obj.get("scene")
    if scene is None:
        scene = ""                      # the SA only says to truncate: a missing scene is not a reason to reject
    if not isinstance(scene, str):
        raise Rejected("'scene' is not text")
    # no control, format, surrogate, private-use or line/paragraph separator characters (categories C* and Zl, Zp)
    scene = "".join(ch for ch in scene if unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp"))[:MAX_SCENE]
    return {"people": clean, "scene": scene}


# --- verdict --------------------------------------------------------------------------------------------------------
def compute_verdict(answer: dict, required_ppe) -> dict:
    """Turn the validated answer into the verdict: for each required item, how many people miss it (`no`) and how many are
    `unclear`; the scene is compliant when nobody misses an item. `unclear` is reported but never counts as a violation, and an
    empty scene is compliant (the person count shows that nobody was seen)."""
    people = answer["people"]
    ppe = {}
    violations = []
    for item in required_ppe:
        missing = sum(1 for p in people if p[item] == "no")
        unclear = sum(1 for p in people if p[item] == "unclear")
        ppe[item] = {"required": True, "missing": missing, "unclear": unclear}
        if missing > 0:
            violations.append(item)
    return {
        "personCount": len(people),
        "ppe": ppe,
        "compliant": not violations,   # nobody seen -> true (person_count makes the empty scene visible)
        "violations": violations,
        "scene": answer["scene"],
    }
