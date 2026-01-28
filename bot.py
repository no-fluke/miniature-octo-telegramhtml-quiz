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
    
    # Normalize line endings and remove extra spaces
    content = content.replace('\r\n', '\n').strip()
    
    # Try different parsing strategies
    # Strategy 1: Questions separated by double newlines
    blocks = re.split(r'\n\s*\n', content)
    
    for block in blocks:
        block = block.strip()
        if not block:
            continue
            
        question = {
            "question": "",
            "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
            "answer": "",
            "solution_text": ""
        }
        
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        
        if len(lines) < 2:  # Need at least question and one option
            continue
        
        current_line = 0
        question_lines = []
        
        # Extract question (until we hit something that looks like an option)
        while current_line < len(lines):
            line = lines[current_line]
            # Check if this line starts an option
            if (re.match(r'^[a-e]\)', line, re.IGNORECASE) or 
                re.match(r'^\([a-e]\)', line, re.IGNORECASE) or
                re.match(r'^[a-e]\.', line, re.IGNORECASE) or
                re.match(r'^\d+\.', line)):
                break
            question_lines.append(line)
            current_line += 1
        
        question["question"] = '<br>'.join(question_lines)
        
        # Extract options
        option_count = 0
        while current_line < len(lines) and option_count < 5:
            line = lines[current_line]
            
            # Check for option patterns
            option_match = re.match(r'^([a-e])[\)\.]\s*(.*)', line, re.IGNORECASE)
            if not option_match:
                option_match = re.match(r'^\(([a-e])\)\s*(.*)', line, re.IGNORECASE)
            
            if option_match:
                option_key = f"option_{option_count + 1}"
                option_letter = option_match.group(1).lower()
                option_text = option_match.group(2)
                
                # Check for multi-line options
                next_line_idx = current_line + 1
                while (next_line_idx < len(lines) and 
                       not re.match(r'^[a-e]\)', lines[next_line_idx], re.IGNORECASE) and
                       not re.match(r'^\([a-e]\)', lines[next_line_idx], re.IGNORECASE) and
                       not re.match(r'^[a-e]\.', lines[next_line_idx], re.IGNORECASE) and
                       not re.match(r'^Correct', lines[next_line_idx], re.IGNORECASE) and
                       not re.match(r'^Answer:', lines[next_line_idx], re.IGNORECASE) and
                       not re.match(r'^ex:', lines[next_line_idx], re.IGNORECASE)):
                    option_text += f"<br>{lines[next_line_idx]}"
                    next_line_idx += 1
                
                question[option_key] = option_text
                option_count += 1
                current_line = next_line_idx
            else:
                current_line += 1
        
        # Extract correct answer
        for i in range(current_line, len(lines)):
            line = lines[i]
            if re.match(r'^Correct\s*(option)?\s*[:-]\s*([a-e])', line, re.IGNORECASE):
                match = re.match(r'^Correct\s*(option)?\s*[:-]\s*([a-e])', line, re.IGNORECASE)
                if match:
                    ans = match.group(2).lower()
                    answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
                    question["answer"] = answer_map.get(ans, '1')
            elif re.match(r'^Answer\s*[:-]\s*([a-e])', line, re.IGNORECASE):
                match = re.match(r'^Answer\s*[:-]\s*([a-e])', line, re.IGNORECASE)
                if match:
                    ans = match.group(1).lower()
                    answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
                    question["answer"] = answer_map.get(ans, '1')
        
        # Extract explanation
        solution_lines = []
        for i in range(current_line, len(lines)):
            line = lines[i]
            if re.match(r'^ex:', line, re.IGNORECASE):
                solution_lines.append(line[3:].strip())
            elif re.match(r'^Explanation\s*[:-]', line, re.IGNORECASE):
                solution_lines.append(line.split(':', 1)[1].strip())
        
        question["solution_text"] = '<br>'.join(solution_lines)
        
        # Add metadata
        question["correct_score"] = "3"
        question["negative_score"] = "1"
        question["deleted"] = "0"
        question["difficulty_level"] = "0"
        question["option_image_1"] = question["option_image_2"] = question["option_image_3"] = ""
        question["option_image_4"] = question["option_image_5"] = ""
        question["question_image"] = ""
        question["solution_heading"] = ""
        question["solution_image"] = ""
        question["solution_video"] = ""
        question["sortingparam"] = "0.00"
        
        # Only add if we have at least 2 options and a question
        if question["question"] and option_count >= 2:
            questions.append(question)
    
    return questions

def generate_html_quiz(quiz_data):
    """Generate HTML quiz from the parsed data"""
    
    # Read template HTML with all new features
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
  --warning:#f5a623;
  --info:#5d6afb;
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
.btn-warning{{background:var(--warning);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer}}
.btn-success{{background:var(--success);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer}}
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
.fbar button{{padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer;border:none;border-radius:6px}}

/* palette popup */
#palette{{position:fixed;top:64px;right:14px;background:#fff;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.12);padding:12px;display:none;gap:8px;flex-wrap:wrap;z-index:200;max-width:300px;max-height:70vh;overflow-y:auto;overscroll-behavior:contain}}
#palette .qbtn{{width:44px;height:44px;border-radius:8px;border:1px solid #e3eaeb;background:#fbfdff;cursor:pointer;font-weight:700}}
#palette .qbtn.answered{{background:var(--success);color:#fff;border:none}}
#palette .qbtn.unattempted{{background:var(--danger);color:#fff;border:none}}
#palette .qbtn.marked{{background:#9370db;color:#fff;border:none}}
#palette .qbtn.current{{border:3px solid var(--accent);font-weight:800}}
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

@media(max-width:768px){{
  .header-inner{{flex-direction:column;gap:10px;padding:10px}}
  .timer-text{{font-size:16px}}
  .fbar-inner{{gap:6px;padding:8px}}
  .fbar button{{padding:8px 10px;font-size:12px}}
  .fbar-inner{{flex-wrap:wrap}}
  #palette{{top:120px;right:10px;max-width:280px}}
}}
@media(max-width:480px){{
  .container{{padding:8px}}
  .header-inner{{padding:8px}}
  .fbar-inner{{gap:4px;padding:6px}}
  .fbar button{{padding:6px 8px;font-size:11px}}
  .btn,.btn-ghost,.btn-warning{{padding:6px 10px;font-size:12px}}
}}
@media(min-width:1024px){{
  .container{{max-width:900px;padding:20px}}
}}
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

/* PDF Download Button */
.pdf-download-btn {{
  background: linear-gradient(135deg, #ff6b6b, #ee5a24);
  color: white;
  border: none;
  padding: 10px 20px;
  border-radius: 6px;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 10px 0;
}}
.pdf-download-btn:hover {{
  background: linear-gradient(135deg, #ee5a24, #d64500);
}}

/* Question status indicators */
.status-indicator {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-left: 10px;
}}
.status-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
}}
.status-answered {{
  background: var(--success);
}}
.status-unattempted {{
  background: var(--danger);
}}
.status-marked {{
  background: #9370db;
}}
.status-unseen {{
  background: #ccc;
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

<!-- jsPDF for PDF generation -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.5.28/jspdf.plugin.autotable.min.js"></script>

</head>
<body>
<!-- 🔐 WATERMARK LAYER -->
<div id="ssc-watermark" class="ssc-watermark">
  <div class="ssc-wm" style="top:10%; left:5%;">@SSC_JOURNEY2</div>
  <div class="ssc-wm" style="top:30%; left:60%;">@SSC_JOURNEY2</div>
  <div class="ssc-wm" style="top:55%; left:25%;">@SSC_JOURNEY2</div>
  <div class="ssc-wm" style="top:75%; left:65%;">@SSC_JOURNEY2</div>
  <div class="ssc-wm" style="top:90%; left:10%;">@SSC_JOURNEY2</div>
</div>

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
    <button id="prevBtn" class="btn-ghost">← Prev</button>
    <button id="clearBtn" class="btn-ghost">Clear</button>
    <button id="markBtn" class="btn-warning">Mark for Review</button>
    <button id="saveNextBtn" class="btn-success">Save & Next</button>
    <button id="nextBtn" class="btn-ghost">Next →</button>
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


/* data from Jinja */
const QUESTIONS = {questions_array};





let current = 0;
let answers = {{}};            // {{ questionId: "1", ... }}
let marked = new Set();        // Set of marked question indices
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
    seconds,
    marked: Array.from(marked)
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
  const saved = localStorage.getItem(QUIZ_STATE_KEY);
  if (saved) {{
    const state = JSON.parse(saved);
    current = state.current ?? 0;
    answers = state.answers ?? {{}};
    seconds = state.seconds ?? seconds;
    marked = new Set(state.marked || []);
  }}

  renderQuestion(current);
  startTimer();
  attachListeners();
  buildPalette();
  highlightPalette();
  renderMath();
}}




/* render question */
function renderQuestion(i){{
  current = i;
  const q = QUESTIONS[i];
  el("qindex").textContent = i+1;
  el("qtext").innerHTML = q.question || "";
  normalizeMathForQuiz(el("qtext"));
  renderMath();
  el("marking").innerHTML = `Marking: <span style="color:var(--success)">+${{Number(q.correct_score ?? 1)}}</span> / <span style="color:var(--danger)">-${{Number(q.negative_score ?? 0)}}</span>`;
  const opts = el("options");
  opts.innerHTML = "";
  el("explanation").style.display = "none";

  const keys = ["option_1","option_2","option_3","option_4","option_5"];
  keys.forEach((k, idx) => {{
    if(!q[k]) return;
    const div = document.createElement("div");
    div.className = "opt";
    div.innerHTML = `
    <div class="custom-radio" aria-hidden="true"></div>
    <div class="opt-text" style="flex:1">${{q[k]}}</div>
    `;
    opts.appendChild(div);

    // 🔥 NORMALIZE MATH IN OPTION TEXT
    normalizeMathForQuiz(div.querySelector(".opt-text"));

    div.addEventListener("click", () => selectOption(q, idx+1, div));

    // if previously answered, mark selected
    const qid = q.id ?? i;
    if(answers[qid] === String(idx+1)) div.classList.add("selected");
  }});

  highlightPalette();
  renderMath();
  saveQuizState();
}}

/* selecting option */
function selectOption(q, val, div){{
  const qid = q.id ?? current;
  answers[qid] = String(val);
  // clear previous selections visually
  Array.from(el("options").children).forEach(o => o.className = "opt");
  div.classList.add("selected");
  // immediate feedback in quiz mode
  if(isQuiz) showFeedback(q, val);
  highlightPalette();
  saveQuizState();
}}

/* show correct/wrong coloring and explanation */
function showFeedback(q, val){{
  const opts = Array.from(el("options").children);
  opts.forEach((o, idx) => {{
    o.classList.remove("correct","wrong");
    const idx1 = idx+1;
    if(String(q.answer) === String(idx1)){{
      o.classList.add("correct");
    }} else if(String(val) === String(idx1)){{
      o.classList.add("wrong");
    }}
  }});
  if(q.solution_text){{
    el("explanation").innerHTML = `<strong>Explanation:</strong> ${{q.solution_text}}`;
    el("explanation").style.display = "block";
    normalizeMathForQuiz(el("explanation"));
    renderMath();
  }}
}}

function loadPreviousAttempts(quizId, deviceId) {{
  const box = document.getElementById("previousAttempts");
  box.innerHTML = "";
  normalizeMathForQuiz(box);
  renderMath();
  box.style.display = "block";

  document.getElementById("results").style.display = "none";
  document.getElementById("attemptReplay").style.display = "none";

  db.ref("attempt_history/" + quizId + "/" + deviceId)
    .once("value")
    .then(snapshot => {{
      const data = snapshot.val();
      if (!data) {{
        box.innerHTML = "<p>No previous attempts found.</p>";
        return;
      }}
      ALL_ATTEMPTS_CACHE = data;

      const attempts = Object.values(data)
        .sort((a, b) => b.submittedAt - a.submittedAt);

      let html = `<div class="card"><h3 style="color:var(--accent)">Previous Attempts</h3>`;

      attempts.forEach((a, i) => {{
        html += `
          <button class="btn-ghost" onclick="showAttempt('${{a.submittedAt}}')">
            Attempt ${{attempts.length - i}} — ${{a.score}}
          </button>
        `;
      }});

      html += "</div>";
      box.innerHTML = html;
      normalizeMathForQuiz(box);
      renderMath();
    }});
}}

function showAttempt(submittedAt) {{
  const attempt = ALL_ATTEMPTS_CACHE[submittedAt];
  if (!attempt) return;

  const box = document.getElementById("attemptReplay");
  box.innerHTML = "";
  normalizeMathForQuiz(box);
  renderMath();
  box.style.display = "block";

  document.getElementById("previousAttempts").style.display = "none";
  document.getElementById("results").style.display = "none";

  let html = `
    <div class="card">
      <h3 style="color:var(--accent)">Attempt Review</h3>
      <div class="stats">
        <div class="stat"><h4>Score</h4><p>${{attempt.score}}</p></div>
        <div class="stat"><h4>Correct</h4><p>${{attempt.correct}}</p></div>
        <div class="stat"><h4>Wrong</h4><p>${{attempt.wrong}}</p></div>
        <div class="stat"><h4>Time Taken</h4><p>${{fmt(attempt.timeTaken)}}</p></div>
        <div class="stat"><h4>Rank</h4><p id="live-rank">...</p></div>
        <div class="stat"><h4>Percentile</h4><p id="live-percentile">...</p></div>
      </div>
    </div>
  `;

  attempt.answers.forEach((q, i) => {{
    html += `<div class="card">
      <div style="font-weight:600;margin-bottom:6px">
        Q${{i + 1}}: ${{q.question}}
      </div>`;

    q.options.forEach((opt, idx) => {{
      const id = String(idx + 1);
      let style = "padding:8px;border-radius:6px;margin:6px 0;border:1px solid #ddd;";
      if (id === q.correctAnswer) style += "border:2px solid var(--success);background:#eaf7f0;";
      else if (id === q.userAnswer) style += "border:2px solid var(--danger);background:#fdecec;";
      html += `<div style="${{style}}">${{opt}}</div>`;
    }});

    html += "</div>";
  }});

  box.innerHTML = html;
  normalizeMathForQuiz(box);
  renderMath();
  getLiveRankForAttempt("{quiz_name}", attempt)
  .then(data => {{
    document.getElementById("live-rank").textContent =
      `${{data.rank}} / ${{data.total}}`;
    document.getElementById("live-percentile").textContent =
      `${{data.percentile}}%`;
  }});

}}



/* Submit flow */
function submitQuiz(){{
  // stop timer
  if(timerInterval) clearInterval(timerInterval);
  const timeTakenSeconds = TOTAL_TIME_SECONDS - seconds;

  let correct = 0, wrong = 0, totalMarks = 0;
  let attemptedCount = 0;
  
  QUESTIONS.forEach((q, i) => {{
    const qid = q.id ?? i;
    const ans = answers[qid];
    if (ans) attemptedCount++;
    
    const isCorrect = ans && String(ans) === String(q.answer);
    if(isCorrect) correct++;
    else if(ans) wrong++;
    
    const cs = Number(q.correct_score ?? 1);
    const ns = Number(q.negative_score ?? 0);
    if(isCorrect) totalMarks += cs;
    else if(ans) totalMarks -= ns;
  }});
  
  // ✅ STEP 1: calculate maximum total marks
  let maxTotalMarks = 0;
  QUESTIONS.forEach(q => {{
    maxTotalMarks += Number(q.correct_score ?? 1);
  }});

  const attempted = Object.keys(answers).length;
  const unattempted = QUESTIONS.length - attempted;
  const accuracy = attempted ? ((correct/attempted) * 100).toFixed(1) : "0.0";


  // build review HTML
  let reviewHTML = `<div class="card"><h3 style="color:var(--accent);margin:0 0 10px">Results Summary</h3>
    <div class="stats">
      <div class="stat"><h4>Correct</h4><p>${{correct}}</p></div>
      <div class="stat"><h4>Wrong</h4><p>${{wrong}}</p></div>
      <div class="stat"><h4>Unattempted</h4><p>${{unattempted}}</p></div>
      <div class="stat"><h4>Accuracy</h4><p>${{accuracy}}%</p></div>
      <div class="stat"><h4>Total Marks</h4><p>${{totalMarks}} / ${{maxTotalMarks}}</p></div>
      <div class="stat"><h4>Time Taken</h4><p>${{fmt(timeTakenSeconds)}}</p></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
      <button class="btn-ghost" onclick="filterResults('all')">ALL</button>
      <button class="btn-ghost" onclick="filterResults('correct')">CORRECT</button>
      <button class="btn-ghost" onclick="filterResults('wrong')">WRONG</button>
      <button class="btn-ghost" onclick="filterResults('unattempted')">UNATTEMPTED</button>
    </div>
    
    <!-- PDF Download Button -->
    <button class="pdf-download-btn" onclick="downloadPDF()">
      📥 Download PDF Report
    </button>

</div></div>`;

  QUESTIONS.forEach((q,i)=>{{
    const qid = q.id ?? i;
    const ans = answers[qid];
    const isCorrect = ans && String(ans) === String(q.answer);
    const cs = Number(q.correct_score ?? 1);
    const ns = Number(q.negative_score ?? 0);

    const status = !ans ? "unattempted" : (isCorrect ? "correct" : "wrong");
    reviewHTML += `<div class="card result-q" data-status="${{status}}"><div style="font-weight:700;margin-bottom:8px">Q${{i+1}}: ${{q.question}}</div>`;
    ["option_1","option_2","option_3","option_4","option_5"].forEach((k,j)=>{{
      if(!q[k]) return;
      const idx = j+1;
      const isOptCorrect = String(idx) === String(q.answer);
      const isUser = ans && String(idx) === String(ans);
      let style = "padding:8px 10px;margin:6px 0;border-radius:8px;";
      if(isOptCorrect) style += "border:2px solid var(--success);background:rgba(31,158,90,0.12);";
      else if(isUser && !isOptCorrect) style += "border:2px solid var(--danger);background:rgba(200,45,63,0.12);";
      else style += "border:1px solid #eee;background:#fafbfd;";
      reviewHTML += `<div style="${{style}}">${{q[k]}}</div>`;
    }});
    const score = isCorrect ? `<span style="color:var(--success)">+${{cs}}</span>` : (ans ? `<span style="color:var(--danger)">-${{ns}}</span>` : `<span style="color:var(--muted)">0</span>`);
    reviewHTML += `<div style="margin-top:8px"><strong>Score:</strong> ${{score}}</div>`;
    if(q.solution_text) reviewHTML += `<div class="explanation" style="display:block;margin-top:8px"><strong>Explanation:</strong> ${{q.solution_text}}</div>`;
    reviewHTML += `</div>`;
  }});

  el("results").innerHTML = reviewHTML;
  normalizeMathForQuiz(el("results"));
  renderMath();
  el("results").style.display = "block";

  el("quizCard").style.display = "none";
  // hide bottom nav
  el("floatBar").style.display = "none";
  LAST_RESULT_HTML = reviewHTML; // ✅ FIX: store latest result immediately

  document.getElementById("mainHeader")?.style.setProperty("display", "block");




const timeTaken = timeTakenSeconds;

const firebasePayload = {{
  name: "Anonymous",
  score: totalMarks,
  total: maxTotalMarks,
  correct,
  wrong,
  unattempted,
  timeTaken,
  quizId: "{quiz_name}",
  deviceId: DEVICE_ID,
  submittedAt: Date.now(),
  answers: QUESTIONS.map((q, i) => {{
    const qid = q.id ?? i;
    return {{
      question: q.question,
      options: [
        q.option_1,
        q.option_2,
        q.option_3,
        q.option_4,
        q.option_5
      ].filter(Boolean),
      correctAnswer: String(q.answer),
      userAnswer: answers[qid] ?? null
    }};
  }})
}};


// 🔥 ADMIN DATA (first attempt only)
saveResultFirebase(firebasePayload)
  .finally(() => {{
    saveAttemptHistory(firebasePayload);

    // 🔥 RESULT PAGE (always show rank, even on reattempt)
    getRankAndPercentile("{quiz_name}", totalMarks, timeTaken, firebasePayload.submittedAt)
      .then(data => {{

        firebasePayload.rank = data.rank;
        firebasePayload.percentile = data.percentile;

        // 🔥 UPDATE THIS ATTEMPT WITH RANK & PERCENTILE
        db.ref(
        "attempt_history/" +
        firebasePayload.quizId + "/" +
        firebasePayload.deviceId + "/" +
        firebasePayload.submittedAt
        ).update({{
        rank: data.rank,
        percentile: data.percentile
        }});

        const rankHTML = `
          <div class="stat">
            <h4>Rank</h4>
            <p>${{data.rank}} / ${{data.total}}</p>
          </div>
          <div class="stat">
            <h4>Percentile</h4>
            <p>${{data.percentile}}%</p>
          </div>
        `;

        document
          .querySelector("#results .stats")
          ?.insertAdjacentHTML("beforeend", rankHTML);
          setTimeout(() => {{
            const finalResultHTML = document.getElementById("results").innerHTML;
            const headerHTML = document.getElementById("headerControls").innerHTML;

            LAST_RESULT_HTML = finalResultHTML;

            // 🔐 SAVE COMPLETE RESULT + HEADER STATE
            localStorage.setItem(QUIZ_RESULT_KEY, JSON.stringify({{
              submitted: true,
              resultHTML: finalResultHTML,
              headerHTML: headerHTML
            }}));
          }}, 0);
      }});
  }});


  // replace header controls with results controls (Re-Attempt, Download, Print)
  const header = el("headerControls");
  header.innerHTML = "";
  const left = document.createElement("div");
  left.style.display = "flex";
  left.style.alignItems = "center";
  left.style.gap = "10px";
  const title = document.createElement("h1");
  title.textContent = QUIZ_TITLE;
  left.appendChild(title);

  const right = document.createElement("div");
  right.style.display = "flex";
  right.style.alignItems = "center";
  right.style.gap = "10px";

  const retry = document.createElement("button");
  retry.className = "btn";
  retry.textContent = "Re-Attempt";
  retry.onclick = ()=> {{
    localStorage.removeItem(QUIZ_RESULT_KEY);
    localStorage.removeItem(QUIZ_STATE_KEY);
    location.reload();
  }};

  right.appendChild(retry);
  const prevBtn = document.createElement("button");
  prevBtn.className = "btn-ghost";
  prevBtn.textContent = "Previous Attempts";
  prevBtn.onclick = () => {{
    const res = document.getElementById("results");
    res.innerHTML = "";                    // clear latest result
    res.style.display = "block";
    loadPreviousAttempts("{quiz_name}", DEVICE_ID);
  }};


  const backBtn = document.createElement("button");
  backBtn.className = "btn-ghost";
  backBtn.textContent = "Back to Latest Result";
  backBtn.onclick = () => {{
    document.getElementById("results").innerHTML = LAST_RESULT_HTML;
    document.getElementById("results").style.display = "block";
  }};


  right.appendChild(prevBtn);
  right.appendChild(backBtn);

  // Download & Print intentionally disabled
  header.appendChild(left);
  header.appendChild(right);
}}

/* Download current results page as .html named after quiz title */
function downloadResults(){{
  const head = document.head.outerHTML;
  const resultsHtml = el("results").outerHTML;
  const pageHtml = `<!doctype html><html>${{head}}<body><div style="padding:20px;max-width:1000px;margin:auto">${{resultsHtml}}</div></body></html>`;
  const blob = new Blob([pageHtml], {{type:"text/html"}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const safe = String(QUIZ_TITLE).replace(/[^a-z0-9]/gi,"_");
  a.href = url;
  a.download = `${{safe}}_results.html`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}}

/* Download PDF with results and explanations */
function downloadPDF() {{
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF('p', 'mm', 'a4');
  
  // Title
  doc.setFontSize(20);
  doc.text(QUIZ_TITLE, 105, 15, {{ align: 'center' }});
  
  // Summary
  doc.setFontSize(12);
  const stats = document.querySelectorAll('.stat p');
  const summary = [
    `Correct: ${{stats[0]?.textContent || 0}}`,
    `Wrong: ${{stats[1]?.textContent || 0}}`,
    `Unattempted: ${{stats[2]?.textContent || 0}}`,
    `Accuracy: ${{stats[3]?.textContent || '0%'}}`,
    `Total Marks: ${{stats[4]?.textContent || '0/0'}}`,
    `Time Taken: ${{stats[5]?.textContent || '00:00'}}`,
    `Rank: ${{stats[6]?.textContent || 'N/A'}}`,
    `Percentile: ${{stats[7]?.textContent || '0%'}}`
  ];
  
  let yPos = 30;
  doc.setFontSize(14);
  doc.text('Summary:', 20, yPos);
  yPos += 10;
  
  doc.setFontSize(11);
  summary.forEach((stat, i) => {{
    const col = i % 2;
    const row = Math.floor(i / 2);
    doc.text(stat, 20 + (col * 90), yPos + (row * 7));
  }});
  
  yPos += 25;
  
  // Questions
  const questions = document.querySelectorAll('.result-q');
  questions.forEach((q, idx) => {{
    if (yPos > 270) {{
      doc.addPage();
      yPos = 20;
    }}
    
    const questionText = q.querySelector('div[style*="font-weight:700"]')?.textContent || `Q${{idx+1}}`;
    const options = q.querySelectorAll('div[style*="padding:8px 10px"]');
    const explanation = q.querySelector('.explanation');
    
    doc.setFontSize(12);
    doc.setFont(undefined, 'bold');
    doc.text(questionText, 20, yPos, {{ maxWidth: 170 }});
    yPos += doc.getTextDimensions(questionText, {{ maxWidth: 170 }}).h + 5;
    
    doc.setFont(undefined, 'normal');
    options.forEach((opt, optIdx) => {{
      const style = opt.getAttribute('style');
      const isCorrect = style?.includes('var(--success)');
      const isWrong = style?.includes('var(--danger)');
      
      let prefix = String.fromCharCode(97 + optIdx) + ') ';
      if (isCorrect) prefix = '✓ ' + prefix;
      if (isWrong) prefix = '✗ ' + prefix;
      
      const text = prefix + opt.textContent;
      doc.text(text, 25, yPos, {{ maxWidth: 160 }});
      yPos += doc.getTextDimensions(text, {{ maxWidth: 160 }}).h + 3;
    }});
    
    if (explanation && explanation.style.display !== 'none') {{
      yPos += 5;
      doc.setFont(undefined, 'italic');
      doc.text('Explanation: ' + explanation.textContent.replace('Explanation:', '').trim(), 20, yPos, {{ maxWidth: 170 }});
      yPos += doc.getTextDimensions('Explanation: ' + explanation.textContent, {{ maxWidth: 170 }}).h + 10;
    }}
    
    yPos += 10;
  }});
  
  doc.save(`${{QUIZ_TITLE.replace(/[^a-zA-Z0-9]/g, '_')}}_Report.pdf`);
}}

function markForReview() {{
  if (marked.has(current)) {{
    marked.delete(current);
  }} else {{
    marked.add(current);
  }}
  highlightPalette();
  saveQuizState();
}}

function saveAndNext() {{
  const qid = QUESTIONS[current].id ?? current;
  if (answers[qid]) {{
    if (current < QUESTIONS.length - 1) {{
      renderQuestion(current + 1);
    }}
  }} else {{
    alert("Please select an answer before proceeding.");
  }}
}}

/* attach DOM listeners */
function attachListeners(){{
  el("nextBtn").addEventListener("click", ()=> {{ if(current < QUESTIONS.length-1) renderQuestion(current+1); }});
  el("prevBtn").addEventListener("click", ()=> {{ if(current > 0) renderQuestion(current-1); }});
  el("clearBtn").addEventListener("click", ()=> {{ delete answers[QUESTIONS[current].id ?? current]; renderQuestion(current); highlightPalette(); }});
  el("markBtn").addEventListener("click", markForReview);
  el("saveNextBtn").addEventListener("click", saveAndNext);

  el("paletteBtn").addEventListener("click", (e) => {{
    e.stopPropagation();
    togglePalette();
  }});

  el("submitBtn").addEventListener("click", ()=>{{
    const attempted = Object.keys(answers).length;
    el("submitMsg").textContent = `You attempted ${{attempted}} of ${{QUESTIONS.length}}. Submit?`;
    el("submitModal").style.display = "flex";
  }});

  el("cancelSubmit").addEventListener("click", ()=> el("submitModal").style.display = "none");
  el("confirmSubmit").addEventListener("click", ()=> {{ el("submitModal").style.display = "none"; submitQuiz(); }});

  el("modeToggle").addEventListener("click", ()=> {{
    el("modeToggle").classList.toggle("active");
    isQuiz = el("modeToggle").classList.contains("active");
    // re-render the current question so immediate behavior toggles
    renderQuestion(current);
  }});

  // close palette when clicking outside
  document.addEventListener("click", (ev) => {{
    const pal = el("palette");
    if(!pal) return;
    if(pal.style.display === "flex"){{
      const viewBtn = el("paletteBtn");
      if(ev.target !== pal && !pal.contains(ev.target) && ev.target !== viewBtn && !viewBtn.contains(ev.target)){{
        pal.style.display = "none";
      }}
    }}
  }});
}}

/* Build palette buttons + summary */
function buildPalette(){{
  const pal = el("palette");
  pal.innerHTML = "";
  for(let i=0;i<QUESTIONS.length;i++){{
    const b = document.createElement("button");
    b.className = "qbtn";
    b.textContent = i+1;
    b.addEventListener("click", (e)=>{{
      e.stopPropagation();
      renderQuestion(i);
      pal.style.display = "none";
    }});
    pal.appendChild(b);
  }}
  const summary = document.createElement("div");
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
  const markedCount = marked.size;
  const unattempted = total - attempted;
  Array.from(pal.children).forEach((child, idx) => {{
    // skip summary
    if(child.id === "palette-summary") return;
    child.classList.remove("answered","unattempted","marked","current");
    
    const qid = QUESTIONS[idx].id ?? idx;
    if(idx === current) {{
      child.classList.add("current");
    }} else if(marked.has(idx)) {{
      child.classList.add("marked");
    }} else if(answers[qid]) {{
      child.classList.add("answered");
    }} else {{
      child.classList.add("unattempted");
    }}
  }});
  const summary = el("palette-summary");
  if(summary) {{
    summary.innerHTML = `
      <div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin-bottom:5px;">
        <div class="status-indicator"><span class="status-dot status-answered"></span> Answered</div>
        <div class="status-indicator"><span class="status-dot status-unattempted"></span> Unattempted</div>
        <div class="status-indicator"><span class="status-dot status-marked"></span> Marked</div>
        <div class="status-indicator"><span class="status-dot status-unseen"></span> Unseen</div>
      </div>
      <div>Total: ${{total}} | Attempted: ${{attempted}} | Marked: ${{markedCount}} | Unseen: ${{total - attempted - markedCount}}</div>
    `;
  }}
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



// 🔥 FIREBASE SDK
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

# Bot Handlers (remain the same as before)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    update_activity()
    await update.message.reply_text(
        "📚 *Quiz Generator Bot*\n\n"
        "Send me a TXT file with questions in any format:\n\n"
        "Example formats:\n"
        "1. Question text\n"
        "a) Option 1\n"
        "b) Option 2\n"
        "Correct option:-a\n"
        "ex: Explanation\n\n"
        "OR\n\n"
        "Q.1 Question text\n"
        "(a) Option 1\n"
        "(b) Option 2\n"
        "Answer: (a)\n\n"
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
        [InlineKeyboardButton("1", callback_data="1"),
         InlineKeyboardButton("2", callback_data="2"),
         InlineKeyboardButton("3", callback_data="3"),
         InlineKeyboardButton("4", callback_data="4")],
        [InlineKeyboardButton("Custom", callback_data="custom_marks")]
    ]
    
    await query.edit_message_text(
        "✍️ Select marks per question (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_MARKS

async def get_time_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom time input"""
    update_activity()
    try:
        time_minutes = int(update.message.text)
        if time_minutes <= 0:
            raise ValueError
        context.user_data["time"] = str(time_minutes)
    except:
        await update.message.reply_text("Please enter a valid number (minutes):")
        return GETTING_TIME
    
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"),
         InlineKeyboardButton("2", callback_data="2"),
         InlineKeyboardButton("3", callback_data="3"),
         InlineKeyboardButton("4", callback_data="4")],
        [InlineKeyboardButton("Custom", callback_data="custom_marks")]
    ]
    
    await update.message.reply_text(
        "✍️ Select marks per question (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_MARKS

async def get_marks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle marks selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom_marks":
        await query.edit_message_text("Enter marks per question:")
        return GETTING_MARKS
    
    context.user_data["marks"] = query.data
    
    keyboard = [
        [InlineKeyboardButton("0 (No negative)", callback_data="0"),
         InlineKeyboardButton("0.25", callback_data="0.25"),
         InlineKeyboardButton("0.5", callback_data="0.5"),
         InlineKeyboardButton("1", callback_data="1")],
        [InlineKeyboardButton("Custom", callback_data="custom_negative")]
    ]
    
    await query.edit_message_text(
        "⚠️ Select negative marking per wrong answer (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_NEGATIVE

async def get_marks_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom marks input"""
    update_activity()
    try:
        marks = float(update.message.text)
        if marks <= 0:
            raise ValueError
        context.user_data["marks"] = str(marks)
    except:
        await update.message.reply_text("Please enter a valid number for marks:")
        return GETTING_MARKS
    
    keyboard = [
        [InlineKeyboardButton("0 (No negative)", callback_data="0"),
         InlineKeyboardButton("0.25", callback_data="0.25"),
         InlineKeyboardButton("0.5", callback_data="0.5"),
         InlineKeyboardButton("1", callback_data="1")],
        [InlineKeyboardButton("Custom", callback_data="custom_negative")]
    ]
    
    await update.message.reply_text(
        "⚠️ Select negative marking per wrong answer (or Custom):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GETTING_NEGATIVE

async def get_negative(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle negative marking selection"""
    update_activity()
    query = update.callback_query
    await query.answer()
    
    if query.data == "custom_negative":
        await query.edit_message_text("Enter negative marking value:")
        return GETTING_NEGATIVE
    
    context.user_data["negative"] = query.data
    
    await query.edit_message_text("🏆 Enter creator name:")
    return GETTING_CREATOR

async def get_negative_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom negative marking input"""
    update_activity()
    try:
        negative = float(update.message.text)
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
        
        # Generate quiz HTML
        html_content = generate_html_quiz(context.user_data)
        
        # Save HTML file
        safe_name = re.sub(r'[^\w\s-]', '', context.user_data['name'])
        safe_name = re.sub(r'[-\s]+', '_', safe_name)
        html_file = f"{safe_name}.html"
        
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        # Update progress
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=progress_msg.message_id,
            text=f"{summary}\n\n✅ Quiz generated! Sending file..."
        )
        
        # Send HTML file with proper caption format
        with open(html_file, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=html_file,
                caption=f"✅ *Quiz Generated Successfully!*\n\n"
                       f"Download and open in any browser.\n\n"
                       f"📘 QUIZ ID: {context.user_data['name']}\n"
                       f"📊 TOTAL QUESTIONS: {total_questions}\n"
                       f"⏱️ TIME: {context.user_data['time']} Minutes\n"
                       f"✍️ EACH QUESTION MARK: {context.user_data['marks']}\n"
                       f"⚠️ NEGATIVE MARKING: {context.user_data['negative']}\n"
                       f"🏆 CREATED BY: {context.user_data['creator']}",
                parse_mode="Markdown"
            )
        
        # Cleanup
        os.remove(html_file)
        if user_id in user_progress:
            del user_progress[user_id]
        
        # Clear user data
        context.user_data.clear()
        
    except Exception as e:
        logger.error(f"Error generating quiz: {e}")
        await update.message.reply_text(f"❌ Error generating quiz: {str(e)}")
        
        if user_id in user_progress:
            del user_progress[user_id]
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the conversation"""
    update_activity()
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    update_activity()
    help_text = """
📚 *Quiz Generator Bot Help*

*Commands:*
/start - Start creating a new quiz
/help - Show this help message
/wake - Keep the bot awake
/status - Check bot status
/cancel - Cancel current operation

*File Format:*
Your TXT file can have any of these formats:

Format 1:
1. Question text
a) Option 1
b) Option 2
Correct option:-a
ex: Explanation text...

Format 2:
Q.1 Question text?
(a) Option 1
(b) Option 2
Answer: (a)

*Features:*
• Parses any TXT format with questions
• Interactive quiz interface with color-coded view
• Timer with countdown
• Test/Quiz mode toggle
• Rank and percentile system
• Previous attempts tracking
• Firebase integration
• Mobile, tablet & desktop responsive design
• Mark for review feature
• PDF download with explanations

*New Features:*
• Color-coded question palette:
  - Green: Attempted
  - Red: Unattempted  
  - Purple: Marked for review
  - Bold: Current question
• Mark for review button
• Save & Next button
• PDF download with full report
• Optimized for all devices
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual wake command"""
    update_activity()
    keep_alive_ping()
    await update.message.reply_text("🔔 Bot is awake and active!")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot status"""
    update_activity()
    status_text = (
        f"🤖 *Bot Status*\n\n"
        f"• Status: ✅ Running\n"
        f"• Last activity: {datetime.fromtimestamp(last_activity).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"• Active users: {len(user_data)}\n"
        f"• Active processes: {len(user_progress)}\n"
        f"• Render URL: {RENDER_APP_URL if RENDER_APP_URL else 'Not set'}\n"
        f"• Keep-alive interval: {KEEP_ALIVE_INTERVAL//60} minutes\n\n"
        f"*Commands:*\n"
        f"/start - Create new quiz\n"
        f"/help - Show help\n"
        f"/wake - Force wake-up\n"
        f"/status - This status"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    error = context.error
    if "terminated by other getUpdates request" in str(error):
        logger.warning("Another bot instance is running. This is normal during deployment.")
        return
    logger.error(f"Update {update} caused error {error}")

def main():
    """Start the bot"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return
    
    # Start health server in a separate thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    # Start keep-alive worker if RENDER_APP_URL is set
    if RENDER_APP_URL:
        keep_alive_thread = threading.Thread(target=keep_alive_worker, daemon=True)
        keep_alive_thread.start()
        logger.info("Keep-alive worker started")
    else:
        logger.warning("RENDER_APP_URL not set - keep-alive disabled")
    
    # Create and configure bot application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )
    
    # Create conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            GETTING_FILE: [
                MessageHandler(filters.Document.FileExtension("txt"), handle_document),
                CommandHandler("cancel", cancel)
            ],
            GETTING_QUIZ_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_quiz_name),
                CommandHandler("cancel", cancel)
            ],
            GETTING_TIME: [
                CallbackQueryHandler(get_time, pattern="^(15|20|25|30|custom)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_time_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_MARKS: [
                CallbackQueryHandler(get_marks, pattern="^(1|2|3|4|custom_marks)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_marks_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_NEGATIVE: [
                CallbackQueryHandler(get_negative, pattern="^(0|0.25|0.5|1|custom_negative)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_negative_custom),
                CommandHandler("cancel", cancel)
            ],
            GETTING_CREATOR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_creator),
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
