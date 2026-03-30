from flask_login import current_user
import os
import re
import json
import csv
import pdfplumber
import docx2txt

from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import func

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import cm
from PyPDF2 import PdfReader

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def extract_text_from_pdf(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text.strip()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / 'uploads'
REPORT_FOLDER = BASE_DIR / 'reports'
ALLOWED_EXTENSIONS = {'pdf', 'docx', 'txt'}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY", "dev-jwt-secret")
app.config["JWT_TOKEN_LOCATION"] = ["cookies"]
app.config["JWT_COOKIE_SECURE"] = True
app.config["JWT_COOKIE_CSRF_PROTECT"] = False

def normalize_database_url(url: str) -> str:
    if not url:
        return f"sqlite:///{BASE_DIR / 'hirescorex.db'}"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

db_url = normalize_database_url(os.getenv('DATABASE_URL', ''))
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['REPORT_FOLDER'] = str(REPORT_FOLDER)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='candidate')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    analyses = db.relationship('Analysis', backref='user', lazy=True)


class Analysis(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    company_name = db.Column(db.String(120))
    role_title = db.Column(db.String(120), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    extracted_text = db.Column(db.Text, nullable=False)
    jd_text = db.Column(db.Text, nullable=False)
    score_overall = db.Column(db.Integer, nullable=False)
    score_skills = db.Column(db.Integer, nullable=False)
    score_experience = db.Column(db.Integer, nullable=False)
    score_projects = db.Column(db.Integer, nullable=False)
    score_education = db.Column(db.Integer, nullable=False)
    score_keywords = db.Column(db.Integer, nullable=False)
    matched_skills = db.Column(db.Text, default='[]')
    missing_skills = db.Column(db.Text, default='[]')
    strong_points = db.Column(db.Text, default='[]')
    weak_points = db.Column(db.Text, default='[]')
    suggestions = db.Column(db.Text, default='[]')
    interview_questions = db.Column(db.Text, default='[]')
    ai_summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_lists(self):
        return {
            'matched_skills': json.loads(self.matched_skills or '[]'),
            'missing_skills': json.loads(self.missing_skills or '[]'),
            'strong_points': json.loads(self.strong_points or '[]'),
            'weak_points': json.loads(self.weak_points or '[]'),
            'suggestions': json.loads(self.suggestions or '[]'),
            'interview_questions': json.loads(self.interview_questions or '[]'),
        }


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == '.pdf':
        reader = PdfReader(str(file_path))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or '')
        return '\n'.join(parts).strip()
    if suffix == '.docx':
        return docx2txt.process(str(file_path)) or ''
    return file_path.read_text(encoding='utf-8', errors='ignore')


def normalize_words(text: str):
    return set(re.findall(r'[a-zA-Z][a-zA-Z0-9+#.\-]{1,}', text.lower()))


ROLE_SKILLS = {
    'software engineer': ['python', 'java', 'c++', 'javascript', 'react', 'node', 'sql', 'api', 'git', 'docker', 'flask', 'django', 'system design', 'oop'],
    'frontend developer': ['html', 'css', 'javascript', 'react', 'typescript', 'tailwind', 'figma', 'redux', 'vite', 'responsive', 'accessibility'],
    'backend developer': ['node', 'express', 'django', 'flask', 'postgresql', 'mongodb', 'redis', 'docker', 'api', 'jwt', 'microservices'],
    'full stack developer': ['react', 'node', 'express', 'mongodb', 'postgresql', 'javascript', 'api', 'git', 'html', 'css', 'docker'],
    'data analyst': ['python', 'sql', 'excel', 'power bi', 'pandas', 'visualization', 'statistics', 'dashboard', 'etl'],
    'devops engineer': ['docker', 'kubernetes', 'aws', 'ci/cd', 'linux', 'terraform', 'monitoring'],
}

ACTION_VERBS = ['built', 'developed', 'implemented', 'optimized', 'designed', 'integrated', 'deployed', 'improved']


def extract_skills_from_jd(jd_text: str, role_title: str):
    words = normalize_words(jd_text)
    role_lower = role_title.lower().strip()
    base = []
    for key, vals in ROLE_SKILLS.items():
        if key in role_lower:
            base.extend(vals)
    if not base:
        base = sorted({v for vals in ROLE_SKILLS.values() for v in vals})
    found = [skill for skill in base if skill.lower() in jd_text.lower() or skill.lower() in words]
    return sorted(set(found or base[:8]))


def section_signals(text: str):
    lower = text.lower()
    return {
        'experience': any(k in lower for k in ['experience', 'intern', 'worked', 'employment']),
        'projects': any(k in lower for k in ['project', 'github', 'built', 'developed']),
        'education': any(k in lower for k in ['education', 'bca', 'b.tech', 'bachelor', 'university', 'college']),
    }


def score_resume(resume_text: str, jd_text: str, role_title: str):
    resume_lower = resume_text.lower()
    jd_lower = jd_text.lower()
    jd_skills = extract_skills_from_jd(jd_text, role_title)

    matched = [s for s in jd_skills if s.lower() in resume_lower]
    missing = [s for s in jd_skills if s.lower() not in resume_lower]

    sig = section_signals(resume_text)
    jd_words = normalize_words(jd_text)
    resume_words = normalize_words(resume_text)
    keyword_overlap = len(jd_words & resume_words)
    keyword_ratio = min(100, int((keyword_overlap / max(1, len(jd_words))) * 160))

    skills_score = min(100, int((len(matched) / max(1, len(jd_skills))) * 100))
    exp_score = 85 if sig['experience'] else 40
    projects_score = 85 if sig['projects'] else 45
    edu_score = 80 if sig['education'] else 50
    keyword_score = max(35, keyword_ratio)

    overall = int(skills_score * 0.4 + exp_score * 0.2 + projects_score * 0.15 + edu_score * 0.1 + keyword_score * 0.15)
    overall = max(35, min(96, overall))

    strong_points = []
    weak_points = []
    suggestions = []

    if matched:
        strong_points.append(f"Resume matches {len(matched)} important role keywords.")
    if sig['projects']:
        strong_points.append('Projects section is present, which improves fresher profile strength.')
    if sig['experience']:
        strong_points.append('Experience or internship indicators were detected.')

    if missing:
        weak_points.append(f"Missing role-focused keywords: {', '.join(missing[:6])}.")
        suggestions.append(f"Add these keywords naturally where relevant: {', '.join(missing[:6])}.")
    if not sig['experience']:
        weak_points.append('Experience section is weak or not clearly labeled.')
        suggestions.append('Add internship, training, or responsibility-based bullet points under experience.')
    if not sig['projects']:
        weak_points.append('Projects are not clearly highlighted.')
        suggestions.append('Add 2-3 strong project bullets with tools, impact, and outcomes.')
    suggestions.append('Use stronger action verbs in project bullets such as: ' + ', '.join(ACTION_VERBS[:5]) + '.')
    suggestions.append('Tailor summary and skills section to the exact target role before applying.')

    questions = [
        f"Walk me through your most relevant project for the {role_title} role.",
        f"Which skills in your resume best match this {role_title} job description?",
        'Tell me about a technical challenge you faced and how you solved it.',
        'How would you improve your resume further for this role?',
        'Why do you want this role and what value can you bring?'
    ]

    return {
        'overall': overall,
        'skills_score': skills_score,
        'experience_score': exp_score,
        'projects_score': projects_score,
        'education_score': edu_score,
        'keyword_score': keyword_score,
        'matched': matched,
        'missing': missing,
        'strong_points': strong_points,
        'weak_points': weak_points,
        'suggestions': suggestions,
        'questions': questions,
    }




def safe_json_list(value):
    try:
        return json.loads(value or '[]')
    except Exception:
        return []


def role_category(role_title: str) -> str:
    title = (role_title or '').lower()
    mapping = [
        ('frontend', 'Frontend'),
        ('backend', 'Backend'),
        ('full stack', 'Full Stack'),
        ('software', 'Software'),
        ('data', 'Data'),
        ('analyst', 'Data'),
        ('marketing', 'Marketing'),
        ('finance', 'Finance'),
        ('civil', 'Civil'),
        ('electrical', 'Electrical'),
    ]
    for key, label in mapping:
        if key in title:
            return label
    return 'General'


def fit_label(score: int) -> str:
    if score >= 85:
        return 'Excellent Fit'
    if score >= 72:
        return 'Strong Fit'
    if score >= 60:
        return 'Moderate Fit'
    return 'Needs Work'


def score_tone(score: int) -> str:
    if score >= 80:
        return 'teal'
    if score >= 65:
        return 'indigo'
    return 'coral'




def readiness_checklist(score: int, analysis: Analysis):
    items = []
    lists = analysis.to_lists()
    items.append({
        'label': 'Overall role fit is interview-worthy',
        'done': score >= 72,
    })
    items.append({
        'label': 'Core skill coverage is strong',
        'done': analysis.score_skills >= 70,
    })
    items.append({
        'label': 'Projects are clearly visible',
        'done': analysis.score_projects >= 70,
    })
    items.append({
        'label': 'Resume has enough role keywords',
        'done': analysis.score_keywords >= 65,
    })
    items.append({
        'label': 'Top missing skills are reduced to 3 or fewer',
        'done': len(lists.get('missing_skills', [])) <= 3,
    })
    return items


def benchmark_percentile(score: int) -> int:
    if score >= 90:
        return 95
    if score >= 85:
        return 90
    if score >= 80:
        return 84
    if score >= 72:
        return 74
    if score >= 60:
        return 56
    return 35


def serialize_analysis(analysis: Analysis):
    lists = analysis.to_lists()
    return {
        'id': analysis.id,
        'candidate': analysis.user.full_name,
        'candidate_email': analysis.user.email,
        'role_title': analysis.role_title,
        'company_name': analysis.company_name,
        'created_at': analysis.created_at.isoformat(),
        'fit_label': fit_label(analysis.score_overall),
        'fit_tone': score_tone(analysis.score_overall),
        'role_category': role_category(analysis.role_title),
        'scores': {
            'overall': analysis.score_overall,
            'skills': analysis.score_skills,
            'experience': analysis.score_experience,
            'projects': analysis.score_projects,
            'education': analysis.score_education,
            'keywords': analysis.score_keywords,
        },
        'matched_skills': lists.get('matched_skills', []),
        'missing_skills': lists.get('missing_skills', []),
        'strong_points': lists.get('strong_points', []),
        'weak_points': lists.get('weak_points', []),
        'suggestions': lists.get('suggestions', []),
        'interview_questions': lists.get('interview_questions', []),
        'ai_summary': analysis.ai_summary or '',
        'fit_index': rank_candidate(analysis),
        'benchmark_percentile': benchmark_percentile(analysis.score_overall),
    }

def build_dashboard_metrics(analyses):
    total = len(analyses)
    best = max((a.score_overall for a in analyses), default=0)
    avg = int(sum((a.score_overall for a in analyses), 0) / total) if total else 0
    interview_count = len([a for a in analyses if safe_json_list(a.interview_questions)])
    total_missing = {}
    category_mix = {}
    for item in analyses:
        for skill in safe_json_list(item.missing_skills):
            total_missing[skill] = total_missing.get(skill, 0) + 1
        cat = role_category(item.role_title)
        category_mix[cat] = category_mix.get(cat, 0) + 1
    top_missing = sorted(total_missing.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    top_categories = sorted(category_mix.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        'total': total,
        'best': best,
        'avg': avg,
        'interviews': interview_count,
        'fit_label': fit_label(avg),
        'fit_tone': score_tone(avg),
        'top_missing': top_missing,
        'top_categories': top_categories,
    }


def rank_candidate(analysis):
    fit = (
        analysis.score_overall * 0.55
        + analysis.score_skills * 0.2
        + analysis.score_projects * 0.1
        + analysis.score_experience * 0.1
        + analysis.score_keywords * 0.05
    )
    matched_bonus = min(8, len(safe_json_list(analysis.matched_skills)))
    return round(fit + matched_bonus, 1)

def make_prompt_excerpt(text: str, limit: int = 1800) -> str:
    cleaned = re.sub(r'\s+', ' ', text or '').strip()
    return cleaned[:limit]


def get_ai_summary(role_title: str, company_name: str, scores: dict, matched: list, missing: list, suggestions: list, resume_text: str = '', jd_text: str = ''):
    api_key = os.getenv('OPENAI_API_KEY')
    model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
    if api_key and OpenAI is not None:
        try:
            client = OpenAI(api_key=api_key)
            system_prompt = (
                'You are a hiring copilot for a resume intelligence platform. '
                'Return exactly 5 short bullet points. Focus on recruiter usefulness, plain English, '
                'specific strengths, gaps, and next steps. Avoid hype. Avoid markdown headings.'
            )
            user_prompt = f"""Role: {role_title}
Company: {company_name or 'Not specified'}
Overall Score: {scores['overall']}
Skills Score: {scores['skills_score']}
Experience Score: {scores['experience_score']}
Projects Score: {scores['projects_score']}
Education Score: {scores['education_score']}
Keyword Score: {scores['keyword_score']}
Matched Skills: {', '.join(matched[:12]) or 'None'}
Missing Skills: {', '.join(missing[:12]) or 'None'}
Top Suggestions: {', '.join(suggestions[:6])}
Resume Excerpt: {make_prompt_excerpt(resume_text)}
Job Description Excerpt: {make_prompt_excerpt(jd_text)}

Write 5 bullet points covering:
1) overall fit
2) strongest evidence from resume
3) biggest gaps
4) how recruiter may view the candidate
5) best next improvement"""
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                temperature=0.2,
            )
            content = (resp.choices[0].message.content or '').strip()
            if content:
                return content
        except Exception:
            pass
    lines = [
        f"Overall fit for {role_title} at {company_name or 'the target company'} is {scores['overall']}%, which indicates a {fit_label(scores['overall']).lower()}.",
        f"Strongest evidence comes from: {', '.join(matched[:5]) if matched else 'transferable technical exposure and visible project work'}.",
        f"Biggest gaps to close: {', '.join(missing[:5]) if missing else 'no major missing core keywords detected'}.",
        'A recruiter would likely see this profile as more credible if resume bullets show impact, ownership, and measurable results.',
        f"Best next step: {suggestions[0] if suggestions else 'Tailor the summary, skills, and projects for the exact job title.'}"
    ]
    return '\n'.join(f"• {line}" for line in lines)


def get_interview_questions(role_title: str, matched: list, missing: list, resume_text: str = '', jd_text: str = ''):
    api_key = os.getenv('OPENAI_API_KEY')
    model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')
    if api_key and OpenAI is not None:
        try:
            client = OpenAI(api_key=api_key)
            system_prompt = 'Generate exactly 5 interview questions. Keep them role-specific, practical, and concise. Return one question per line with no numbering.'
            user_prompt = f"Role: {role_title}\nMatched Skills: {', '.join(matched[:10]) or 'None'}\nMissing Skills: {', '.join(missing[:10]) or 'None'}\nResume: {make_prompt_excerpt(resume_text, 1200)}\nJD: {make_prompt_excerpt(jd_text, 1200)}"
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                temperature=0.3,
            )
            content = (resp.choices[0].message.content or '').strip()
            lines = [line.strip('•- ').strip() for line in content.splitlines() if line.strip()]
            cleaned = [line for line in lines if line.endswith('?') or len(line) > 15]
            if cleaned:
                return cleaned[:5]
        except Exception:
            pass
    return [
        f"Walk me through your most relevant project for the {role_title} role.",
        f"Which skills in your resume best match this {role_title} job description?",
        'Tell me about a technical challenge you faced and how you solved it.',
        'How would you improve your resume further for this role?',
        'Why do you want this role and what value can you bring?',
    ]


def generate_report(analysis: Analysis):
    filename = f"report_{analysis.id}.pdf"
    out_path = REPORT_FOLDER / filename
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(out_path), pagesize=A4, rightMargin=1.5*cm, leftMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []
    story.append(Paragraph('HireScoreX Analysis Report', styles['Title']))
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Candidate: {analysis.user.full_name}", styles['Normal']))
    story.append(Paragraph(f"Role: {analysis.role_title}", styles['Normal']))
    story.append(Paragraph(f"Company: {analysis.company_name or 'Not specified'}", styles['Normal']))
    story.append(Paragraph(f"Date: {analysis.created_at.strftime('%d %b %Y, %I:%M %p')}", styles['Normal']))
    story.append(Spacer(1, 12))
    data = [
        ['Metric', 'Score'],
        ['Overall', f"{analysis.score_overall}%"],
        ['Skills', f"{analysis.score_skills}%"],
        ['Experience', f"{analysis.score_experience}%"],
        ['Projects', f"{analysis.score_projects}%"],
        ['Education', f"{analysis.score_education}%"],
        ['Keywords', f"{analysis.score_keywords}%"],
    ]
    table = Table(data, colWidths=[8*cm, 4*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#4f46e5')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#f8fafc')),
        ('PADDING', (0,0), (-1,-1), 8),
    ]))
    story.append(table)
    story.append(Spacer(1, 14))
    lists = analysis.to_lists()
    for title, items in [('Matched Skills', lists['matched_skills']), ('Missing Skills', lists['missing_skills']), ('Strong Points', lists['strong_points']), ('Suggestions', lists['suggestions'])]:
        story.append(Paragraph(title, styles['Heading2']))
        if items:
            for item in items:
                story.append(Paragraph(f"• {item}", styles['Normal']))
        else:
            story.append(Paragraph('• None', styles['Normal']))
        story.append(Spacer(1, 8))
    story.append(Paragraph('AI Summary', styles['Heading2']))
    for line in (analysis.ai_summary or '').splitlines():
        story.append(Paragraph(line, styles['Normal']))
    doc.build(story)
    return filename


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        role = request.form.get('role', 'candidate')
        if not full_name or not email or not password:
            flash('Please fill all required fields.', 'error')
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('register'))
        user = User(full_name=full_name, email=email, password_hash=generate_password_hash(password), role=role)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash('Account created successfully.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            flash('Welcome back.', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    analyses = Analysis.query.filter_by(user_id=current_user.id).order_by(Analysis.created_at.desc()).all()
    metrics = build_dashboard_metrics(analyses)
    recent_scores = [a.score_overall for a in analyses[:6]][::-1]
    recent_labels = [a.role_title[:18] for a in analyses[:6]][::-1]
    category_labels = [label for label, _ in metrics['top_categories']] or ['No Data']
    category_values = [value for _, value in metrics['top_categories']] or [1]
    score_breakdown = {
        'skills': int(sum((a.score_skills for a in analyses), 0) / len(analyses)) if analyses else 0,
        'experience': int(sum((a.score_experience for a in analyses), 0) / len(analyses)) if analyses else 0,
        'projects': int(sum((a.score_projects for a in analyses), 0) / len(analyses)) if analyses else 0,
        'education': int(sum((a.score_education for a in analyses), 0) / len(analyses)) if analyses else 0,
        'keywords': int(sum((a.score_keywords for a in analyses), 0) / len(analyses)) if analyses else 0,
    }
    spotlight = analyses[0] if analyses else None
    return render_template(
        'dashboard.html',
        analyses=analyses[:5],
        stats=metrics,
        recent_scores=recent_scores,
        recent_labels=recent_labels,
        category_labels=category_labels,
        category_values=category_values,
        score_breakdown=score_breakdown,
        spotlight=spotlight,
    )


@app.route('/analyze', methods=['GET', 'POST'])
@login_required
def analyze():
    if request.method == 'POST':
        role_title = request.form.get('role_title', '').strip()
        company_name = request.form.get('company_name', '').strip()
        jd_text = request.form.get('jd_text', '').strip()

        upload = request.files.get('resume_file')

        if not role_title or not jd_text or not upload:
            flash('Role, JD, and resume are required.', 'error')
            return redirect(url_for('analyze'))

        if not allowed_file(upload.filename):
            flash('Please upload PDF, DOCX, or TXT file.', 'error')
            return redirect(url_for('analyze'))

        filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secure_filename(upload.filename)}"
        path = UPLOAD_FOLDER / filename
        upload.save(path)

        try:
            resume_text = extract_text(path)
        except Exception as e:
            flash(f'Could not parse file: {e}', 'error')
            return redirect(url_for('analyze'))

        if not resume_text.strip():
            flash('Resume text could not be extracted.', 'error')
            return redirect(url_for('analyze'))

        scores = score_resume(resume_text, jd_text, role_title)
        ai_summary = get_ai_summary(
            role_title,
            company_name,
            scores,
            scores['matched'],
            scores['missing'],
            scores['suggestions']
        )
        questions = get_interview_questions(
            role_title,
            scores['matched'],
            scores['missing'],
            resume_text,
            jd_text
        )

        analysis = Analysis(
            user_id=current_user.id,
            company_name=company_name,
            role_title=role_title,
            filename=filename,
            extracted_text=resume_text,
            jd_text=jd_text,
            score_overall=scores['overall'],
            score_skills=scores['skills_score'],
            score_experience=scores['experience_score'],
            score_projects=scores['projects_score'],
            score_education=scores['education_score'],
            score_keywords=scores['keyword_score'],
            matched_skills=json.dumps(scores['matched']),
            missing_skills=json.dumps(scores['missing']),
            strong_points=json.dumps(scores['strong_points']),
            weak_points=json.dumps(scores['weak_points']),
            suggestions=json.dumps(scores['suggestions']),
            interview_questions=json.dumps(questions),
            ai_summary=ai_summary,
        )

        db.session.add(analysis)
        db.session.commit()
        generate_report(analysis)

        flash('Analysis completed successfully.', 'success')
        return redirect(url_for('analysis_detail', analysis_id=analysis.id))

    return render_template('analyze.html')


@app.route('/analysis/<int:analysis_id>')
@login_required
def analysis_detail(analysis_id):
    analysis = Analysis.query.get_or_404(analysis_id)
    if analysis.user_id != current_user.id and current_user.role != 'recruiter':
        abort(403)
    lists = analysis.to_lists()
    score_series = [analysis.score_skills, analysis.score_experience, analysis.score_projects, analysis.score_education, analysis.score_keywords]
    score_labels = ['Skills', 'Experience', 'Projects', 'Education', 'Keywords']
    checklist = readiness_checklist(analysis.score_overall, analysis)
    percentile = benchmark_percentile(analysis.score_overall)
    return render_template('analysis_detail.html', analysis=analysis, lists=lists, score_series=score_series, score_labels=score_labels, fit_label=fit_label(analysis.score_overall), fit_tone=score_tone(analysis.score_overall), checklist=checklist, percentile=percentile)


@app.route('/history')
@login_required
def history():
    query = Analysis.query.filter_by(user_id=current_user.id).order_by(Analysis.created_at.desc())
    role = request.args.get('role', '').strip()
    company = request.args.get('company', '').strip()
    minimum = request.args.get('min_score', '').strip()
    if role:
        query = query.filter(Analysis.role_title.ilike(f'%{role}%'))
    if company:
        query = query.filter(Analysis.company_name.ilike(f'%{company}%'))
    if minimum.isdigit():
        query = query.filter(Analysis.score_overall >= int(minimum))
    analyses = query.all()
    avg_score = int(sum((a.score_overall for a in analyses), 0) / len(analyses)) if analyses else 0
    return render_template('history.html', analyses=analyses, role=role, company=company, min_score=minimum, avg_score=avg_score)


@app.route('/recruiter')
@login_required
def recruiter():
    query = Analysis.query.join(User).order_by(Analysis.score_overall.desc(), Analysis.created_at.desc())
    role = request.args.get('role', '').strip()
    minimum = request.args.get('min_score', '').strip()
    skill = request.args.get('skill', '').strip().lower()
    company = request.args.get('company', '').strip()
    if role:
        query = query.filter(Analysis.role_title.ilike(f'%{role}%'))
    if company:
        query = query.filter(Analysis.company_name.ilike(f'%{company}%'))
    if minimum.isdigit():
        query = query.filter(Analysis.score_overall >= int(minimum))
    rows = query.all()
    if skill:
        rows = [r for r in rows if skill in (r.matched_skills or '').lower() or skill in (r.extracted_text or '').lower()]
    ranked_rows = []
    for idx, row in enumerate(rows, start=1):
        ranked_rows.append({
            'rank': idx,
            'analysis': row,
            'fit_index': rank_candidate(row),
            'fit_label': fit_label(row.score_overall),
            'fit_tone': score_tone(row.score_overall),
        })
    overview = {
        'count': len(ranked_rows),
        'top_score': ranked_rows[0]['analysis'].score_overall if ranked_rows else 0,
        'avg_fit': int(sum((r['analysis'].score_overall for r in ranked_rows), 0) / len(ranked_rows)) if ranked_rows else 0,
    }
    return render_template('recruiter.html', rows=ranked_rows, role=role, min_score=minimum, skill=skill, company=company, overview=overview)




@app.route('/healthz')
def healthz():
    return jsonify({
        'status': 'ok',
        'database': 'connected',
        'app': 'HireScoreX',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
    })


@app.route('/api/dashboard/summary')
@login_required
def api_dashboard_summary():
    analyses = Analysis.query.filter_by(user_id=current_user.id).order_by(Analysis.created_at.desc()).all()
    stats = build_dashboard_metrics(analyses)
    return jsonify({
        'stats': stats,
        'recent': [serialize_analysis(item) for item in analyses[:5]],
    })


@app.route('/api/analysis/<int:analysis_id>')
@login_required
def api_analysis_detail(analysis_id):
    analysis = Analysis.query.get_or_404(analysis_id)
    if analysis.user_id != current_user.id and current_user.role != 'recruiter':
        abort(403)
    payload = serialize_analysis(analysis)
    payload['readiness_checklist'] = readiness_checklist(analysis.score_overall, analysis)
    return jsonify(payload)


@app.route('/recruiter/export.csv')
@login_required
def recruiter_export_csv():
    if current_user.role != 'recruiter':
        abort(403)
    query = Analysis.query.join(User).order_by(Analysis.score_overall.desc(), Analysis.created_at.desc())
    role = request.args.get('role', '').strip()
    minimum = request.args.get('min_score', '').strip()
    skill = request.args.get('skill', '').strip().lower()
    company = request.args.get('company', '').strip()
    if role:
        query = query.filter(Analysis.role_title.ilike(f'%{role}%'))
    if company:
        query = query.filter(Analysis.company_name.ilike(f'%{company}%'))
    if minimum.isdigit():
        query = query.filter(Analysis.score_overall >= int(minimum))
    rows = query.all()
    if skill:
        rows = [r for r in rows if skill in (r.matched_skills or '').lower() or skill in (r.extracted_text or '').lower()]

    def generate():
        header = ['candidate', 'email', 'role', 'company', 'overall_score', 'fit_index', 'fit_label', 'created_at']
        yield ','.join(header) + '\n'
        for row in rows:
            values = [
                row.user.full_name,
                row.user.email,
                row.role_title,
                row.company_name or '',
                str(row.score_overall),
                str(rank_candidate(row)),
                fit_label(row.score_overall),
                row.created_at.isoformat(),
            ]
            escaped = []
            for value in values:
                value = value.replace('"', '""')
                escaped.append(f'"{value}"')
            yield ','.join(escaped) + '\n'

    return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=recruiter_candidates.csv'})

@app.route('/report/<int:analysis_id>')
@login_required
def report(analysis_id):
    analysis = Analysis.query.get_or_404(analysis_id)
    if analysis.user_id != current_user.id and current_user.role != 'recruiter':
        abort(403)
    filename = f"report_{analysis.id}.pdf"
    path = REPORT_FOLDER / filename
    if not path.exists():
        generate_report(analysis)
    return send_from_directory(REPORT_FOLDER, filename, as_attachment=True)


def seed_demo_data():
    if User.query.first():
        return
    candidate = User(full_name='Demo Candidate', email='demo@hirescorex.com', password_hash=generate_password_hash('demo1234'), role='candidate')
    recruiter = User(full_name='Demo Recruiter', email='recruiter@hirescorex.com', password_hash=generate_password_hash('demo1234'), role='recruiter')
    db.session.add_all([candidate, recruiter])
    db.session.commit()

    samples = [
        {
            'company': 'Google',
            'role': 'Software Engineer Intern',
            'resume': 'BCA student with projects in Python, Flask, SQL, React, APIs, Git, and dashboard development. Built HireScoreX and TrackFix. Internship experience in web development with responsive UI and backend integration.',
            'jd': 'Looking for a software engineer intern with Python, data structures, APIs, SQL, Git, debugging, and problem solving. Experience with web development, testing, and scalable system thinking is a plus.'
        },
        {
            'company': 'Amazon',
            'role': 'Frontend Developer',
            'resume': 'Frontend-focused developer with HTML, CSS, JavaScript, React, Tailwind, Vite, responsive design, and UI improvement work. Built dashboard interfaces and login flows.',
            'jd': 'Need a frontend developer with React, TypeScript, JavaScript, responsive design, performance optimization, accessibility, Git, and API integration.'
        },
        {
            'company': 'Microsoft',
            'role': 'Full Stack Developer',
            'resume': 'Built full-stack apps using React, Node.js, Express, MongoDB, PostgreSQL, authentication, dashboards, PDF parsing, and API-driven analysis tools.',
            'jd': 'Seeking a full stack developer with React, Node, Express, PostgreSQL, Docker, API design, authentication, testing, and cloud deployment basics.'
        },
    ]

    for item in samples:
        scores = score_resume(item['resume'], item['jd'], item['role'])
        ai_summary = get_ai_summary(item['role'], item['company'], scores, scores['matched'], scores['missing'], scores['suggestions'], item['resume'], item['jd'])
        questions = get_interview_questions(item['role'], scores['matched'], scores['missing'], item['resume'], item['jd'])
        analysis = Analysis(
            user_id=candidate.id,
            company_name=item['company'],
            role_title=item['role'],
            filename='demo_resume.txt',
            extracted_text=item['resume'],
            jd_text=item['jd'],
            score_overall=scores['overall'],
            score_skills=scores['skills_score'],
            score_experience=scores['experience_score'],
            score_projects=scores['projects_score'],
            score_education=scores['education_score'],
            score_keywords=scores['keyword_score'],
            matched_skills=json.dumps(scores['matched']),
            missing_skills=json.dumps(scores['missing']),
            strong_points=json.dumps(scores['strong_points']),
            weak_points=json.dumps(scores['weak_points']),
            suggestions=json.dumps(scores['suggestions']),
            interview_questions=json.dumps(questions),
            ai_summary=ai_summary,
        )
        db.session.add(analysis)
        db.session.commit()
        generate_report(analysis)


@app.context_processor
def inject_now():
    return {'now_year': datetime.utcnow().year}


if __name__ == '__main__':
    UPLOAD_FOLDER.mkdir(exist_ok=True)
    REPORT_FOLDER.mkdir(exist_ok=True)
    with app.app_context():
        db.create_all()
        seed_demo_data()
    app.run(debug=True)

@app.before_request
def create_tables():
    db.create_all()