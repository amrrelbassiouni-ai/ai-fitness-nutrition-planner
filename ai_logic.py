import os
import json
import requests
from datetime import datetime
from typing import Dict, Any, Tuple

MODEL_ID = os.getenv("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# -----------------------------
# PROMPTS
# -----------------------------
SYSTEM_PROMPT = """You are a professional fitness coach.
Return ONLY valid JSON (no markdown, no commentary).
Follow the schema exactly.
If the user says halal only, do NOT include pork or alcohol.
If the user lists dislikes/allergies, do NOT include those foods.
All food quantities must be in grams (g) or millilitres (ml) only.
Do NOT use cups, ounces, tablespoons, teaspoons, or vague household measurements.
"""

REPAIR_SYSTEM_PROMPT = """You are a strict JSON repair tool.
You will be given text that SHOULD be a single JSON object but may contain minor JSON syntax errors.
Return ONLY a valid JSON object. No commentary. No markdown. No extra keys.
"""

NUTRITION_SCHEMA_PROMPT = """Generate a FULL 14-day NUTRITION plan using this JSON schema.
CRITICAL RULES:
- The plan duration is 14 days only.
- nutrition_plan.week_1 MUST include day_1..day_7 (all present)
- nutrition_plan.week_2 MUST include day_1..day_7 (all present)
- snacks MUST be {"main":[...]} (not a raw list)
- Do not omit any days. Do not leave days empty.
- All food quantities must be in grams (g) or millilitres (ml) only.
- Do NOT use cups, ounces, tablespoons, teaspoons, or vague household measurements.
- nutrition_progression.overview must refer only to how to follow this 14-day plan.
- Do not mention 4 weeks, monthly phases, or longer timelines unless the user explicitly asks.
- nutrition_progression.overview should be short, practical, and focused on consistency, hydration, meal timing, and what to do after the 14 days if needed.

Schema:
{
  "title": "string",
  "targets": {
    "daily_calories": "number",
    "protein_g": "number",
    "carbs_g": "number",
    "fat_g": "number",
    "notes": "string"
  },
  "nutrition_plan": {
    "week_1": {
      "day_1": {
        "breakfast": {"main": [{"name":"string","amount":"string"}]},
        "lunch": {"main": [{"name":"string","amount":"string"}]},
        "dinner": {"main": [{"name":"string","amount":"string"}]},
        "snacks": {"main": [{"name":"string","amount":"string"}]}
      }
    },
    "week_2": {}
  },
  "nutrition_progression": {
    "overview": "string"
  },
  "lifestyle_and_recovery": {
    "sleep": "string",
    "stress_management": "string",
    "consistency_tips": "string"
  }
}
"""

TRAINING_SCHEMA_PROMPT = """Generate a FULL 14-day TRAINING plan using this JSON schema.
CRITICAL RULES:
- training_plan.week_1 MUST include day_1..day_7 (all present)
- training_plan.week_2 MUST include day_1..day_7 (all present)
- Do not omit any days. Do not leave days empty.

Schema:
{
  "training_plan": {
    "week_1": {
      "day_1": {
        "focus": "string",
        "warmup": ["string"],
        "exercises": [{"name":"string","sets":"string","reps":"string","rest":"string"}],
        "finisher": ["string"],
        "notes": "string"
      }
    },
    "week_2": {}
  },
  "training_guidance": {
    "frequency_per_week": "string",
    "training_types": [],
    "weekly_structure": "string"
  }
}
"""

# -----------------------------
# ENV + HEADERS
# -----------------------------
def _headers() -> dict:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not found in environment variables.")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5000",
        "X-Title": "FinalFitnessPlan"
    }

# -----------------------------
# JSON extraction
# -----------------------------
def extract_json(text: str) -> str:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model output.")
    return text[start:end + 1]

# -----------------------------
# OpenRouter call
# -----------------------------
def _call_openrouter(messages: list[dict]) -> str:
    payload = {
        "model": MODEL_ID,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    resp = requests.post(
        OPENROUTER_URL,
        headers=_headers(),
        json=payload,
        timeout=180
    )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text}")

    return resp.json()["choices"][0]["message"]["content"]

def _repair_json_via_model(bad_text: str) -> Dict[str, Any]:
    repaired = _call_openrouter([
        {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Fix this into ONE valid JSON object only. "
                "Return only JSON. Do not explain anything.\n\n"
                f"{bad_text}"
            ),
        },
    ])
    repaired_json = extract_json(repaired)
    return json.loads(repaired_json)

def _parse_or_repair(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()

    try:
        return json.loads(raw)
    except Exception:
        pass

    try:
        return json.loads(extract_json(raw))
    except Exception:
        pass

    return _repair_json_via_model(raw)

# -----------------------------
# NORMALIZATION FIXES
# -----------------------------
def _expected_days() -> list[str]:
    return [f"day_{i}" for i in range(1, 8)]

def _normalize_week_days(week_dict: dict) -> dict:
    if not isinstance(week_dict, dict):
        return {}

    out = {}
    for d in _expected_days():
        if d in week_dict:
            out[d] = week_dict[d]

    for i in range(8, 15):
        src = f"day_{i}"
        dst = f"day_{i-7}"
        if src in week_dict and dst not in out:
            out[dst] = week_dict[src]

    return out

def _normalize_meals_for_day(day_obj: dict) -> dict:
    if not isinstance(day_obj, dict):
        day_obj = {}

    def ensure_meal(name: str):
        val = day_obj.get(name)
        if isinstance(val, dict):
            if "main" not in val or not isinstance(val.get("main"), list):
                val["main"] = val.get("main", []) if isinstance(val.get("main"), list) else []
            day_obj[name] = val
        elif isinstance(val, list):
            day_obj[name] = {"main": val}
        else:
            day_obj[name] = {"main": []}

    for meal in ["breakfast", "lunch", "dinner", "snacks"]:
        ensure_meal(meal)

    return day_obj

def normalize_plan_schema(plan: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        return {}

    np = plan.get("nutrition_plan") or {}
    w1 = _normalize_week_days(np.get("week_1") or {})
    w2 = _normalize_week_days(np.get("week_2") or {})

    for d in _expected_days():
        w1[d] = _normalize_meals_for_day(w1.get(d) or {})
        w2[d] = _normalize_meals_for_day(w2.get(d) or {})

    plan["nutrition_plan"] = {"week_1": w1, "week_2": w2}

    tp = plan.get("training_plan") or {}
    tw1 = _normalize_week_days(tp.get("week_1") or {})
    tw2 = _normalize_week_days(tp.get("week_2") or {})

    for d in _expected_days():
        tw1.setdefault(d, {"focus": "", "warmup": [], "exercises": [], "finisher": [], "notes": ""})
        tw2.setdefault(d, {"focus": "", "warmup": [], "exercises": [], "finisher": [], "notes": ""})

    plan["training_plan"] = {"week_1": tw1, "week_2": tw2}

    return plan

# -----------------------------
# Macro validation
# calories ~= 4P + 4C + 9F
# -----------------------------
def validate_and_fix_targets(plan: Dict[str, Any], tolerance: float = 0.15) -> Tuple[Dict[str, Any], bool]:
    targets = plan.get("targets", {}) or {}

    def num(x):
        try:
            return float(x)
        except Exception:
            return None

    p = num(targets.get("protein_g"))
    c = num(targets.get("carbs_g"))
    f = num(targets.get("fat_g"))
    cal = num(targets.get("daily_calories"))

    if p is None or c is None or f is None:
        return plan, False

    est = 4 * p + 4 * c + 9 * f
    if est <= 0:
        return plan, False

    changed = False
    if cal is None or cal <= 0:
        targets["daily_calories"] = int(round(est))
        changed = True
    else:
        diff = abs(est - cal) / max(cal, 1.0)
        if diff > tolerance:
            targets["daily_calories"] = int(round(est))
            changed = True

    if changed:
        note = (targets.get("notes") or "").strip()
        extra = "Auto-corrected calories to match macros (4P+4C+9F)."
        targets["notes"] = (note + " " + extra).strip() if note else extra
        plan["targets"] = targets

    return plan, changed

# -----------------------------
# Split generation helpers
# -----------------------------
def _generate_nutrition_json(user_text: str) -> Dict[str, Any]:
    prompt = f"""
User details:
{user_text}

{NUTRITION_SCHEMA_PROMPT}

Return JSON only.
"""
    raw = _call_openrouter([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ])
    return _parse_or_repair(raw)

def _generate_training_json(user_text: str) -> Dict[str, Any]:
    prompt = f"""
User details:
{user_text}

{TRAINING_SCHEMA_PROMPT}

Return JSON only.
"""
    raw = _call_openrouter([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ])
    return _parse_or_repair(raw)

def _revision_changes_targets(revision_request: str) -> bool:
    text = (revision_request or "").lower()

    target_keywords = [
        "calorie", "calories", "protein", "carb", "carbs", "fat", "fats",
        "macro", "macros", "bulk", "lean bulk", "cut", "deficit", "surplus",
        "maintenance", "maintain", "weight loss", "fat loss", "lose weight",
        "muscle gain", "gain weight"
    ]
    return any(keyword in text for keyword in target_keywords)

def _revise_nutrition_json(previous_plan: dict, revision_request: str, original_user_text: str = "") -> Dict[str, Any]:
    previous_targets = previous_plan.get("targets", {}) or {}

    prompt = f"""
You are revising an existing 14-day nutrition plan.

STRICT RULES:
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include commentary.
- Keep the exact schema structure.
- Keep all 14 days present.
- Keep the same foods and day structure unless the user explicitly asks to change them.
- Preserve the existing calorie and macro targets unless the user explicitly asks to change them.
- If the user asks to change calories, update calories accordingly while keeping the meal plan as similar as possible.
- All quantities must be in grams (g) or millilitres (ml) only.
- nutrition_progression.overview must refer only to the 14-day plan.
- Do not mention 4 weeks, monthly phases, or longer timelines unless the user explicitly asks.

Original user constraints:
{original_user_text}

Existing target values:
Calories: {previous_targets.get("daily_calories", "")}
Protein: {previous_targets.get("protein_g", "")}
Carbs: {previous_targets.get("carbs_g", "")}
Fat: {previous_targets.get("fat_g", "")}

Previous plan JSON:
{json.dumps(previous_plan, ensure_ascii=False)}

User revision request:
{revision_request}

{NUTRITION_SCHEMA_PROMPT}

Return updated nutrition JSON only.
"""
    raw = _call_openrouter([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ])

    print("RAW NUTRITION REVISION OUTPUT:")
    print(raw[:4000])

    return _parse_or_repair(raw)

def _revise_training_json(previous_plan: dict, revision_request: str, original_user_text: str = "") -> Dict[str, Any]:
    prompt = f"""
You are revising an existing 14-day training plan.

STRICT RULES:
- Return ONLY valid JSON.
- Do not include markdown.
- Do not include commentary.
- Keep the exact schema structure.
- Keep all 14 days present.
- Do not change the training plan unless the user's request requires training changes.

Original user constraints:
{original_user_text}

Previous plan JSON:
{json.dumps(previous_plan, ensure_ascii=False)}

User revision request:
{revision_request}

{TRAINING_SCHEMA_PROMPT}

Return updated training JSON only.
"""
    raw = _call_openrouter([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ])

    print("RAW TRAINING REVISION OUTPUT:")
    print(raw[:4000])

    return _parse_or_repair(raw)

# -----------------------------
# MAIN API
# -----------------------------
def generate_plan_from_user_text(user_text: str) -> Dict[str, Any]:
    nutrition_json = _generate_nutrition_json(user_text)
    training_json = _generate_training_json(user_text)

    plan = {}
    plan.update(nutrition_json)
    plan.update(training_json)

    plan = normalize_plan_schema(plan)
    plan, _ = validate_and_fix_targets(plan)
    return plan

def revise_plan(previous_plan: dict, revision_request: str, original_user_text: str = "") -> Dict[str, Any]:
    nutrition_json = None
    training_json = None

    nutrition_error = None
    training_error = None

    try:
        nutrition_json = _revise_nutrition_json(previous_plan, revision_request, original_user_text)
    except Exception as e:
        nutrition_error = str(e)

    try:
        training_json = _revise_training_json(previous_plan, revision_request, original_user_text)
    except Exception as e:
        training_error = str(e)

    if nutrition_json is None and training_json is None:
        raise RuntimeError(
            f"Both revision steps failed. Nutrition error: {nutrition_error} | Training error: {training_error}"
        )

    new_plan = {}

    if nutrition_json is not None:
        new_plan.update(nutrition_json)
    else:
        new_plan.update({
            "title": previous_plan.get("title", "Revised Plan"),
            "targets": previous_plan.get("targets", {}),
            "nutrition_plan": previous_plan.get("nutrition_plan", {}),
            "nutrition_progression": previous_plan.get("nutrition_progression", {}),
            "lifestyle_and_recovery": previous_plan.get("lifestyle_and_recovery", {}),
        })

    if training_json is not None:
        new_plan.update(training_json)
    else:
        new_plan.update({
            "training_plan": previous_plan.get("training_plan", {}),
            "training_guidance": previous_plan.get("training_guidance", {}),
        })

    if not new_plan.get("title"):
        new_plan["title"] = previous_plan.get("title", "Revised Plan")

    if not _revision_changes_targets(revision_request):
        new_plan["targets"] = previous_plan.get("targets", {}).copy()

    new_plan = normalize_plan_schema(new_plan)
    new_plan, _ = validate_and_fix_targets(new_plan)
    return new_plan

def save_plan_json(plan: Dict[str, Any], json_path: str) -> None:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

# -----------------------------
# PDF: Nutrition
# -----------------------------
def json_data_to_pdf(data: dict, output_pdf: str):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import inch

    doc = SimpleDocTemplate(output_pdf, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    title_style = styles["Heading1"]
    section_style = styles["Heading2"]
    cell_body = ParagraphStyle(name="CellBody", fontSize=9, leading=11)
    header_body = ParagraphStyle(name="HeaderBody", fontSize=10, leading=12, fontName="Helvetica-Bold")

    elements.append(Paragraph(data.get("title", "14-Day Nutrition Plan"), title_style))
    elements.append(Spacer(1, 10))

    targets = data.get("targets", {}) or {}
    if targets:
        macros_line = (
            f"<b>Daily targets:</b> "
            f"{targets.get('daily_calories','?')} kcal • "
            f"P {targets.get('protein_g','?')}g • "
            f"C {targets.get('carbs_g','?')}g • "
            f"F {targets.get('fat_g','?')}g"
        )
        elements.append(Paragraph(macros_line, cell_body))
        if targets.get("notes"):
            elements.append(Paragraph(f"<b>Notes:</b> {targets.get('notes')}", cell_body))
        elements.append(Spacer(1, 12))

    nutrition_plan = data.get("nutrition_plan", {}) or {}
    for week_key, days in nutrition_plan.items():
        elements.append(Paragraph(week_key.replace("_", " ").title(), section_style))

        for day_key in _expected_days():
            meals = (days or {}).get(day_key, {})
            elements.append(Paragraph(f"<b>{day_key.replace('_', ' ').title()}</b>", styles["Heading3"]))

            table_data = [[
                Paragraph("Meal", header_body),
                Paragraph("Food Item", header_body),
                Paragraph("Amount", header_body),
            ]]

            for meal_name, meal_details in (meals or {}).items():
                mains = []
                if isinstance(meal_details, dict):
                    mains = meal_details.get("main", []) or []
                elif isinstance(meal_details, list):
                    mains = meal_details

                for item in mains:
                    if not isinstance(item, dict):
                        continue
                    table_data.append([
                        Paragraph(meal_name.capitalize(), cell_body),
                        Paragraph(str(item.get("name", "")), cell_body),
                        Paragraph(str(item.get("amount", "")), cell_body),
                    ])

            t = Table(table_data, colWidths=[1.0 * inch, 2.5 * inch, 3.8 * inch])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 15))

    doc.build(elements)

# -----------------------------
# PDF: Workout
# -----------------------------
def training_data_to_pdf(plan: dict, output_pdf: str):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import inch
    from urllib.parse import quote_plus

    doc = SimpleDocTemplate(output_pdf, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    elements = []

    title_style = styles["Heading1"]
    section_style = styles["Heading2"]
    body = ParagraphStyle(name="Body", fontSize=9, leading=12)
    head = ParagraphStyle(name="Head", fontSize=10, leading=12, fontName="Helvetica-Bold")
    link_style = ParagraphStyle(name="LinkBody", fontSize=9, leading=12, textColor=colors.blue)

    elements.append(Paragraph(plan.get("title", "Training Plan"), title_style))
    elements.append(Spacer(1, 10))

    training_plan = plan.get("training_plan", {}) or {}
    for week_key, days in training_plan.items():
        elements.append(Paragraph(week_key.replace("_", " ").title(), section_style))

        for day_key in _expected_days():
            day = (days or {}).get(day_key, {}) or {}
            focus = day.get("focus", "")
            elements.append(Paragraph(f"<b>{day_key.replace('_', ' ').title()}</b> — {focus}", styles["Heading3"]))

            warmup = day.get("warmup", []) or []
            if warmup:
                elements.append(Paragraph("<b>Warmup:</b>", body))
                for item in warmup:
                    search_url = f"https://www.youtube.com/results?search_query={quote_plus(str(item) + ' exercise tutorial')}"
                    elements.append(Paragraph(f'• {item} — <a href="{search_url}">Video Link</a>', link_style))
                elements.append(Spacer(1, 6))

            exs = day.get("exercises", []) or []
            table_data = [[
                Paragraph("Exercise", head),
                Paragraph("Sets", head),
                Paragraph("Reps", head),
                Paragraph("Rest", head),
                Paragraph("Video", head)
            ]]

            for ex in exs:
                if not isinstance(ex, dict):
                    continue

                ex_name = str(ex.get("name", ""))
                search_url = f"https://www.youtube.com/results?search_query={quote_plus(ex_name + ' exercise tutorial')}"

                table_data.append([
                    Paragraph(ex_name, body),
                    Paragraph(str(ex.get("sets", "")), body),
                    Paragraph(str(ex.get("reps", "")), body),
                    Paragraph(str(ex.get("rest", "")), body),
                    Paragraph(f'<a href="{search_url}">Video Link</a>', link_style),
                ])

            t = Table(table_data, colWidths=[2.7 * inch, 0.8 * inch, 0.8 * inch, 0.9 * inch, 1.3 * inch])
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 10))

            finisher = day.get("finisher", []) or []
            if finisher:
                elements.append(Paragraph("<b>Finisher:</b>", body))
                for item in finisher:
                    search_url = f"https://www.youtube.com/results?search_query={quote_plus(str(item) + ' exercise tutorial')}"
                    elements.append(Paragraph(f'• {item} — <a href="{search_url}">Video Link</a>', link_style))

            notes = day.get("notes", "")
            if notes:
                elements.append(Paragraph(f"<b>Notes:</b> {notes}", body))

            elements.append(Spacer(1, 14))

    doc.build(elements)