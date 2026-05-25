from flask import Flask, render_template, request, redirect, url_for, session, flash
import mysql.connector
from passlib.hash import pbkdf2_sha256
from config import DB_CONFIG, SECRET_KEY

app = Flask(__name__)
app.secret_key = SECRET_KEY

CASE_STATUSES = [
    'REGISTERED',
    'UNDER_INVESTIGATION',
    'CHARGESHEET_FILED',
    'TRIAL',
    'CONVICTED',
    'ACQUITTED',
    'CLOSED'
]

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

def log_activity(user_id, action, table_name, record_id, details=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO activity_log (user_id, action, table_name, record_id, details) VALUES (%s, %s, %s, %s, %s)",
        (user_id, action, table_name, record_id, details)
    )
    conn.commit()
    cur.close()
    conn.close()

def init_admin_password():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT user_id, password_hash FROM users WHERE username = 'admin'")
    user = cur.fetchone()
    if user and user["password_hash"] == "TEMP_PASSWORD":
        hashed = pbkdf2_sha256.hash("admin123")
        cur.execute("UPDATE users SET password_hash = %s WHERE user_id = %s", (hashed, user["user_id"]))
        conn.commit()
    cur.close()
    conn.close()

@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if user and pbkdf2_sha256.verify(password, user["password_hash"]):
            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash("Welcome back — you have successfully signed in.", "success")
            log_activity(user["user_id"], "LOGIN", "users", user["user_id"], "User logged in")
            return redirect(url_for("dashboard"))
        else:
            flash("Sorry — that username or password didn't match our records.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    if "user_id" in session:
        uid = session.get("user_id")
        log_activity(uid, "LOGOUT", "users", uid, "User logged out")
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM crimes")
    total_crimes = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM crimes WHERE status = 'UNDER_INVESTIGATION'")
    under_investigation = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM crimes WHERE status IN ('CONVICTED','CLOSED','ACQUITTED')")
    closed_cases = cur.fetchone()[0]
    cur.close()
    conn.close()
    return render_template(
        "dashboard.html",
        username=session.get("username"),
        role=session.get("role"),
        total_crimes=total_crimes,
        under_investigation=under_investigation,
        closed_cases=closed_cases
    )

@app.route("/crimes")
def crimes_list():
    if "user_id" not in session:
        return redirect(url_for("login"))
    fir_filter = (request.args.get("fir") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    section_filter = (request.args.get("section_no") or "").strip()
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    query = """
        SELECT
            c.crime_id,
            c.fir_number,
            c.title,
            c.police_station,
            c.district,
            c.status,
            c.created_at,
            ls.code,
            ls.section_no,
            u.full_name AS officer_name
        FROM crimes c
        LEFT JOIN law_sections ls ON c.primary_law_id = ls.law_id
        LEFT JOIN users u ON c.officer_id = u.user_id
    """
    where_clauses = []
    params = []
    if fir_filter:
        where_clauses.append("c.fir_number LIKE %s")
        params.append("%" + fir_filter + "%")
    if status_filter and status_filter in CASE_STATUSES:
        where_clauses.append("c.status = %s")
        params.append(status_filter)
    if section_filter:
        where_clauses.append("ls.section_no = %s")
        params.append(section_filter)
    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)
    query += " ORDER BY c.created_at DESC"
    cur.execute(query, params)
    crimes = cur.fetchall()
    cur.close()
    conn.close()
    return render_template(
        "crimes_list.html",
        crimes=crimes,
        case_statuses=CASE_STATUSES,
        fir_filter=fir_filter,
        status_filter=status_filter,
        section_filter=section_filter
    )

@app.route("/crimes/new", methods=["GET", "POST"])
def crimes_new():
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT law_id, code, section_no, offence_name FROM law_sections ORDER BY code, section_no")
    law_sections = cur.fetchall()
    if request.method == "POST":
        fir_number = (request.form.get("fir_number") or "").strip()
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip()
        police_station = (request.form.get("police_station") or "").strip()
        district = (request.form.get("district") or "").strip()
        state = (request.form.get("state") or "").strip()
        crime_date = request.form.get("crime_date") or None
        reported_date = request.form.get("reported_date") or None
        primary_law_id = request.form.get("primary_law_id") or None
        status = request.form.get("status") or "REGISTERED"
        if not fir_number or not title or not primary_law_id:
            flash("Please provide FIR number, title and primary law section.", "danger")
            return render_template("crimes_new.html", law_sections=law_sections, case_statuses=CASE_STATUSES)
        officer_id = session.get("user_id")
        insert_sql = """
            INSERT INTO crimes
            (fir_number, title, description, police_station, district, state,
             crime_date, reported_date, status, primary_law_id, officer_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cur.execute(insert_sql, (fir_number, title, description, police_station, district, state, crime_date, reported_date, status, primary_law_id, officer_id))
        conn.commit()
        crime_id = cur.lastrowid
        cur.execute("INSERT INTO crime_sections (crime_id, law_id, role) VALUES (%s, %s, 'PRIMARY')", (crime_id, primary_law_id))
        conn.commit()
        log_activity(officer_id, "CREATE_CRIME", "crimes", crime_id, f"Created crime FIR {fir_number}")
        flash("Crime registered successfully.", "success")
        cur.close()
        conn.close()
        return redirect(url_for("crime_detail", crime_id=crime_id))
    cur.close()
    conn.close()
    return render_template("crimes_new.html", law_sections=law_sections, case_statuses=CASE_STATUSES)

@app.route("/crimes/<int:crime_id>")
def crime_detail(crime_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT
            c.*,
            ls.code,
            ls.section_no,
            ls.offence_name,
            u.full_name AS officer_name
        FROM crimes c
        LEFT JOIN law_sections ls ON c.primary_law_id = ls.law_id
        LEFT JOIN users u ON c.officer_id = u.user_id
        WHERE c.crime_id = %s
        """,
        (crime_id,)
    )
    crime = cur.fetchone()
    if not crime:
        cur.close()
        conn.close()
        flash("We couldn't find that case.", "danger")
        return redirect(url_for("crimes_list"))
    cur.execute(
        "SELECT cs.role, ls.code, ls.section_no, ls.offence_name FROM crime_sections cs JOIN law_sections ls ON cs.law_id = ls.law_id WHERE cs.crime_id = %s",
        (crime_id,)
    )
    sections = cur.fetchall()
    cur.execute(
        "SELECT cp.role_in_case, p.full_name, p.person_type FROM case_person cp JOIN persons p ON cp.person_id = p.person_id WHERE cp.crime_id = %s",
        (crime_id,)
    )
    persons = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("crime_detail.html", crime=crime, sections=sections, persons=persons, case_statuses=CASE_STATUSES)

@app.route("/crimes/<int:crime_id>/update_status", methods=["POST"])
def update_crime_status(crime_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    new_status = request.form.get("status")
    if new_status not in CASE_STATUSES:
        flash("That's not a valid status option.", "danger")
        return redirect(url_for("crime_detail", crime_id=crime_id))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE crimes SET status = %s WHERE crime_id = %s", (new_status, crime_id))
    conn.commit()
    log_activity(session.get("user_id"), "UPDATE_STATUS", "crimes", crime_id, f"Status changed to {new_status}")
    cur.close()
    conn.close()
    flash("Case status updated successfully.", "success")
    return redirect(url_for("crime_detail", crime_id=crime_id))

if __name__ == "__main__":
    init_admin_password()
    app.run(debug=True)
