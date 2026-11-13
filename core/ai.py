"""
CivicWatch AI engine (lightweight, no GPU / heavy model needed).

  1. classify_category   - keyword-scored text classifier
  2. verify_evidence     - photo quality checks + report completeness -> trust score
  3. image_hash          - perceptual hash (dHash) for duplicate photos
  4. find_duplicate      - location + text + image similarity
  5. compute_priority    - explainable 0-100 score

Each function is isolated so you can swap in a real ML model later
(e.g. a YOLO / CLIP image classifier inside verify_evidence).
"""
import difflib
import math
import re

from PIL import Image, ImageFilter, ImageStat

CATEGORIES = {
    "pothole": "Pothole",
    "garbage": "Garbage accumulation",
    "damaged_road": "Damaged road",
    "streetlight": "Broken streetlight",
    "drain": "Overflowing drain",
    "illegal_dumping": "Illegal dumping",
    "infrastructure": "Damaged public infrastructure",
}

KEYWORDS = {
    "pothole": ["pothole", "pot hole", "crater", "hole in the road", "hole on road", "sinkhole"],
    "garbage": ["garbage", "trash", "waste", "litter", "rubbish", "dustbin", "bin overflow", "smell", "stink"],
    "damaged_road": ["damaged road", "road damage", "cracked", "crack", "broken road", "uneven", "road caved", "asphalt", "tar"],
    "streetlight": ["streetlight", "street light", "lamp", "light not working", "dark street", "bulb", "pole light"],
    "drain": ["drain", "sewage", "sewer", "overflow", "manhole", "clogged", "waterlogging", "flooding", "dirty water"],
    "illegal_dumping": ["illegal dump", "dumping", "dumped", "construction debris", "debris", "burning waste", "dumped waste"],
    "infrastructure": ["bridge", "railing", "footpath", "pavement", "bus stop", "signboard", "fence", "wall collapsed", "broken bench", "park"],
}

BASE_SEVERITY = {  # max 25 points
    "drain": 25, "pothole": 22, "damaged_road": 20, "infrastructure": 18,
    "streetlight": 15, "illegal_dumping": 14, "garbage": 12,
}
URGENT_WORDS = ["accident", "injur", "danger", "dangerous", "unsafe", "urgent", "fell", "fall", "death",
                "electric", "shock", "live wire", "open manhole", "flood", "disease", "dengue", "fire", "blocked"]
SENSITIVE_PLACES = ["school", "hospital", "highway", "junction", "crossing", "market", "temple", "mosque",
                    "church", "bus stand", "station", "college", "main road"]


# ---------------------------------------------------------------- 1. category
def classify_category(text, chosen="auto"):
    text = text.lower()
    scores = {c: sum(1 for k in kws if k in text) for c, kws in KEYWORDS.items()}
    total = sum(scores.values())
    best = max(scores, key=scores.get)
    if chosen and chosen != "auto":
        conf = round(scores.get(chosen, 0) / total, 2) if total else None
        note = None
        if total and scores[best] > scores.get(chosen, 0):
            note = f"Text looks more like '{CATEGORIES[best]}'"
        return chosen, "user", conf, note
    if total == 0:
        return "infrastructure", "default", 0.0, "Could not detect a category from the text"
    return best, "ai", round(scores[best] / total, 2), None


# ------------------------------------------------------ 2. evidence verification
def image_hash(path):
    """64-bit difference hash; similar photos differ by only a few bits."""
    try:
        img = Image.open(path).convert("L").resize((9, 8))
        px = list(img.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
        return format(bits, "016x")
    except Exception:
        return None


def verify_evidence(description, image_path):
    score, flags = 100, []
    if len(description.strip()) < 15:
        score -= 20
        flags.append("Description is very short")
    if len(set(re.findall(r"\w+", description.lower()))) < 4:
        score -= 10
        flags.append("Description is unclear")

    if not image_path:
        score -= 35
        flags.append("No photo evidence")
    else:
        try:
            img = Image.open(image_path)
            w, h = img.size
            gray = img.convert("L")
            gray.thumbnail((512, 512))
            sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
            bright = ImageStat.Stat(gray).mean[0]
            contrast = ImageStat.Stat(gray).stddev[0]
            if w < 300 or h < 300:
                score -= 20
                flags.append("Photo resolution is very low")
            if sharp < 25:
                score -= 20
                flags.append("Photo looks blurry")
            if bright < 30:
                score -= 10
                flags.append("Photo is too dark")
            elif bright > 235:
                score -= 10
                flags.append("Photo is overexposed")
            if contrast < 12:
                score -= 15
                flags.append("Photo has almost no detail (blank or screenshot?)")
        except Exception:
            score -= 40
            flags.append("Photo could not be read")

    score = max(score, 0)
    verdict = "verified" if score >= 60 else "needs_review"
    return {"verification_score": score, "flags": flags, "verdict": verdict}


# ---------------------------------------------------------- 3. duplicate check
def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _hamming(h1, h2):
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def find_duplicate(new, candidates, radius_m=100):
    """`new` and each candidate are dicts. Returns (candidate, reason) or (None, None)."""
    for c in candidates:
        if c.get("duplicate_of"):
            continue  # always attach to the original report
        text_sim = difflib.SequenceMatcher(None, new["description"].lower(), c.get("description", "").lower()).ratio()
        img_close = bool(new.get("image_hash") and c.get("image_hash")
                         and _hamming(new["image_hash"], c["image_hash"]) <= 8)
        has_geo = None not in (new.get("lat"), new.get("lng"), c.get("lat"), c.get("lng"))
        dist = haversine_m(new["lat"], new["lng"], c["lat"], c["lng"]) if has_geo else None

        if dist is not None and dist <= radius_m and (text_sim >= 0.35 or img_close):
            return c, f"Same category, {int(dist)} m away, similar {'photo' if img_close else 'description'}"
        if dist is None and (img_close or text_sim >= 0.75):
            return c, "Very similar photo/description"
    return None, None


# ------------------------------------------------------------- 4. prioritisation
def compute_priority(category, text, duplicate_count=0, verification_score=100):
    text = text.lower()
    reasons = []
    score = BASE_SEVERITY.get(category, 12)
    reasons.append(f"{CATEGORIES.get(category, category)} base severity +{score}")

    urgent = sum(1 for w in URGENT_WORDS if w in text)
    if urgent:
        pts = min(urgent * 10, 30)
        score += pts
        reasons.append(f"Urgent/safety words +{pts}")
    places = sum(1 for w in SENSITIVE_PLACES if w in text)
    if places:
        pts = min(places * 10, 20)
        score += pts
        reasons.append(f"Sensitive location +{pts}")
    if duplicate_count:
        pts = min(duplicate_count * 5, 25)
        score += pts
        reasons.append(f"{duplicate_count} other citizen(s) reported this +{pts}")
    if verification_score < 60:
        score -= 10
        reasons.append("Weak evidence -10")

    score = max(0, min(100, score))
    level = "critical" if score >= 65 else "high" if score >= 45 else "medium" if score >= 25 else "low"
    return score, level, reasons
