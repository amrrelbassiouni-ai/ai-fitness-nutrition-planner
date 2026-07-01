from dotenv import load_dotenv
load_dotenv()

import os
import uuid
import json
import hashlib
import smtplib

from email.message import EmailMessage

from flask import (
    Flask,
    render_template,
    render_template_string,
    request,
    send_file,
    session,
    redirect,
    url_for,
    flash,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from database import init_db, get_db_connection
from ai_logic import (
    generate_plan_from_user_text,
    save_plan_json,
    json_data_to_pdf,
    training_data_to_pdf,
    revise_plan,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fitness_secret_key_123")

init_db()

BASE_DIR = os.path.dirname(__file__)
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def _init_chat():
    if "chat_history" not in session:
        session["chat_history"] = []


def _safe_load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _canonical_plan_hash(plan: dict) -> str:
    plan_string = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(plan_string.encode("utf-8")).hexdigest()


def _get_current_user_profile():
    if not session.get("user_id"):
        return None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],))
    user_profile = cursor.fetchone()
    conn.close()
    return user_profile


def _is_admin():
    return bool(session.get("is_admin"))


def _profile_is_complete(user_profile) -> bool:
    if not user_profile:
        return False

    required_fields = ["age", "height", "weight", "goal"]
    for field in required_fields:
        value = user_profile[field]
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True


def _build_combined_user_text(extra_request: str, user_profile) -> str:
    extra_request = (extra_request or "").strip()
    profile_lines = []

    if user_profile:
        if user_profile["age"] is not None:
            profile_lines.append(f"Age: {user_profile['age']}")
        if user_profile["height"] is not None:
            profile_lines.append(f"Height: {user_profile['height']} cm")
        if user_profile["weight"] is not None:
            profile_lines.append(f"Weight: {user_profile['weight']} kg")
        if user_profile["goal"]:
            profile_lines.append(f"Goal: {user_profile['goal']}")
        if user_profile["diet"]:
            profile_lines.append(f"Diet: {user_profile['diet']}")
        if user_profile["allergies"]:
            profile_lines.append(f"Allergies: {user_profile['allergies']}")

    if profile_lines and extra_request:
        return (
            "Saved user profile:\n"
            + "\n".join(profile_lines)
            + "\n\nAdditional user request:\n"
            + extra_request
        )

    if profile_lines:
        return "Saved user profile:\n" + "\n".join(profile_lines)

    if extra_request:
        return extra_request

    return ""


def _send_welcome_email(username: str, recipient_email: str):
    mail_server = os.getenv("MAIL_SERVER")
    mail_port = os.getenv("MAIL_PORT")
    mail_username = os.getenv("MAIL_USERNAME")
    mail_password = os.getenv("MAIL_PASSWORD")
    mail_from = os.getenv("MAIL_FROM", mail_username)

    if not all([mail_server, mail_port, mail_username, mail_password, mail_from, recipient_email]):
        return False, "Email settings not configured."

    try:
        msg = EmailMessage()
        msg["Subject"] = "Welcome to the AI Fitness and Nutrition Planner"
        msg["From"] = mail_from
        msg["To"] = recipient_email
        msg.set_content(
            f"""Hi {username},

Welcome to the AI-Powered Personalised Fitness and Nutrition Planner.

Your account has been created successfully. You can now log in, complete your profile, and generate personalised 14-day nutrition and training plans.

Best regards,
AI Fitness Planner
"""
        )

        with smtplib.SMTP(mail_server, int(mail_port)) as server:
            server.starttls()
            server.login(mail_username, mail_password)
            server.send_message(msg)

        return True, "Welcome email sent."
    except Exception as e:
        return False, str(e)


def _save_plan_to_db(
    user_id,
    plan_id,
    plan,
    user_input,
    json_file,
    nutrition_pdf,
    workout_pdf,
    version=1,
    revision_request="",
    parent_plan_id=None,
):
    targets = plan.get("targets", {}) or {}
    plan_hash = _canonical_plan_hash(plan)

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO plans (
            user_id, plan_id, parent_plan_id, title, user_input, json_file, nutrition_pdf, workout_pdf,
            daily_calories, protein_g, carbs_g, fat_g, version, revision_request, plan_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            plan_id,
            parent_plan_id,
            plan.get("title", "Untitled Plan"),
            user_input,
            json_file,
            nutrition_pdf,
            workout_pdf,
            targets.get("daily_calories"),
            targets.get("protein_g"),
            targets.get("carbs_g"),
            targets.get("fat_g"),
            version,
            revision_request,
            plan_hash,
        ),
    )

    conn.commit()
    conn.close()


def _get_user_plan_row(plan_id, user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM plans WHERE plan_id = ? AND user_id = ?",
        (plan_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def _save_chat_message(user_id, plan_id, role, message):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO chat_messages (user_id, plan_id, role, message)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, plan_id, role, message),
    )
    conn.commit()
    conn.close()


def _get_chat_history_for_plan(user_id, plan_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT role, message
        FROM chat_messages
        WHERE user_id = ? AND plan_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (user_id, plan_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"role": row["role"], "message": row["message"]} for row in rows]


def _get_next_version_number(user_id, root_plan_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COALESCE(MAX(version), 0)
        FROM plans
        WHERE user_id = ?
          AND (plan_id = ? OR parent_plan_id = ?)
        """,
        (user_id, root_plan_id, root_plan_id),
    )
    row = cursor.fetchone()
    conn.close()

    max_version = row[0] if row and row[0] is not None else 0
    return max_version + 1


def _find_duplicate_plan_in_family(user_id, root_plan_id, plan_hash):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT *
        FROM plans
        WHERE user_id = ?
          AND plan_hash = ?
          AND (plan_id = ? OR parent_plan_id = ?)
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (user_id, plan_hash, root_plan_id, root_plan_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def _delete_file_if_exists(filename):
    if not filename:
        return
    path = os.path.join(OUTPUT_DIR, secure_filename(str(filename)))
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


@app.get("/")
def index():
    user_profile = _get_current_user_profile()
    return render_template("index.html", user_profile=user_profile)


@app.get("/dashboard")
def dashboard():
    if not session.get("user_id"):
        return redirect(url_for("index"))

    user_profile = _get_current_user_profile()
    return render_template("dashboard.html", user_profile=user_profile)


@app.post("/register")
def register():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
    confirm_password = (request.form.get("confirm_password") or "").strip()

    if not username or not email or not password or not confirm_password:
        return render_template(
            "index.html",
            error="Please fill in username, email, password, and confirm password.",
            user_profile=_get_current_user_profile(),
        )

    if password != confirm_password:
        return render_template(
            "index.html",
            error="Passwords do not match.",
            user_profile=_get_current_user_profile(),
        )

    hashed_password = generate_password_hash(password)

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    existing_username = cursor.fetchone()

    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    existing_email = cursor.fetchone()

    if existing_username:
        conn.close()
        return render_template(
            "index.html",
            error="Username already exists.",
            user_profile=_get_current_user_profile(),
        )

    if existing_email:
        conn.close()
        return render_template(
            "index.html",
            error="Email already exists.",
            user_profile=_get_current_user_profile(),
        )

    cursor.execute(
        """
        INSERT INTO users (username, email, password)
        VALUES (?, ?, ?)
        """,
        (username, email, hashed_password),
    )
    conn.commit()

    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["email"] = user["email"]
    session["user_age"] = user["age"]
    session["user_goal"] = user["goal"]
    session["is_admin"] = bool(user["is_admin"])

    _send_welcome_email(user["username"], user["email"])

    flash("Account created successfully. Please complete your profile before generating a plan.")
    return redirect(url_for("dashboard"))


@app.post("/login")
def login():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not username or not password:
        return render_template(
            "index.html",
            error="Please enter both username and password.",
            user_profile=_get_current_user_profile(),
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user["password"], password):
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["email"] = user["email"]
        session["user_age"] = user["age"]
        session["user_goal"] = user["goal"]
        session["is_admin"] = bool(user["is_admin"])

        if not _profile_is_complete(user):
            flash("Please complete your profile before generating a plan.")
            return redirect(url_for("dashboard"))

        return redirect(url_for("index"))

    return render_template(
        "index.html",
        error="Invalid username or password.",
        user_profile=_get_current_user_profile(),
    )


@app.post("/profile")
def save_profile():
    if not session.get("user_id"):
        return render_template("index.html", error="You must be logged in to save a profile.")

    age = (request.form.get("age") or "").strip()

    height_unit = (request.form.get("height_unit") or "cm").strip()
    height_cm_value = (request.form.get("height_cm_value") or "").strip()
    height_feet = (request.form.get("height_feet") or "").strip()
    height_inches = (request.form.get("height_inches") or "").strip()

    weight_unit = (request.form.get("weight_unit") or "kg").strip()
    weight_value = (request.form.get("weight_value") or "").strip()

    goal = (request.form.get("goal") or "").strip()
    diet = (request.form.get("diet") or "").strip()
    allergies = (request.form.get("allergies") or "").strip()

    def to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    age_value = to_int(age)

    height_cm = None
    if height_unit == "cm":
        height_cm = to_float(height_cm_value)
        if height_cm is None:
            flash("Please enter your height in centimeters.")
            return redirect(url_for("dashboard"))
    elif height_unit == "ft_in":
        feet = to_float(height_feet)
        inches = to_float(height_inches)
        if feet is None:
            flash("Please enter your height in feet.")
            return redirect(url_for("dashboard"))
        total_inches = feet * 12 + (inches or 0)
        height_cm = round(total_inches * 2.54, 2)

    weight_kg = None
    if weight_unit == "kg":
        weight_kg = to_float(weight_value)
        if weight_kg is None:
            flash("Please enter your weight in kilograms.")
            return redirect(url_for("dashboard"))
    elif weight_unit == "lbs":
        lbs = to_float(weight_value)
        if lbs is None:
            flash("Please enter your weight in pounds.")
            return redirect(url_for("dashboard"))
        weight_kg = round(lbs * 0.45359237, 2)

    if age_value is None:
        flash("Please enter a valid age.")
        return redirect(url_for("dashboard"))

    if not goal:
        flash("Please enter your goal.")
        return redirect(url_for("dashboard"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE users
        SET age = ?, height = ?, weight = ?, goal = ?, diet = ?, allergies = ?
        WHERE id = ?
        """,
        (
            age_value,
            height_cm,
            weight_kg,
            goal,
            diet,
            allergies,
            session["user_id"],
        ),
    )

    conn.commit()
    conn.close()

    session["user_age"] = age_value
    session["user_goal"] = goal

    flash("Profile saved successfully.")
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.post("/generate")
def generate():
    extra_request = (request.form.get("user_text") or "").strip()
    user_profile = _get_current_user_profile()

    if session.get("user_id") and not _profile_is_complete(user_profile):
        flash("Please complete your profile before generating a plan.")
        return redirect(url_for("dashboard"))

    combined_user_text = _build_combined_user_text(extra_request, user_profile)

    if not combined_user_text:
        return render_template(
            "index.html",
            error="Please enter your details or save a profile first.",
            user_profile=user_profile,
        )

    session["last_user_text"] = combined_user_text
    session["chat_history"] = []
    _init_chat()
    session["chat_history"].append({"role": "user", "message": combined_user_text})

    plan_id = uuid.uuid4().hex[:10]
    session["last_plan_id"] = plan_id

    json_name = secure_filename(f"plan_{plan_id}.json")
    pdf_nutrition_name = secure_filename(f"nutrition_{plan_id}.pdf")
    pdf_workout_name = secure_filename(f"workout_{plan_id}.pdf")

    json_path = os.path.join(OUTPUT_DIR, json_name)
    pdf_nutrition_path = os.path.join(OUTPUT_DIR, pdf_nutrition_name)
    pdf_workout_path = os.path.join(OUTPUT_DIR, pdf_workout_name)

    try:
        plan = generate_plan_from_user_text(combined_user_text)
        save_plan_json(plan, json_path)
        json_data_to_pdf(plan, pdf_nutrition_path)
        training_data_to_pdf(plan, pdf_workout_path)

        user_id = session.get("user_id")
        if not user_id:
            return render_template(
                "index.html",
                error="Please log in to save and generate plans.",
                user_profile=user_profile,
            )

        _save_plan_to_db(
            user_id=user_id,
            plan_id=plan_id,
            plan=plan,
            user_input=combined_user_text,
            json_file=json_name,
            nutrition_pdf=pdf_nutrition_name,
            workout_pdf=pdf_workout_name,
            version=1,
            revision_request="",
            parent_plan_id=None,
        )

        _save_chat_message(user_id, plan_id, "user", combined_user_text)
        _save_chat_message(
            user_id,
            plan_id,
            "assistant",
            "Done — I generated your nutrition and workout plans. You can review them below or request changes.",
        )

        session["chat_history"].append(
            {
                "role": "assistant",
                "message": "Done — I generated your nutrition and workout plans. You can review them below or request changes.",
            }
        )
        session.modified = True

    except Exception as e:
        return render_template(
            "index.html",
            error=f"Error: {str(e)}",
            user_profile=user_profile,
        )

    return render_template(
        "result.html",
        pdf_nutrition_file=pdf_nutrition_name,
        pdf_workout_file=pdf_workout_name,
        plan_title=plan.get("title", "Your Plan"),
        targets=plan.get("targets"),
        plan_id=plan_id,
        plan=plan,
        chat_history=session.get("chat_history", []),
    )


@app.post("/revise/<plan_id>")
def revise(plan_id):
    revision_request = (request.form.get("revision_request") or "").strip()
    if not revision_request:
        return redirect(url_for("index"))

    user_id = session.get("user_id")
    if not user_id:
        flash("Please log in to revise plans.")
        return redirect(url_for("index"))

    _init_chat()
    session["chat_history"].append({"role": "user", "message": revision_request})

    old_json_name = secure_filename(f"plan_{plan_id}.json")
    old_json_path = os.path.join(OUTPUT_DIR, old_json_name)

    if not os.path.exists(old_json_path):
        return render_template(
            "index.html",
            error="Could not find the previous plan to revise.",
            user_profile=_get_current_user_profile(),
        )

    old_plan = _safe_load_json(old_json_path)
    original_user_text = session.get("last_user_text", "")

    try:
        new_plan = revise_plan(old_plan, revision_request, original_user_text=original_user_text)
    except Exception as e:
        return render_template(
            "index.html",
            error=f"Revision error: {str(e)}",
            user_profile=_get_current_user_profile(),
        )

    parent_plan = _get_user_plan_row(plan_id, user_id)
    parent_root = plan_id
    next_version = 2

    if parent_plan:
        parent_root = parent_plan["parent_plan_id"] or parent_plan["plan_id"]
        next_version = _get_next_version_number(user_id, parent_root)

    new_plan_hash = _canonical_plan_hash(new_plan)
    duplicate_row = _find_duplicate_plan_in_family(user_id, parent_root, new_plan_hash)

    if duplicate_row:
        flash("This revision produced the same plan as an existing version, so no duplicate was saved.")
        return redirect(url_for("open_saved_plan", plan_id=duplicate_row["plan_id"]))

    new_id = uuid.uuid4().hex[:10]
    session["last_plan_id"] = new_id

    new_json_name = secure_filename(f"plan_{new_id}.json")
    pdf_nutrition_name = secure_filename(f"nutrition_{new_id}.pdf")
    pdf_workout_name = secure_filename(f"workout_{new_id}.pdf")

    new_json_path = os.path.join(OUTPUT_DIR, new_json_name)
    pdf_nutrition_path = os.path.join(OUTPUT_DIR, pdf_nutrition_name)
    pdf_workout_path = os.path.join(OUTPUT_DIR, pdf_workout_name)

    try:
        save_plan_json(new_plan, new_json_path)
        json_data_to_pdf(new_plan, pdf_nutrition_path)
        training_data_to_pdf(new_plan, pdf_workout_path)

        _save_plan_to_db(
            user_id=user_id,
            plan_id=new_id,
            plan=new_plan,
            user_input="(revision)",
            json_file=new_json_name,
            nutrition_pdf=pdf_nutrition_name,
            workout_pdf=pdf_workout_name,
            version=next_version,
            revision_request=revision_request,
            parent_plan_id=parent_root,
        )

        prior_chat = _get_chat_history_for_plan(user_id, plan_id)
        for msg in prior_chat:
            _save_chat_message(user_id, new_id, msg["role"], msg["message"])

        _save_chat_message(user_id, new_id, "user", revision_request)
        _save_chat_message(user_id, new_id, "assistant", f"Updated your plan based on: {revision_request}")

        session["chat_history"].append(
            {
                "role": "assistant",
                "message": f"Updated your plan based on: {revision_request}",
            }
        )
        session.modified = True

    except Exception as e:
        return render_template(
            "index.html",
            error=f"Error saving revised plan: {str(e)}",
            user_profile=_get_current_user_profile(),
        )

    return render_template(
        "result.html",
        pdf_nutrition_file=pdf_nutrition_name,
        pdf_workout_file=pdf_workout_name,
        plan_title=new_plan.get("title", "Your Plan (Revised)"),
        targets=new_plan.get("targets"),
        plan_id=new_id,
        revision_request=revision_request,
        plan=new_plan,
        chat_history=session.get("chat_history", []),
    )


@app.get("/history")
def history():
    user_id = session.get("user_id")
    username = session.get("username", "Guest")

    if not user_id:
        flash("Please log in to view saved plans.")
        return redirect(url_for("index"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT *
        FROM plans
        WHERE user_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 50
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    rows = [dict(row) for row in rows]
    return render_template("history.html", username=username, rows=rows)


@app.get("/plan/<plan_id>")
def open_saved_plan(plan_id):
    user_id = session.get("user_id")

    if not user_id:
        return render_template(
            "index.html",
            error="Please log in to open saved plans.",
            user_profile=_get_current_user_profile(),
        )

    row = _get_user_plan_row(plan_id, user_id)
    if not row:
        return render_template(
            "index.html",
            error="Plan not found.",
            user_profile=_get_current_user_profile(),
        )

    json_file = row["json_file"]
    nutrition_pdf_file = row["nutrition_pdf"]
    workout_pdf_file = row["workout_pdf"]

    json_path = os.path.join(OUTPUT_DIR, secure_filename(str(json_file)))
    if not os.path.exists(json_path):
        return render_template(
            "index.html",
            error="Saved plan file could not be found.",
            user_profile=_get_current_user_profile(),
        )

    try:
        plan = _safe_load_json(json_path)
    except Exception:
        return render_template(
            "index.html",
            error="Could not open saved plan JSON.",
            user_profile=_get_current_user_profile(),
        )

    chat_history = _get_chat_history_for_plan(user_id, plan_id)

    session["last_user_text"] = row["user_input"] or ""
    session["last_plan_id"] = str(plan_id)
    session["chat_history"] = chat_history

    return render_template(
        "result.html",
        pdf_nutrition_file=secure_filename(str(nutrition_pdf_file)),
        pdf_workout_file=secure_filename(str(workout_pdf_file)),
        plan_title=plan.get("title", "Saved Plan"),
        targets=plan.get("targets"),
        plan_id=plan_id,
        plan=plan,
        chat_history=chat_history,
    )


@app.post("/delete-plan/<plan_id>")
def delete_plan(plan_id):
    user_id = session.get("user_id")

    if not user_id:
        flash("Please log in to delete saved plans.")
        return redirect(url_for("index"))

    row = _get_user_plan_row(plan_id, user_id)
    if not row:
        flash("Plan not found.")
        return redirect(url_for("history"))

    _delete_file_if_exists(row["json_file"])
    _delete_file_if_exists(row["nutrition_pdf"])
    _delete_file_if_exists(row["workout_pdf"])

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM chat_messages WHERE plan_id = ? AND user_id = ?", (plan_id, user_id))
    cursor.execute("DELETE FROM plans WHERE plan_id = ? AND user_id = ?", (plan_id, user_id))
    conn.commit()
    conn.close()

    flash("Plan deleted successfully.")
    return redirect(url_for("history"))


@app.get("/admin/users")
def admin_users():
    if not session.get("user_id") or not _is_admin():
        flash("Admin access only.")
        return redirect(url_for("index"))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, username, email, is_admin, created_at
        FROM users
        ORDER BY created_at DESC, id DESC
    """)
    users = cursor.fetchall()
    conn.close()

    html = """
    <!doctype html>
    <html>
    <head>
        <title>Admin - Users</title>
        <style>
            body { font-family: Arial, sans-serif; padding: 30px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ccc; padding: 10px; text-align: left; }
            th { background: #f3f3f3; }
            .btn { padding: 8px 12px; cursor: pointer; }
            .danger { background: #c0392b; color: white; border: none; }
        </style>
    </head>
    <body>
        <h1>Admin - User Accounts</h1>
        <p><a href="{{ url_for('index') }}">Back to Home</a></p>
        <table>
            <tr>
                <th>ID</th>
                <th>Username</th>
                <th>Email</th>
                <th>Admin</th>
                <th>Created</th>
                <th>Action</th>
            </tr>
            {% for user in users %}
            <tr>
                <td>{{ user["id"] }}</td>
                <td>{{ user["username"] }}</td>
                <td>{{ user["email"] }}</td>
                <td>{{ "Yes" if user["is_admin"] else "No" }}</td>
                <td>{{ user["created_at"] }}</td>
                <td>
                    {% if user["id"] != session["user_id"] %}
                    <form method="post" action="{{ url_for('admin_delete_user', user_id=user['id']) }}" onsubmit="return confirm('Delete this user and all their data?');">
                        <button class="btn danger" type="submit">Delete</button>
                    </form>
                    {% else %}
                    Current admin
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, users=users)


@app.post("/admin/delete-user/<int:user_id>")
def admin_delete_user(user_id):
    if not session.get("user_id") or not _is_admin():
        flash("Admin access only.")
        return redirect(url_for("index"))

    if user_id == session.get("user_id"):
        flash("You cannot delete your own admin account while logged in.")
        return redirect(url_for("admin_users"))

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        flash("User not found.")
        return redirect(url_for("admin_users"))

    cursor.execute("SELECT * FROM plans WHERE user_id = ?", (user_id,))
    plans = cursor.fetchall()

    for plan in plans:
        _delete_file_if_exists(plan["json_file"])
        _delete_file_if_exists(plan["nutrition_pdf"])
        _delete_file_if_exists(plan["workout_pdf"])

    cursor.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM plans WHERE user_id = ?", (user_id,))
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))

    conn.commit()
    conn.close()

    flash("User deleted successfully.")
    return redirect(url_for("admin_users"))


@app.get("/view/<filename>")
def view_file(filename):
    safe = secure_filename(filename)
    path = os.path.join(OUTPUT_DIR, safe)

    if not os.path.exists(path):
        return "File not found", 404

    return send_file(path, as_attachment=False)


@app.get("/download/<filename>")
def download(filename):
    safe = secure_filename(filename)
    path = os.path.join(OUTPUT_DIR, safe)

    if not os.path.exists(path):
        return "File not found", 404

    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)