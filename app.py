from flask import Flask, render_template, request, redirect, url_for, session, make_response, flash
import bcrypt
import config
import email_config
from flask_mail import Mail, Message
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date, timedelta
import threading
import pandas as pd
from io import StringIO
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import io
import atexit
import psycopg2
from psycopg2 import sql

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = config.SECRET_KEY

# Mail Config
app.config.update(
    MAIL_SERVER=email_config.EMAIL_HOST,
    MAIL_PORT=email_config.EMAIL_PORT,
    MAIL_USE_TLS=email_config.EMAIL_USE_TLS,
    MAIL_USERNAME=email_config.EMAIL_HOST_USER,
    MAIL_PASSWORD=email_config.EMAIL_HOST_PASSWORD
)

# Initialize mail extension
mail = Mail(app)

# Global connection pool
def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=config.DB_HOST,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dbname=config.DB_NAME,
            port=config.DB_PORT
        )
        return conn
    except Exception as e:
        print(f"❌ Failed to connect to the database: {e}")
        return None

# Create tables if not exist
def create_tables():
    with app.app_context():
        conn = get_db_connection()
        if conn is None:
            return
        cur = conn.cursor()
        try:
            # Users Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password VARCHAR(255) NOT NULL
                )
            """)

            # Medications Table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS medications (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    total_pills INTEGER NOT NULL,
                    dosage_per_day INTEGER NOT NULL,
                    schedule TIME WITHOUT TIME ZONE,
                    description TEXT,
                    last_taken DATE,
                    next_reminder TIMESTAMP WITHOUT TIME ZONE,
                    notify_email BOOLEAN DEFAULT TRUE
                )
            """)
            conn.commit()
            print("✅ Tables created or already exist.")
        except Exception as e:
            print(f"❌ Error creating tables: {e}")
            conn.rollback()
        finally:
            cur.close()
            conn.close()

# Call to ensure tables exist
create_tables()

# Helper function to format time correctly
def format_time(value):
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}"
    elif isinstance(value, datetime):
        return value.strftime("%H:%M")
    elif isinstance(value, str):
        return value[:5]
    return ""

app.jinja_env.filters['format_time'] = format_time

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password'].encode('utf-8')
        hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode('utf-8')

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (email, password) VALUES (%s, %s)", (email, hashed))
        conn.commit()
        cur.close()
        conn.close()

        send_welcome_email(email)
        return redirect(url_for('home'))
    return render_template('register.html')

@app.route('/login', methods=['POST'])
def login():
    email = request.form['email']
    password = request.form['password'].encode('utf-8')

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, email, password FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    if user and bcrypt.checkpw(password, user[2].encode('utf-8')):
        session['user_id'] = user[0]
        session.permanent = True
        return redirect(url_for('dashboard'))
    else:
        return """
            <script>
                alert("Invalid credentials");
                window.location.href='/';
            </script>
        """

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_agent = request.headers.get('User-Agent').lower()
    is_mobile = 'mobile' in user_agent or 'android' in user_agent or 'iphone' in user_agent

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name, dosage_per_day, total_pills, schedule, last_taken FROM medications WHERE user_id = %s", (user_id,))
    meds = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    medications = []
    for row in meds:
        med = dict(zip(columns, row))
        if isinstance(med['schedule'], timedelta):
            seconds = med['schedule'].total_seconds()
            hours, remainder = divmod(seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            med['schedule_str'] = f"{int(hours):02d}:{int(minutes):02d}"
        elif med['schedule']:
            med['schedule_str'] = str(med['schedule'])[:5]
        else:
            med['schedule_str'] = 'N/A'
        medications.append(med)

    notifications = []
    current_time = datetime.now().strftime("%H:%M")

    for med in medications:
        if med['schedule_str'] != 'N/A' and med['schedule_str'] <= current_time:
            if med['last_taken'] != date.today() and med['total_pills'] > 0:
                notifications.append({
                    "type": "info",
                    "message": f"⏰ It's time to take {med['name']} ({med['dosage_per_day']} pill(s))."
                })
        if med['total_pills'] < med['dosage_per_day'] * 3 and med['total_pills'] > 0:
            notifications.append({
                "type": "warning",
                "message": f"⏳ Low pills for {med['name']}. Only {med['total_pills']} left."
            })
        if med['total_pills'] <= 0:
            notifications.append({
                "type": "danger",
                "message": f"❌ You're out of pills for {med['name']}. Please refill."
            })

    cur.close()
    conn.close()
    return render_template('dashboard.html', medications=medications, notifications=notifications, is_mobile=is_mobile)

@app.route('/add_medication')
def add_medication():
    if 'user_id' not in session:
        return redirect(url_for('home'))
    return render_template('add_medication.html')

@app.route('/save_medication', methods=['POST'])
def save_medication():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_id = session['user_id']
    name = request.form['name']
    total_pills = request.form['total_pills']
    dosage_per_day = request.form['dosage_per_day']
    schedule = request.form['schedule']

    now = datetime.now()
    hour, minute = map(int, schedule.split(':'))
    next_reminder = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_reminder < now:
        next_reminder += timedelta(days=1)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO medications (user_id, name, total_pills, dosage_per_day, schedule, next_reminder)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_id, name, total_pills, dosage_per_day, schedule, next_reminder))
    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for('dashboard'))

@app.route('/take_medication', methods=['POST'])
def take_medication():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    med_id = request.form['med_id']
    user_id = session['user_id']

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT total_pills, dosage_per_day FROM medications WHERE id = %s AND user_id = %s", (med_id, user_id))
    med = cur.fetchone()

    if med:
        total_pills, dosage = med[0], med[1]
        new_count = total_pills - dosage
        today = date.today()

        if new_count <= 0:
            cur.execute("DELETE FROM medications WHERE id = %s AND user_id = %s", (med_id, user_id))
            flash("⚠️ You're out of pills. Please refill.")
        else:
            cur.execute("""
                UPDATE medications
                SET total_pills = %s, last_taken = %s
                WHERE id = %s AND user_id = %s
            """, (new_count, today, med_id, user_id))

        conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for('dashboard'))

@app.route('/statistics')
def statistics():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, dosage_per_day, total_pills, last_taken FROM medications WHERE user_id = %s", (user_id,))
    meds = cur.fetchall()
    stats = {
        "total_medications": len(meds),
        "total_doses_scheduled": 0,
        "total_doses_taken": 0,
        "missed_doses": 0,
        "adherence_rate": 0
    }

    for med in meds:
        name, dosage, total_pills, last_taken = med
        if last_taken:
            days_prescribed = (datetime.now().date() - last_taken).days
            scheduled_doses = max(0, days_prescribed * dosage)
            taken_doses = total_pills // dosage if total_pills else 0
        else:
            scheduled_doses = 0
            taken_doses = 0
        stats["total_doses_scheduled"] += scheduled_doses
        stats["total_doses_taken"] += taken_doses

    stats["missed_doses"] = max(0, stats["total_doses_scheduled"] - stats["total_doses_taken"])
    if stats["total_doses_scheduled"] > 0:
        stats["adherence_rate"] = round((stats["total_doses_taken"] / stats["total_doses_scheduled"]) * 100, 2)

    cur.close()
    conn.close()
    return render_template('statistics.html', stats=stats)

@app.route('/export_statistics/csv')
def export_csv():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, dosage_per_day, total_pills, last_taken FROM medications WHERE user_id = %s", (user_id,))
    meds = cur.fetchall()
    df = pd.DataFrame(meds, columns=['Name', 'Dosage per Day', 'Total Pills', 'Last Taken'])

    si = StringIO()
    df.to_csv(si, index=False)
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=medication_stats.csv"
    output.headers["Content-type"] = "text/csv"
    cur.close()
    conn.close()
    return output

@app.route('/export_statistics/pdf')
def export_pdf():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, dosage_per_day, total_pills, last_taken FROM medications WHERE user_id = %s", (user_id,))
    meds = cur.fetchall()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer)
    styles = getSampleStyleSheet()
    story = [Paragraph("📄 Medication Report", styles['Title']), Spacer(1, 24)]

    for med in meds:
        name, dosage, total, last_taken = med
        last_taken_str = last_taken.strftime("%Y-%m-%d") if last_taken else "N/A"
        med_info = f"<b>Name:</b> {name}<br/><b>Dosage/Day:</b> {dosage}<br/><b>Total Pills:</b> {total}<br/><b>Last Taken:</b> {last_taken_str}"
        story.append(Paragraph(med_info, styles['Normal']))
        story.append(Spacer(1, 12))

    doc.build(story)
    pdf_output = buffer.getvalue()
    buffer.close()
    response = make_response(pdf_output)
    response.headers['Content-Disposition'] = 'attachment; filename=medication_report.pdf'
    response.headers['Content-Type'] = 'application/pdf'
    cur.close()
    conn.close()
    return response

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('home'))

# Scheduler for reminders
def send_reminder_emails():
    with app.app_context():
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            today = date.today()
            cur.execute("SELECT * FROM medications WHERE notify_email = TRUE AND next_reminder <= NOW()")
            meds = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            meds_with_cols = [dict(zip(columns, row)) for row in meds]

            now = datetime.now()
            for med in meds_with_cols:
                if med['last_taken'] != today and med['total_pills'] > 0:
                    user_id = med['user_id']
                    cur.execute("SELECT email FROM users WHERE id = %s", (user_id,))
                    user = cur.fetchone()
                    if user:
                        msg = Message(
                            subject="⏰ Medication Reminder: " + med['name'],
                            sender=email_config.EMAIL_HOST_USER,
                            recipients=[user[0]]
                        )
                        msg.body = f"""
Hi there,
It's time to take your medication "{med['name']}".
Dosage: {med['dosage_per_day']} pill(s) per day.
Please log in to confirm you've taken it.
Best regards,
Medication Tracker Team
                        """
                        mail.send(msg)

                # Reschedule reminder
                schedule_str = str(med['schedule'])[:5]
                hour, minute = map(int, schedule_str.split(':'))
                next_reminder = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if next_reminder <= now:
                    next_reminder += timedelta(days=1)
                cur.execute("UPDATE medications SET next_reminder = %s WHERE id = %s", (next_reminder, med['id']))
                conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Scheduler error: {e}")

# Start background scheduler
scheduler_started = False

def start_scheduler():
    global scheduler_started
    if not scheduler_started:
        scheduler = BackgroundScheduler()
        scheduler.add_job(send_reminder_emails, 'interval', minutes=1)
        scheduler.start()
        scheduler_started = True
        atexit.register(lambda: scheduler.shutdown())

@app.route('/education')
def education():
    if 'user_id' not in session:
        return redirect(url_for('home'))
    return render_template('education.html')

@app.route('/safety')
def safety():
    if 'user_id' not in session:
        return redirect(url_for('home'))
    return render_template('safety.html')

@app.route('/print_guide')
def print_guide():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name, dosage_per_day, total_pills, schedule, last_taken, description FROM medications WHERE user_id = %s", (user_id,))
    meds = cur.fetchall()
    columns = [desc[0] for desc in cur.description]
    medications = []

    for row in meds:
        med = dict(zip(columns, row))
        if isinstance(med['schedule'], timedelta):
            seconds = med['schedule'].total_seconds()
            hours, remainder = divmod(seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            med['schedule_str'] = f"{int(hours):02d}:{int(minutes):02d}"
        elif med['schedule']:
            med['schedule_str'] = str(med['schedule'])[:5]
        else:
            med['schedule_str'] = 'N/A'
        medications.append(med)

    cur.close()
    conn.close()
    return render_template('print_guide.html', medications=medications)

@app.route('/delete_medication', methods=['POST'])
def delete_medication():
    if 'user_id' not in session:
        return redirect(url_for('home'))

    med_id = request.form['med_id']
    user_id = session['user_id']

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM medications WHERE id = %s AND user_id = %s", (med_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

    return redirect(url_for('dashboard'))

def send_welcome_email(email):
    msg = Message(
        subject="🎉 Welcome to Medication Tracker!",
        sender=email_config.EMAIL_HOST_USER,
        recipients=[email]
    )
    msg.body = """
Hi there,
Welcome to the Medication Tracker App!
We're excited to help you manage your medication schedule more effectively.
Please log in to add your first medication.
Best regards,
Medication Tracker Team
"""
    mail.send(msg)

# Run the app
if __name__ == '__main__':
    start_scheduler()
    app.run(host='0.0.0.0', port=5000, debug=False)
