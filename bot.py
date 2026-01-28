import os
import re
import asyncio
import logging
import threading
import time
import json
import random
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
RENDER_APP_URL = os.getenv('RENDER_APP_URL', '')

# States for conversation
GETTING_FILE, GETTING_QUIZ_NAME, GETTING_TIME, GETTING_MARKS, GETTING_NEGATIVE, GETTING_CREATOR = range(6)

# Store user data
user_data = {}
user_progress = {}
last_activity = time.time()

# Keep-alive configuration
KEEP_ALIVE_INTERVAL = 5 * 60  # Ping every 5 minutes

def create_progress_bar(current, total, bar_length=20):
    """Create a visual progress bar"""
    progress = current / total
    filled_length = int(bar_length * progress)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    percentage = int(progress * 100)
    return f"{bar} {percentage}%"

# Simple HTTP server for health checks
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global last_activity
        last_activity = time.time()
        
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        elif self.path == '/wake':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'AWAKE')
        elif self.path == '/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            status = {
                'status': 'running',
                'last_activity': datetime.fromtimestamp(last_activity).isoformat(),
                'active_users': len(user_data)
            }
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        return

def run_health_server():
    """Run a simple HTTP server for health checks"""
    port = int(os.getenv('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    logger.info(f"Health server running on port {port}")
    server.serve_forever()

def keep_alive_ping():
    """Ping the app itself to keep it awake"""
    if RENDER_APP_URL:
        try:
            # Send multiple pings to ensure wake-up
            for i in range(3):
                try:
                    response = requests.get(f"{RENDER_APP_URL}/wake", timeout=5)
                    logger.info(f"Keep-alive ping {i+1}: {response.status_code}")
                except:
                    pass
                time.sleep(1)
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")

def keep_alive_worker():
    """Background thread to keep the app alive"""
    logger.info("Keep-alive worker started")
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        keep_alive_ping()

def update_activity():
    """Update the last activity timestamp"""
    global last_activity
    last_activity = time.time()

def parse_txt_file(content):
    """Parse various TXT file formats and extract questions"""
    questions = []
    
    # Try different parsing patterns
    lines = content.strip().split('\n')
    
    current_question = None
    current_options = []
    current_explanation = []
    in_question = False
    in_options = False
    in_explanation = False
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Check for question start (various formats)
        question_match = re.match(r'^(\d+)[\.\)]\s*(.+)$', line) or \
                        re.match(r'^Q[\.]?\s*(\d+)[\.\)]?\s*(.+)$', line) or \
                        re.match(r'^Q[\.]?\s*(\d+)[:\-]?\s*(.+)$', line)
        
        if question_match:
            # Save previous question if exists
            if current_question:
                question_data = {
                    "question": current_question,
                    "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
                    "answer": "",
                    "solution_text": "<br>".join(current_explanation) if current_explanation else ""
                }
                
                # Add options
                for i, option in enumerate(current_options[:5]):
                    question_data[f"option_{i+1}"] = option
                
                questions.append(question_data)
                
            # Start new question
            current_question = question_match.group(2)
            current_options = []
            current_explanation = []
            in_question = True
            in_options = False
            in_explanation = False
            continue
        
        # Check for options (multiple formats)
        option_match = re.match(r'^([a-e])[\.\)]\s*(.+)$', line) or \
                      re.match(r'^\(([a-e])\)\s*(.+)$', line) or \
                      re.match(r'^([a-e])\s*[-:]\s*(.+)$', line)
        
        if option_match:
            in_options = True
            option_text = option_match.group(2)
            
            # Check if next line is part of this option (Hindi text)
            current_options.append(option_text)
            continue
        
        # Check for answer (multiple formats)
        answer_match = re.match(r'^Correct\s*[:\-]?\s*([a-e])', line, re.IGNORECASE) or \
                      re.match(r'^Answer\s*[:\-]?\s*\(?([a-e])\)?', line, re.IGNORECASE) or \
                      re.match(r'^Ans\s*[:\-]?\s*\(?([a-e])\)?', line, re.IGNORECASE)
        
        if answer_match:
            if current_question and current_options:
                question_data = {
                    "question": current_question,
                    "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
                    "answer": answer_match.group(1).lower(),
                    "solution_text": "<br>".join(current_explanation) if current_explanation else ""
                }
                
                # Add options
                for i, option in enumerate(current_options[:5]):
                    question_data[f"option_{i+1}"] = option
                
                questions.append(question_data)
                
            # Reset for next question
            current_question = None
            current_options = []
            current_explanation = []
            in_question = False
            in_options = False
            in_explanation = False
            continue
        
        # Check for explanation
        if re.match(r'^ex[:\-]', line, re.IGNORECASE) or \
           re.match(r'^explanation[:\-]', line, re.IGNORECASE) or \
           re.match(r'^solution[:\-]', line, re.IGNORECASE):
            in_explanation = True
            exp_text = re.sub(r'^ex[:\-]\s*', '', line, flags=re.IGNORECASE)
            exp_text = re.sub(r'^explanation[:\-]\s*', '', exp_text, flags=re.IGNORECASE)
            exp_text = re.sub(r'^solution[:\-]\s*', '', exp_text, flags=re.IGNORECASE)
            if exp_text:
                current_explanation.append(exp_text)
            continue
        
        # Add text based on current section
        if in_explanation:
            current_explanation.append(line)
        elif in_options and current_options:
            # Append to last option (for multi-line options)
            current_options[-1] += "<br>" + line
        elif in_question and current_question:
            # Append to question (for multi-line questions)
            current_question += "<br>" + line
    
    # Add last question if exists
    if current_question and current_options:
        question_data = {
            "question": current_question,
            "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
            "answer": "",
            "solution_text": "<br>".join(current_explanation) if current_explanation else ""
        }
        
        # Add options
        for i, option in enumerate(current_options[:5]):
            question_data[f"option_{i+1}"] = option
        
        questions.append(question_data)
    
    # If no questions found with the above method, try alternative parsing
    if not questions:
        # Try parsing with different pattern
        blocks = re.split(r'\n\s*\n', content.strip())
        
        for block in blocks:
            lines = [line.strip() for line in block.strip().split('\n') if line.strip()]
            if len(lines) < 3:  # Minimum lines for a question
                continue
                
            # Look for question and options in block
            question_lines = []
            options = []
            answer = ""
            explanation_lines = []
            
            for line in lines:
                # Check if it's an option
                option_match = re.match(r'^([a-e])[\.\)\-\:]\s*(.+)$', line)
                if option_match:
                    options.append(f"{option_match.group(1)}) {option_match.group(2)}")
                # Check if it's an answer
                elif re.match(r'^(Correct|Answer|Ans)[:\-]', line, re.IGNORECASE):
                    ans_match = re.search(r'([a-e])', line, re.IGNORECASE)
                    if ans_match:
                        answer = ans_match.group(1).lower()
                # Check if it's explanation
                elif re.match(r'^ex[:\-]', line, re.IGNORECASE):
                    exp_text = re.sub(r'^ex[:\-]\s*', '', line, flags=re.IGNORECASE)
                    explanation_lines.append(exp_text)
                else:
                    # Assume it's part of question
                    question_lines.append(line)
            
            if question_lines and options:
                question_data = {
                    "question": "<br>".join(question_lines),
                    "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
                    "answer": answer,
                    "solution_text": "<br>".join(explanation_lines) if explanation_lines else ""
                }
                
                # Add options
                for i, option in enumerate(options[:5]):
                    question_data[f"option_{i+1}"] = option
                
                questions.append(question_data)
    
    # Add metadata to each question
    for i, q in enumerate(questions):
        q["id"] = str(50000 + i + 1)
        q["correct_score"] = "1"
        q["negative_score"] = "0"
        q["deleted"] = "0"
        q["difficulty_level"] = "0"
        q["option_image_1"] = q["option_image_2"] = q["option_image_3"] = ""
        q["option_image_4"] = q["option_image_5"] = ""
        q["question_image"] = ""
        q["solution_heading"] = ""
        q["solution_image"] = ""
        q["solution_video"] = ""
        q["sortingparam"] = "0.00"
        
        # Ensure answer is in correct format (1-5)
        if q["answer"]:
            answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
            q["answer"] = answer_map.get(q["answer"].lower(), '1')
    
    return questions

def generate_html_quiz(quiz_data):
    """Generate HTML quiz from the parsed data"""
  # Read template HTML
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{quiz_name}</title>
<style>
:root{{
  --accent:#2ec4b6;
  --accent-dark:#1da89a;
  --muted:#69707a;
  --success:#1f9e5a;
  --danger:#c82d3f;
  --warning:#f39c12;
  --info:#3498db;
  --bg:#f5f7fa;
  --card:#fff;
  --maxw:820px;
  --radius:10px;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:#111;padding-bottom:96px}}
.container{{max-width:var(--maxw);margin:auto;padding:10px 16px}}
header{{background:#fff;box-shadow:0 2px 6px rgba(0,0,0,0.08);position:relative;z-index:10}}
.header-inner{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;gap:12px}}
h1{{margin:0;color:var(--accent);font-size:18px}}
.btn{{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer;transition:background .18s}}
.btn:hover{{background:var(--accent-dark)}}
.btn-ghost{{background:#fff;color:var(--accent);border:2px solid var(--accent);padding:8px 12px;border-radius:999px;font-weight:700;cursor:pointer}}
.btn-warning{{background:var(--warning);color:#fff}}
.btn-info{{background:var(--info);color:#fff}}
.btn-success{{background:var(--success);color:#fff}}
.btn-danger{{background:var(--danger);color:#fff}}
.timer-text{{color:var(--accent-dark);font-weight:700;font-size:18px;min-width:72px;text-align:right}}
.toggle-pill{{position:relative;width:90px;height:30px;background:#eaeef0;border-radius:999px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:0 6px;font-size:13px;color:#444;font-weight:600}}
.toggle-pill span{{z-index:2;flex:1;text-align:center}}
.toggle-pill::before{{content:"";position:absolute;top:3px;left:3px;width:42px;height:24px;background:var(--accent);border-radius:999px;transition:.28s}}
.toggle-pill.active::before{{transform:translateX(45px);background:var(--accent-dark)}}
.toggle-pill.active span:last-child{{color:#fff}}
.toggle-pill span:first-child{{color:#fff}}

/* quiz card */
.card{{background:var(--card);border-radius:10px;padding:10px 12px;margin:12px 0;box-shadow:0 4px 10px rgba(0,0,0,0.05)}}
.qbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.qmeta{{font-size:13px;color:var(--muted)}}
.marking{{font-size:13px;color:var(--muted)}}
.qtext{{font-size:16px;margin:6px 0;font-weight:500}}
.opt{{padding:10px;border:1px solid #e6eaec;border-radius:8px;background:#fff;cursor:pointer;display:flex;align-items:center;gap:10px;transition:all .12s;font-weight:500}}
.opt{{padding:10px;border:1px solid #e6eaec;border-radius:8px;background:#fff;cursor:pointer;display:flex;align-items:center;gap:10px;transition:all .12s}}
.opt:hover{{border-color:#cfd8da}}
.opt.selected{{border-color:var(--accent)}}
.opt.correct{{border-color:var(--success);background:rgba(31,158,90,0.12)}}
.opt.wrong{{border-color:var(--danger);background:rgba(200,45,63,0.12)}}
.custom-radio{{display:none;height:16px;width:16px;border-radius:50%;border:2px solid #ccc}}
.opt.selected .custom-radio{{display:block;border:6px solid var(--accent)}}
.opt.correct .custom-radio{{display:block;border:6px solid var(--success)}}
.opt.wrong .custom-radio{{display:block;border:6px solid var(--danger)}}
.explanation{{margin-top:8px;padding:10px;border-radius:8px;background:#fbfdfe;border:1px solid #edf2f3;display:none;font-size:14px}}

/* bottom nav */
.fbar{{position:fixed;left:0;right:0;bottom:0;background:#fff;box-shadow:0 -3px 12px rgba(0,0,0,0.08);display:flex;justify-content:center;z-index:50}}
.fbar-inner{{display:flex;justify-content:center;align-items:center;gap:10px;max-width:var(--maxw);width:100%;padding:10px}}
.fbar button{{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer;min-width:80px}}
.fbar button:hover{{background:var(--accent-dark)}}
.fbar .btn-warning{{background:var(--warning)}}
.fbar .btn-info{{background:var(--info)}}

/* palette popup */
#palette{{position:fixed;top:64px;right:14px;background:#fff;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.12);padding:12px;display:none;gap:8px;flex-wrap:wrap;z-index:200;max-width:300px;max-height:70vh;overflow-y:auto;overscroll-behavior:contain}}
#palette .qbtn{{width:44px;height:44px;border-radius:8px;border:1px solid #e3eaeb;background:#fbfdff;cursor:pointer;font-weight:700}}
#palette .qbtn.attempted{{background:var(--success);color:#fff;border:none}}
#palette .qbtn.unattempted{{background:var(--danger);color:#fff;border:none}}
#palette .qbtn.marked{{background:var(--warning);color:#fff;border:none}}
#palette .qbtn.current{{font-weight:bold;border:3px solid var(--accent);transform:scale(1.1)}}
#palette-summary{{margin-top:8px;font-size:13px;color:var(--muted);text-align:center}}

/* modal */
.modal{{position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:center;justify-content:center;z-index:300}}
.modal-content{{background:#fff;border-radius:12px;padding:18px;max-width:420px;width:92%;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.18)}}
.modal h3{{margin:0 0 8px;color:var(--accent)}}
.modal p{{color:#333;margin:8px 0 12px;font-size:15px}}
.modal .actions{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),var(--accent-dark));color:#fff;border:none;padding:8px 14px;border-radius:999px;font-weight:700;cursor:pointer}}

/* results */
.results{{margin-top:12px}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.stat{{flex:1 1 120px;padding:10px;border-radius:10px;text-align:center;background:#f7fbfb;border:1px solid #e6eeed}}
.stat h4{{margin:0;color:var(--accent);font-size:13px}}
.stat p{{margin:6px 0 0;font-weight:700;font-size:18px}}

/* download buttons */
.download-buttons{{display:flex;gap:10px;margin:20px 0;flex-wrap:wrap}}
.download-buttons button{{flex:1;min-width:150px}}

/* 🔐 COPY & SELECTION BLOCK */
body {{
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
}}

/* Allow inputs only */
input, textarea {{
  user-select: text !important;
}}

/* 🔐 WATERMARK BASE */
.ssc-watermark {{
  position: fixed;
  inset: 0;
  z-index: 3;
  pointer-events: none;
  overflow: hidden;
}}

.ssc-wm {{
  position: absolute;
  font-size: 26px;
  font-weight: 700;
  color: rgba(0,0,0,0.045);
  transform: rotate(-30deg);
  white-space: nowrap;
}}
/* 🔢 MathJax mobile safety */
mjx-container {{
  max-width: 100%;
  overflow-x: auto;
}}

/* 🔢 Make MathJax match quiz text font */
mjx-container {{
  font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif !important;
  font-size: 1em;
}}

/* Responsive adjustments */
@media (max-width: 768px) {{
  .container{{padding:8px}}
  .header-inner{{padding:6px;flex-wrap:wrap}}
  .fbar-inner{{gap:6px;padding:8px}}
  .fbar button{{padding:8px 10px;font-size:12px;min-width:70px}}
  .stat{{flex:1 1 100px;padding:8px}}
  .stat p{{font-size:16px}}
  #palette{{max-width:250px}}
  #palette .qbtn{{width:38px;height:38px}}
}}

@media (max-width: 480px) {{
  .header-inner{{flex-direction:column;gap:8px}}
  .fbar-inner{{flex-wrap:wrap}}
  .fbar button{{flex:1 1 calc(50% - 10px);max-width:none}}
  .stat{{flex:1 1 100%}}
  .download-buttons button{{min-width:100%}}
}}

/* Tablet optimizations */
@media (min-width: 769px) and (max-width: 1024px) {{
  .container{{max-width:90%}}
  #palette{{max-width:280px}}
}}

</style>
  <!-- 🔢 MathJax Auto-LaTeX (Google Docs like) -->
<script>
  window.MathJax = {{
    tex: {{
      inlineMath: [['\\\\(', '\\\\)'], ['$', '$']],
      displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']]
    }},
    options: {{
      skipHtmlTags: ['script', 'style', 'textarea', 'pre']
    }}
  }};
</script>

<script
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"
  async>
</script>

<!-- PDF Generation Libraries -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>

</head>
<body>
<!-- 🔐 WATERMARK LAYER -->
<div id="ssc-watermark" class="ssc-watermark">
  <div class="ssc-wm" style="top:10%; left:5%;">Quiz Generated</div>
  <div class="ssc-wm" style="top:30%; left:60%;">Quiz Generated</div>
  <div class="ssc-wm" style="top:55%; left:25%;">Quiz Generated</div>
  <div class="ssc-wm" style="top:75%; left:65%;">Quiz Generated</div>
  <div class="ssc-wm" style="top:90%; left:10%;">Quiz Generated</div>
</div>

<!-- 🔥 FIREBASE SDK -->
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-database-compat.js"></script>

<script>
  const firebaseConfig = {{
    apiKey: "AIzaSyBWF7Ojso-w0BucbqJylGR7h9eGeDQodzE",
    authDomain: "ssc-quiz-rank-percentile.firebaseapp.com",
    databaseURL: "https://ssc-quiz-rank-percentile-default-rtdb.firebaseio.com",
    projectId: "ssc-quiz-rank-percentile",
    storageBucket: "ssc-quiz-rank-percentile.firebasestorage.app",
    messagingSenderId: "944635517164",
    appId: "1:944635517164:web:62f0cc83892917f225edc9"
  }};

  // 🔥 Initialize Firebase
  firebase.initializeApp(firebaseConfig);

  // 🔥 Realtime Database reference
  const db = firebase.database();
</script>

<header id="mainHeader">
  <div class="header-inner" id="headerControls">
    <div style="display:flex;align-items:center;gap:12px">
      <div class="toggle-pill" id="modeToggle"><span>Test</span><span>Quiz</span></div>
    </div>

    <div style="display:flex;align-items:center;gap:10px">
      <div class="timer-text" id="timer">00:00</div>
      <button id="submitBtn" class="btn">Submit</button>
      <button id="paletteBtn" class="btn-ghost">View</button>
    </div>
  </div>
</header>

<div class="container">
  <div id="quizCard" class="card">
    <div class="qbar">
      <div class="qmeta">Question <span id="qindex">0</span> / <span id="qtotal">0</span></div>
      <div class="marking" id="marking"></div>
    </div>

    <div class="qtext" id="qtext"></div>
    <div class="options" id="options"></div>
    <div id="explanation" class="explanation"></div>
  </div>

  <div id="results" class="results" style="display:none"></div>
  <div id="previousAttempts" class="results" style="display:none"></div>
  <div id="attemptReplay" class="results" style="display:none"></div>

</div>

<div class="fbar" id="floatBar">
  <div class="fbar-inner">
    <button id="prevBtn">← Prev</button>
    <button id="markReviewBtn" class="btn-warning">Mark for Review</button>
    <button id="clearBtn">Clear</button>
    <button id="saveNextBtn" class="btn-success">Save & Next →</button>
  </div>
</div>

<div id="palette" aria-hidden="true"></div>

<div id="submitModal" class="modal" role="dialog" aria-modal="true">
  <div class="modal-content">
    <h3>Submit quiz?</h3>
    <p id="submitMsg">You attempted X/Y. Are you sure you want to submit?</p>
    <div class="actions">
      <button id="cancelSubmit" class="btn-ghost">Cancel</button>
      <button id="confirmSubmit" class="btn-primary">Submit</button>
    </div>
  </div>
</div>

<script>
function renderMath(){{
  if (window.MathJax && MathJax.typesetPromise) {{
    MathJax.typesetPromise();
  }}
}}


function normalizeMathForQuiz(container) {{
  if (!container) return;

  // Replace $$...$$ inside text with inline math \( ... \)
  container.innerHTML = container.innerHTML
    // case 1: $$...$$ surrounded by text → inline
    .replace(/(\\S)\\s*\\$\\$(.+?)\\$\\*\\s*(\\S)/g, '$1 \\\\($2\\\\) $3')
    // case 2: $$...$$ with spaces but not line-isolated → inline
    .replace(/\\$\\$(.+?)\\$\\$/g, function(match, math) {{
      // if already block (surrounded by <br> or alone), keep it
      if (/^<br>|<div>|<\/div>|<p>|<\/p>/.test(match)) {{
        return match;
      }}
      return '\\\\(' + math + '\\\\)';
    }});
}}


function _hx(s){{
  let h = 0;
  for(let i = 0; i < s.length; i++){{
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0;
  }}
  return h;
}}

let __QUIZ_OK = true;

// 🔐 SAFE Device ID (works everywhere)
let DEVICE_ID = localStorage.getItem("ssc_quiz_device_id");
if (!DEVICE_ID) {{
  DEVICE_ID = "dev-" + Date.now() + "-" + Math.random().toString(36).substring(2, 12);
  localStorage.setItem("ssc_quiz_device_id", DEVICE_ID);
}}

const QUIZ_TITLE = "{quiz_name}";
const QUIZ_STATE_KEY = "ssc_quiz_state_{quiz_name}";
const QUIZ_RESULT_KEY = "ssc_quiz_result_{quiz_name}";



// 🔥 FIREBASE: SAVE ONLY FIRST ATTEMPT (ADMIN DATA)
function saveResultFirebase(payload) {{
  const ref = db.ref(
    "quiz_results/" + payload.quizId + "/" + payload.deviceId
  );

  return ref.once("value").then(snap => {{
    if (snap.exists()) {{
      // ❌ already attempted → DO NOT overwrite admin data
      return false;
    }}

    // ✅ first attempt only
    return ref.set(payload);
  }});
}}
function saveAttemptHistory(payload) {{
  const ref = db.ref(
    "attempt_history/" +
    payload.quizId + "/" +
    payload.deviceId + "/" +
    payload.submittedAt
  );
  return ref.set(payload);
}}

// 🔥 FIREBASE: RANK + PERCENTILE (SCORE ↓, TIME ↑)
function getRankAndPercentile(quizId, myScore, myTime, mySubmittedAt) {{
  return db
    .ref("attempt_history/" + quizId)
    .once("value")
    .then(snapshot => {{
      const quizData = snapshot.val() || {{}};
      let attempts = [];

      // collect ALL attempts from ALL devices
      Object.values(quizData).forEach(deviceAttempts => {{
        Object.values(deviceAttempts).forEach(attempt => {{
          attempts.push(attempt);
        }});
      }});

      // sort: score desc, time asc
      attempts.sort((a, b) => {{
        if (b.score !== a.score) return b.score - a.score;
        return a.timeTaken - b.timeTaken;
      }});

      const total = attempts.length;

      let rank = total + 1;
      for (let i = 0; i < attempts.length; i++) {{
        if (
          attempts[i].score === myScore &&
          attempts[i].timeTaken === myTime &&
          attempts[i].submittedAt === mySubmittedAt
        ) {{

          rank = i + 1;
          break;
        }}
      }}

      const below = attempts.filter(
        a =>
          a.score < myScore ||
          (a.score === myScore && a.timeTaken > myTime)
      ).length;

      const percentile = total
        ? ((below / total) * 100).toFixed(2)
        : "0.00";

      return {{ rank, percentile, total }};
    }});
}}

// 🔥 LIVE rank recalculation for PREVIOUS attempts
function getLiveRankForAttempt(quizId, attempt) {{
  return db.ref("attempt_history/" + quizId)
    .once("value")
    .then(snapshot => {{
      const quizData = snapshot.val() || {{}};
      let attempts = [];

      Object.values(quizData).forEach(deviceAttempts => {{
        Object.values(deviceAttempts).forEach(a => {{
          attempts.push(a);
        }});
      }});

      // SAME SORT LOGIC (score ↓, time ↑)
      attempts.sort((a, b) => {{
        if (b.score !== a.score) return b.score - a.score;
        return a.timeTaken - b.timeTaken;
      }});

      const total = attempts.length;

      let rank = total;
      for (let i = 0; i < attempts.length; i++) {{
        if (attempts[i].submittedAt === attempt.submittedAt) {{
          rank = i + 1;
          break;
        }}
      }}

      const below = attempts.filter(
        a =>
          a.score < attempt.score ||
          (a.score === attempt.score && a.timeTaken > attempt.timeTaken)
      ).length;

      const percentile = total
        ? ((below / total) * 100).toFixed(2)
        : "0.00";

      return {{ rank, percentile, total }};
    }});
}}

/* data from Jinja */
const QUESTIONS = {questions_array};





let current = 0;
let answers = {{}};            // {{ questionId: "1", ... }}
let markedForReview = {{}};    // {{ questionId: true, ... }}
let seenQuestions = {{}};      // {{ questionId: true, ... }}
let ALL_ATTEMPTS_CACHE = {{}};
let LAST_RESULT_HTML = "";
let seconds = {seconds}; //  countdown
const TOTAL_TIME_SECONDS = seconds;
let isQuiz = false;
let timerInterval = null;

function saveQuizState(){{
  const state = {{
    current,
    answers,
    markedForReview,
    seenQuestions,
    seconds
  }};
  localStorage.setItem(QUIZ_STATE_KEY, JSON.stringify(state));
}}

const el = id => document.getElementById(id);

function rebindResultHeaderActions() {{
  const header = document.getElementById("headerControls");
  if (!header) return;

  header.querySelectorAll("button").forEach(btn => {{
    const txt = btn.textContent.trim();

    if (txt === "Re-Attempt") {{
      btn.onclick = () => {{
        localStorage.removeItem(QUIZ_RESULT_KEY);
        localStorage.removeItem(QUIZ_STATE_KEY);
        location.reload();
      }};
    }}

    if (txt === "Previous Attempts") {{
      btn.onclick = () => {{
        const res = document.getElementById("results");
        res.innerHTML = "";
        res.style.display = "block";
        loadPreviousAttempts("{quiz_name}", DEVICE_ID);
      }};
    }}

    if (txt === "Back to Latest Result") {{
      btn.onclick = () => {{
        document.getElementById("results").innerHTML = LAST_RESULT_HTML;
        document.getElementById("results").style.display = "block";
      }};
    }}
  }});
}}


/* format mm:ss */
function fmt(s){{
  const m = Math.floor(s/60);
  const sec = s%60;
  return `${{String(m).padStart(2,"0")}}:${{String(sec).padStart(2,"0")}}`;
}}

/* Timer */
function startTimer(){{
  if(timerInterval) clearInterval(timerInterval);

  // show initial time immediately
  el("timer").textContent = fmt(seconds);
  saveQuizState();

  timerInterval = setInterval(()=>{{
    if(seconds <= 0){{
      clearInterval(timerInterval);
      el("timer").textContent = "00:00";

      // auto submit when time is over (optional)
      try {{
        document.getElementById("submitBtn")?.click();
      }} catch(e){{}}

      return;
    }}

    seconds--;
    el("timer").textContent = fmt(seconds);
  }}, 1000);
}}

/* initialize */
function init(){{
  if (!__QUIZ_OK) return;

  el("qtotal").textContent = QUESTIONS.length;

  // 🔥 CHECK IF QUIZ ALREADY SUBMITTED
  const resultSaved = localStorage.getItem(QUIZ_RESULT_KEY);
  if (resultSaved) {{
    const data = JSON.parse(resultSaved);

    if (data.submitted && data.resultHTML) {{

      // hide quiz UI
      el("quizCard").style.display = "none";
      el("floatBar").style.display = "none";

      // restore results
      const res = document.getElementById("results");
      res.innerHTML = data.resultHTML;
      res.style.display = "block";
      renderMath();

      // 🔥 RESTORE HEADER BUTTONS
      if (data.headerHTML) {{
        document.getElementById("headerControls").innerHTML = data.headerHTML;
        rebindResultHeaderActions(); // ✅ REQUIRED FIX
      }}

      return; // ❌ STOP QUIZ INIT
    }}
  }}


  // 🔁 NORMAL QUIZ RESUME FLOW
  const saved = 
  document.createElement("div");
  summary.id = "palette-summary";
  summary.style.marginTop = "8px";
  pal.appendChild(summary);
  highlightPalette();
}}

/* color palette buttons and show summary */
function highlightPalette(){{
  const pal = el("palette");
  if(!pal) return;
  const total = QUESTIONS.length;
  const attempted = Object.keys(answers).length;
  const marked = Object.keys(markedForReview).length;
  const notAttempted = total - attempted;
  Array.from(pal.children).forEach((child, idx) => {{
    // skip summary
    if(child.id === "palette-summary") return;
    child.classList.remove("attempted", "unattempted", "marked", "current", "unseen");
    
    const qid = QUESTIONS[idx].id ?? idx;
    
    if(idx === current) {{
      child.classList.add("current");
    }}
    
    if(answers[qid]) {{
      child.classList.add("attempted");
    }} else if(markedForReview[qid]) {{
      child.classList.add("marked");
    }} else if(seenQuestions[idx]) {{
      child.classList.add("unattempted");
    }} else {{
      child.classList.add("unseen");
    }}
  }});
  const summary = el("palette-summary");
  if(summary) summary.textContent = `Total: ${{total}} | Attempted: ${{attempted}} | Marked: ${{marked}} | Unseen: ${{total - Object.keys(seenQuestions).length}}`;
}}

/* toggle palette visibility */
function togglePalette(){{
  const pal = el("palette");
  if(pal.style.display === "flex") pal.style.display = "none";
  else {{ pal.style.display = "flex"; highlightPalette(); }}
}}

// 🔐 COPY + WATERMARK PROTECTION (DEPENDENCY TRAP)
(function () {{

  // Disable right click
  document.addEventListener("contextmenu", e => e.preventDefault());

  // Disable common copy shortcuts
  document.addEventListener("keydown", function (e) {{
    if (
      (e.ctrlKey || e.metaKey) &&
      ["c","x","v","a","s","p","u"].includes(e.key.toLowerCase())
    ) {{
      e.preventDefault();
    }}
  }});

  // Ensure watermark always exists
  function ensureWatermark(){{
    if(!document.getElementById("ssc-watermark")){{
      document.body.innerHTML = "";
      alert("Quiz file modified");
      throw new Error("WATERMARK_REMOVED");
    }}
  }}

  ensureWatermark();
  setInterval(ensureWatermark, 2000);

}})();



function filterResults(type){{
  const cards = document.querySelectorAll(".result-q");
  cards.forEach(card=>{{
    if(type === "all"){{
      card.style.display = "block";
    }} else {{
      card.style.display = card.dataset.status === type ? "block" : "none";
    }}
  }});
}}

/* start */
window.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>"""
    
    # Generate QUESTIONS array
    questions_js = "[\n"
    for i, q in enumerate(quiz_data["questions"]):
        q["id"] = str(50000 + i + 1)
        q["quiz_id"] = quiz_data["name"]
        
        # Update scores with actual values from quiz data
        q["correct_score"] = str(quiz_data.get("marks", "3"))
        q["negative_score"] = str(quiz_data.get("negative", "1"))
        
        # Convert to JSON-like string
        q_str = json.dumps(q, ensure_ascii=False)
        questions_js += q_str + ",\n"
    questions_js = questions_js.rstrip(",\n") + "\n]"
    
    # Calculate seconds
    seconds = int(quiz_data.get("time", "25")) * 60
    
    html = template.format(
        quiz_name=quiz_data["name"],
        questions_array=questions_js,
        seconds=seconds
    )
    
    return html

# Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    update_activity()
    await update.message.reply_text(
        "📚 *Quiz Generator Bot*\n\n"
        "Send me a TXT file with questions in any format:\n\n"
        "**Supported Formats:**\n"
        "1. Q1. Question text\n"
        "   a) Option 1\n"
        "   b) Option 2\n"
        "   Answer: (a)\n\n"
        "2. 1. Question text\n"
        "   a) Option 1\n"
        "   b) Option 2\n"
        "   Correct option:-a\n\n"
        "3. Any format with questions and options!\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/help - Show help\n"
        "/wake - Keep the bot awake\n"
        "/status - Check bot status\n"
        "/cancel - Cancel current operation",
        parse_mode="Markdown"
    )
    return GETTING_FILE

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle TXT file upload"""
    update_activity()
    
    try:
        document = update.message.document
        user_id = update.effective_user.id
        
        # Check if it's a text file
        if not document.mime_type == 'text/plain' and not document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Please send a text file (.txt)")
            return GETTING_FILE
        
        # Download the file
        await update.message.reply_text("📥 Processing your quiz file...")
        
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        content = file_content.decode('utf-8')
        
        # Parse questions
        questions = parse_txt_file(content)
        
        if not questions:
            await update.message.reply_text("❌ Could not parse any questions from the file. Please check the format.")
            return GETTING_FILE
        
        # Store in context
        context.user_data["questions"] = questions
        context.user_data["file_name"] = document.file_name
        
        await update.message.reply_text(f"✅ Parsed {len(questions)} questions successfully!\n\nNow enter the quiz name:")
        return GETTING_QUIZ_NAME
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text(f"❌ Error reading file: {str(e)}")
        return GETTING_FILE

async def get_quiz_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get quiz name"""
    update_activity()
    context.user_data["name"] = update.message.text
    
    keyboard = [
        [InlineKeyboardButton("15 min", callback_data="15"),
         InlineKeyboardButton("20 min", callback_data="20"),
         InlineKeyboardButton("25 min", callback_data="25"),
         InlineKeyboardButton("30 min", callback_data="30")],
        [InlineKeyboardButton("Custom", callback_data="custom")]
    ]
    
    await update.message.reply_text(
        "⏱️ Select quiz time (or choose Custom to enter minutes):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_TIME

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle time selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom":
        await query.edit_message_text("Enter time in minutes:")
        return GETTING_TIME
    
    context.user_data["time"] = query.data
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"),        negative = float(update.message.text)
        if negative < 0:
            raise ValueError
        context.user_data["negative"] = str(negative)
    except:
        await update.message.reply_text("Please enter a valid number for negative marking:")
        return GETTING_NEGATIVE
    
    await update.message.reply_text("🏆 Enter creator name:")
    return GETTING_CREATOR

async def get_creator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get creator name and generate quiz"""
    update_activity()
    context.user_data["creator"] = update.message.text
    
    # Show summary with proper format
    total_questions = len(context.user_data["questions"])
    summary = (
        "📋 *Quiz Summary*\n\n"
        f"📘 QUIZ ID: {context.user_data['name']}\n"
        f"📊 TOTAL QUESTIONS: {total_questions}\n"
        f"⏱️ TIME: {context.user_data['time']} Minutes\n"
        f"✍️ EACH QUESTION MARK: {context.user_data['marks']}\n"
        f"⚠️ NEGATIVE MARKING: {context.user_data['negative']}\n"
        f"🏆 CREATED BY: {context.user_data['creator']}\n\n"
        "🔄 Generating quiz HTML..."
    )
    
    progress_msg = await update.message.reply_text(summary, parse_mode="Markdown")
    
    # Generate progress bar
    user_id = update.effective_user.id
    user_progress[user_id] = progress_msg.message_id
    
    # Generate HTML
    try:
        # Update progress
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=progress_msg.message_id,
            text=f"{summary}\n\n🔄 Processing {total_questions} questions..."
        )
        ~filters.COMMAND, get_creator),
                CommandHandler("cancel", cancel)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("wake", wake_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_error_handler(error_handler)
    
    logger.info("Quiz Generator Bot is starting...")
    
    # Start the bot
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        time.sleep(10)
        logger.info("Retrying to start bot...")
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            close_loop=False
        )

if __name__ == '__main__':
    main()
