import json
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from firebase_admin import firestore
from PIL import Image

from . import ai
from .firebase import get_db, verify_token

STATUSES = [
    ("submitted", "Submitted"),
    ("in_progress", "In progress"),
    ("resolved", "Resolved"),
    ("rejected", "Rejected"),
]
STATUS_LABEL = dict(STATUSES)


# ------------------------------------------------------------------ helpers
def session_user(request):
    """Context processor: makes `user` and `is_admin` available in every template."""
    s = request.session
    return {"user": {"uid": s.get("uid"), "email": s.get("email"), "name": s.get("name")} if s.get("uid") else None,
            "is_admin": s.get("role") == "admin"}


def login_required(view):
    @wraps(view)
    def wrapper(request, *a, **kw):
        if not request.session.get("uid"):
            return redirect("login")
        return view(request, *a, **kw)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *a, **kw):
        if not request.session.get("uid"):
            return redirect("login")
        if request.session.get("role") != "admin":
            messages.error(request, "Admin access only.")
            return redirect("my_reports")
        return view(request, *a, **kw)
    return wrapper


def _doc(snap):
    d = snap.to_dict()
    d["id"] = snap.id
    d["short_id"] = snap.id[:6].upper()
    d["status_label"] = STATUS_LABEL.get(d.get("status"), d.get("status"))
    d["category_label"] = ai.CATEGORIES.get(d.get("category"), d.get("category"))
    return d


def _ts(d):
    return d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------- auth
def index(request):
    if not request.session.get("uid"):
        return redirect("login")
    return redirect("admin_panel" if request.session.get("role") == "admin" else "my_reports")


def login_page(request):
    return render(request, "core/login.html", {"firebase_config": settings.FIREBASE_WEB_CONFIG})


@require_POST
def session_login(request):
    """Browser signs in with Firebase JS SDK, then sends us the ID token to verify."""
    try:
        token = json.loads(request.body)["idToken"]
        decoded = verify_token(token)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Invalid login: {e}"}, status=401)

    email = (decoded.get("email") or "").lower()
    role = "admin" if email in settings.ADMIN_EMAILS else "user"
    request.session.update({
        "uid": decoded["uid"], "email": email,
        "name": decoded.get("name") or email.split("@")[0], "role": role,
    })
    get_db().collection("users").document(decoded["uid"]).set(
        {"email": email, "name": request.session["name"], "role": role,
         "last_login": firestore.SERVER_TIMESTAMP}, merge=True)
    return JsonResponse({"ok": True, "redirect": "/panel/" if role == "admin" else "/my/"})


def logout_view(request):
    request.session.flush()
    return redirect("login")


# ------------------------------------------------------------------ citizen
@login_required
def my_reports(request):
    snaps = get_db().collection("reports").where("user_id", "==", request.session["uid"]).stream()
    reports = sorted((_doc(s) for s in snaps), key=_ts, reverse=True)
    counts = {k: sum(1 for r in reports if r["status"] == k) for k, _ in STATUSES}
    return render(request, "core/my_reports.html", {"reports": reports, "counts": counts})


@login_required
def report_new(request):
    ctx = {"categories": ai.CATEGORIES}
    if request.method == "GET":
        return render(request, "core/report_form.html", ctx)

    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    address = request.POST.get("address", "").strip()
    if not title or not description:
        messages.error(request, "Please add a title and a description.")
        return render(request, "core/report_form.html", ctx)

    def _f(name):
        try:
            return float(request.POST.get(name, ""))
        except ValueError:
            return None
    lat, lng = _f("lat"), _f("lng")

    # --- save photo (re-encoded as JPEG, max 1280px) ---
    image_rel, image_abs = None, None
    photo = request.FILES.get("photo")
    if photo:
        try:
            img = Image.open(photo).convert("RGB")
            img.thumbnail((1280, 1280))
            (settings.MEDIA_ROOT / "reports").mkdir(parents=True, exist_ok=True)
            image_rel = f"reports/{uuid.uuid4().hex}.jpg"
            image_abs = settings.MEDIA_ROOT / image_rel
            img.save(image_abs, "JPEG", quality=85)
        except Exception:
            messages.error(request, "That file is not a valid image.")
            return render(request, "core/report_form.html", ctx)

    # --- AI pipeline ---
    text = f"{title}. {description} {address}"
    category, cat_source, cat_conf, cat_note = ai.classify_category(text, request.POST.get("category", "auto"))
    evidence = ai.verify_evidence(description, image_abs)
    if cat_note:
        evidence["flags"].append(cat_note)
    new = {"description": description, "lat": lat, "lng": lng,
           "image_hash": ai.image_hash(image_abs) if image_abs else None}

    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    candidates = [c for c in (_doc(s) for s in db.collection("reports").where("category", "==", category).stream())
                  if c["status"] in ("submitted", "in_progress") and _ts(c) > cutoff
                  and c["user_id"] != request.session["uid"]]
    dup, dup_reason = ai.find_duplicate(new, candidates)

    ref = db.collection("reports").document()
    if dup:
        new_count = dup.get("duplicate_count", 0) + 1
        score, level, reasons = ai.compute_priority(dup["category"], f"{dup['title']} {dup['description']}",
                                                    new_count, dup["ai"]["verification_score"])
        db.collection("reports").document(dup["id"]).update({
            "duplicate_count": new_count, "priority_score": score,
            "priority_level": level, "priority_reasons": reasons})
        # the new report shares the original's (updated) priority
    else:
        score, level, reasons = ai.compute_priority(category, text, 0, evidence["verification_score"])

    now = datetime.now(timezone.utc)
    ref.set({
        "user_id": request.session["uid"], "user_email": request.session["email"],
        "title": title, "description": description, "address": address,
        "lat": lat, "lng": lng, "category": category, "category_source": cat_source,
        "image_path": image_rel, "image_hash": new["image_hash"],
        "status": "submitted", "created_at": now, "updated_at": now,
        "priority_score": score, "priority_level": level, "priority_reasons": reasons,
        "ai": {"category_confidence": cat_conf, **evidence},
        "duplicate_of": dup["id"] if dup else None, "duplicate_reason": dup_reason,
        "duplicate_count": 0,
        "history": [{"status": "submitted", "note": "Report received", "at": now}],
    })
    if dup:
        messages.info(request, f"This looks like an issue already reported nearby (#{dup['short_id']}). "
                               "We linked your report to it - you'll see the same status updates.")
    else:
        messages.success(request, "Report submitted. You can track its status here.")
    return redirect("report_detail", rid=ref.id)


@login_required
def report_detail(request, rid):
    snap = get_db().collection("reports").document(rid).get()
    if not snap.exists:
        messages.error(request, "Report not found.")
        return redirect("index")
    r = _doc(snap)
    if r["user_id"] != request.session["uid"] and request.session.get("role") != "admin":
        messages.error(request, "You can only view your own reports.")
        return redirect("my_reports")
    r["history"] = list(reversed(r.get("history", [])))
    return render(request, "core/report_detail.html", {"r": r, "statuses": STATUSES})


# -------------------------------------------------------------------- admin
@admin_required
def admin_panel(request):
    reports = [_doc(s) for s in get_db().collection("reports").stream()]
    status_f = request.GET.get("status", "")
    cat_f = request.GET.get("category", "")
    show_dups = request.GET.get("dups") == "1"

    stats = {k: sum(1 for r in reports if r["status"] == k) for k, _ in STATUSES}
    stats["needs_review"] = sum(1 for r in reports if r["ai"]["verdict"] == "needs_review" and r["status"] == "submitted")
    stats["total"] = len(reports)

    rows = [r for r in reports
            if (not status_f or r["status"] == status_f)
            and (not cat_f or r["category"] == cat_f)
            and (show_dups or not r.get("duplicate_of"))]
    rows.sort(key=lambda r: (r["status"] in ("resolved", "rejected"), -r["priority_score"]))
    return render(request, "core/admin_panel.html", {
        "rows": rows, "stats": stats, "statuses": STATUSES, "categories": ai.CATEGORIES,
        "status_f": status_f, "cat_f": cat_f, "show_dups": show_dups})


@admin_required
@require_POST
def admin_update(request, rid):
    status = request.POST.get("status")
    note = request.POST.get("note", "").strip()
    if status not in STATUS_LABEL:
        messages.error(request, "Invalid status.")
        return redirect("report_detail", rid=rid)

    db = get_db()
    now = datetime.now(timezone.utc)
    entry = {"status": status, "note": note or STATUS_LABEL[status], "at": now, "by": request.session["email"]}
    update = {"status": status, "updated_at": now, "history": firestore.ArrayUnion([entry])}
    db.collection("reports").document(rid).update(update)
    # duplicates follow the original report's status
    for dup in db.collection("reports").where("duplicate_of", "==", rid).stream():
        dup.reference.update(update)
    messages.success(request, f"Status updated to '{STATUS_LABEL[status]}'.")
    return redirect("report_detail", rid=rid)
