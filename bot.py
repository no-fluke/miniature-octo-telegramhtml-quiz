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
    
    # Normalize line endings and split into lines
    lines = [line.rstrip('\n\r') for line in content.split('\n') if line.strip()]
    
    i = 0
    while i < len(lines):
        # Look for question start patterns
        if (re.match(r'^\d+[\.\)]\s*', lines[i]) or 
            re.match(r'^Q\.?\d*\.?\s*', lines[i], re.IGNORECASE)):
            
            question = {
                "question": "",
                "option_1": "", "option_2": "", "option_3": "", "option_4": "", "option_5": "",
                "answer": "",
                "solution_text": ""
            }
            
            # Extract question text
            question_lines = []
            # Remove question number prefix
            q_line = re.sub(r'^(?:\d+[\.\)]|Q\.?\d*\.?)\s*', '', lines[i])
            question_lines.append(q_line.strip())
            i += 1
            
            # Continue adding question lines until we hit an option or answer
            while i < len(lines) and not (
                re.match(r'^[a-e][\.\)]\s*', lines[i], re.IGNORECASE) or
                re.match(r'^\([a-e]\)\s*', lines[i], re.IGNORECASE) or
                re.match(r'^[a-e]\.\s*', lines[i], re.IGNORECASE) or
                re.match(r'^\s*[a-e]\s*\)', lines[i], re.IGNORECASE) or
                'Correct' in lines[i] or 
                'Answer:' in lines[i] or
                re.match(r'^\s*[a-e]\s*$', lines[i]) or
                re.match(r'^[A-E]\s*\)', lines[i])
            ):
                if lines[i].strip():
                    question_lines.append(lines[i].strip())
                i += 1
            
            question["question"] = '<br>'.join(filter(None, question_lines))
            
            # Extract options
            option_count = 0
            option_patterns = [
                r'^[a-e][\.\)]\s*(.*)',
                r'^\([a-e]\)\s*(.*)',
                r'^[a-e]\.\s*(.*)',
                r'^[A-E]\s*\)\s*(.*)',
                r'^\s*[a-e]\s*\)\s*(.*)'
            ]
            
            while i < len(lines) and option_count < 5:
                option_text = None
                for pattern in option_patterns:
                    match = re.match(pattern, lines[i], re.IGNORECASE)
                    if match:
                        option_text = match.group(1).strip()
                        break
                
                if option_text:
                    option_key = f"option_{option_count + 1}"
                    question[option_key] = option_text
                    option_count += 1
                    i += 1
                    
                    # Check for multi-line option text
                    while i < len(lines) and not (
                        re.match(r'^[a-e][\.\)]\s*', lines[i], re.IGNORECASE) or
                        re.match(r'^\([a-e]\)\s*', lines[i], re.IGNORECASE) or
                        re.match(r'^[a-e]\.\s*', lines[i], re.IGNORECASE) or
                        'Correct' in lines[i] or 
                        'Answer:' in lines[i] or
                        re.match(r'^[A-E]\s*\)', lines[i]) or
                        re.match(r'^\s*[a-e]\s*$', lines[i])
                    ):
                        if lines[i].strip():
                            question[option_key] += f"<br>{lines[i].strip()}"
                        i += 1
                else:
                    break
            
            # Extract correct answer
            while i < len(lines) and ('Correct' in lines[i] or 'Answer:' in lines[i]):
                line = lines[i]
                answer_match = None
                
                # Try different answer patterns
                patterns = [
                    r'Correct\s*[Oo]ption\s*[:-]\s*([a-e])',
                    r'Answer\s*[:\(]\s*([a-e])',
                    r'Correct\s*answer\s*[:-]\s*([a-e])',
                    r'^[a-e]\s*$'
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        answer_match = match.group(1).lower()
                        break
                
                if answer_match:
                    answer_map = {'a': '1', 'b': '2', 'c': '3', 'd': '4', 'e': '5'}
                    question["answer"] = answer_map.get(answer_match, '1')
                    i += 1
                    break
                i += 1
            
            # Extract explanation
            solution_lines = []
            while i < len(lines) and ('ex:' in lines[i].lower() or 
                                     'explanation:' in lines[i].lower() or
                                     'explain:' in lines[i].lower()):
                line = lines[i]
                if 'ex:' in line.lower():
                    solution_lines.append(line.split(':', 1)[1].strip())
                elif 'explanation:' in line.lower():
                    solution_lines.append(line.split(':', 1)[1].strip())
                elif 'explain:' in line.lower():
                    solution_lines.append(line.split(':', 1)[1].strip())
                else:
                    solution_lines.append(line.strip())
                i += 1
            
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
            
            questions.append(question)
        else:
            i += 1
    
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
<!-- Favicon -->
<link rel="icon" type="image/x-icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
<style>
:root{{
  --accent:#2ec4b6;
  --accent-dark:#1da89a;
  --muted:#69707a;
  --success:#1f9e5a;
  --danger:#c82d3f;
  --warning:#f59e0b;
  --info:#3b82f6;
  --purple:#8b5cf6;
  --bg:#f5f7fa;
  --card:#fff;
  --maxw:820px;
  --radius:10px;
  --shadow:0 4px 10px rgba(0,0,0,0.05);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{margin:0;font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:var(--bg);color:#111;line-height:1.6;padding-bottom:96px;min-height:100vh}}
.container{{max-width:var(--maxw);margin:auto;padding:10px 16px;width:100%}}

/* 🔥 HEADER STYLES */
header{{background:#fff;box-shadow:0 2px 6px rgba(0,0,0,0.08);position:sticky;top:0;z-index:100}}
.header-inner{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;gap:12px;flex-wrap:wrap}}
h1{{margin:0;color:var(--accent);font-size:18px;font-weight:700}}
.btn{{background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer;transition:background .18s, transform .1s}}
.btn:hover{{background:var(--accent-dark);transform:translateY(-1px)}}
.btn:active{{transform:translateY(0)}}
.btn-ghost{{background:#fff;color:var(--accent);border:2px solid var(--accent);padding:8px 12px;border-radius:999px;font-weight:700;cursor:pointer;transition:all .18s}}
.btn-ghost:hover{{background:var(--accent);color:#fff}}
.timer-text{{color:var(--accent-dark);font-weight:700;font-size:18px;min-width:72px;text-align:right}}
.toggle-pill{{position:relative;width:90px;height:30px;background:#eaeef0;border-radius:999px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;padding:0 6px;font-size:13px;color:#444;font-weight:600}}
.toggle-pill span{{z-index:2;flex:1;text-align:center}}
.toggle-pill::before{{content:"";position:absolute;top:3px;left:3px;width:42px;height:24px;background:var(--accent);border-radius:999px;transition:.28s}}
.toggle-pill.active::before{{transform:translateX(45px);background:var(--accent-dark)}}
.toggle-pill.active span:last-child{{color:#fff}}
.toggle-pill span:first-child{{color:#fff}}

/* 📱 RESPONSIVE HEADER */
@media(max-width:768px){{
  .header-inner{{flex-direction:column;gap:8px}}
  .timer-text{{order:-1;width:100%;text-align:center}}
  h1{{font-size:16px}}
}}

/* 📄 QUIZ CARD */
.card{{background:var(--card);border-radius:var(--radius);padding:16px;margin:12px 0;box-shadow:var(--shadow);transition:transform .2s}}
.card:hover{{transform:translateY(-2px)}}
.qbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px}}
.qmeta{{font-size:14px;color:var(--muted);font-weight:500}}
.marking{{font-size:14px;color:var(--muted);font-weight:500}}
.qtext{{font-size:17px;margin:12px 0;font-weight:600;line-height:1.7}}
.opt{{padding:14px;border:1.5px solid #e6eaec;border-radius:10px;background:#fff;cursor:pointer;display:flex;align-items:flex-start;gap:12px;transition:all .15s;margin:10px 0}}
.opt:hover{{border-color:var(--accent);background:#f8fdfc}}
.opt.selected{{border-color:var(--accent);background:rgba(46,196,182,0.08)}}
.opt.correct{{border-color:var(--success);background:rgba(31,158,90,0.12)}}
.opt.wrong{{border-color:var(--danger);background:rgba(200,45,63,0.12)}}
.custom-radio{{flex-shrink:0;width:20px;height:20px;border-radius:50%;border:2px solid #ccc;position:relative}}
.opt.selected .custom-radio{{border-color:var(--accent)}}
.opt.selected .custom-radio::after{{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:10px;height:10px;background:var(--accent);border-radius:50%}}
.opt.correct .custom-radio{{border-color:var(--success)}}
.opt.correct .custom-radio::after{{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:10px;height:10px;background:var(--success);border-radius:50%}}
.opt.wrong .custom-radio{{border-color:var(--danger)}}
.opt.wrong .custom-radio::after{{content:"";position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:10px;height:10px;background:var(--danger);border-radius:50%}}
.opt-text{{flex:1;font-size:15px;line-height:1.6}}
.explanation{{margin-top:16px;padding:14px;border-radius:10px;background:#f9fcfd;border-left:4px solid var(--accent);display:none;font-size:15px;line-height:1.7}}

/* 📱 RESPONSIVE QUIZ CARD */
@media(max-width:540px){{
  .card{{padding:12px}}
  .qtext{{font-size:16px}}
  .opt{{padding:12px;gap:10px}}
  .opt-text{{font-size:14px}}
}}

/* ⬇️ BOTTOM NAV */
.fbar{{position:fixed;left:0;right:0;bottom:0;background:#fff;box-shadow:0 -3px 12px rgba(0,0,0,0.08);display:flex;justify-content:center;z-index:50}}
.fbar-inner{{display:flex;justify-content:space-between;align-items:center;gap:8px;max-width:var(--maxw);width:100%;padding:10px}}
.fbar button{{flex:1;background:var(--accent);color:#fff;border:none;border-radius:6px;padding:10px;font-size:14px;font-weight:600;cursor:pointer;transition:background .18s}}
.fbar button:hover{{background:var(--accent-dark)}}
.fbar button#markReviewBtn{{background:var(--purple)}}
.fbar button#markReviewBtn:hover{{background:#7c3aed}}
.fbar button#clearBtn{{background:var(--danger)}}
.fbar button#clearBtn:hover{{background:#b91c1c}}

/* 📱 RESPONSIVE BOTTOM NAV */
@media(max-width:768px){{
  .fbar-inner{{flex-wrap:wrap}}
  .fbar button{{min-width:calc(50% - 4px);margin-bottom:4px}}
}}
@media(max-width:480px){{
  .fbar button{{min-width:100%;margin-bottom:6px}}
  .fbar button:last-child{{margin-bottom:0}}
}}

/* 🎨 PALETTE POPUP */
#palette{{position:fixed;top:60px;right:14px;background:#fff;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,0.12);padding:16px;display:none;gap:8px;flex-wrap:wrap;z-index:200;max-width:320px;max-height:70vh;overflow-y:auto;overscroll-behavior:contain;border:1px solid #e3eaeb}}
#palette .qbtn{{width:48px;height:48px;border-radius:10px;border:2px solid #e3eaeb;background:#fbfdff;cursor:pointer;font-weight:700;font-size:14px;display:flex;align-items:center;justify-content:center;transition:all .15s}}
#palette .qbtn:hover{{transform:scale(1.05);box-shadow:0 3px 8px rgba(0,0,0,0.1)}}
#palette .qbtn.attempted{{background:var(--success);color:#fff;border-color:var(--success)}}
#palette .qbtn.unattempted{{background:var(--danger);color:#fff;border-color:var(--danger)}}
#palette .qbtn.marked{{background:var(--purple);color:#fff;border-color:var(--purple)}}
#palette .qbtn.unseen{{background:#f8f9fa;color:#333;border-color:#dee2e6}}
#palette .qbtn.current{{border:3px solid var(--accent);font-weight:900;box-shadow:0 0 0 2px rgba(46,196,182,0.2)}}
#palette-summary{{margin-top:12px;font-size:13px;color:var(--muted);text-align:center;width:100%;padding-top:10px;border-top:1px solid #eee}}

/* 📱 RESPONSIVE PALETTE */
@media(max-width:768px){{
  #palette{{top:120px;left:50%;transform:translateX(-50%);max-width:90%;max-height:50vh}}
}}

/* 🪟 MODAL */
.modal{{position:fixed;inset:0;background:rgba(0,0,0,0.45);display:none;align-items:center;justify-content:center;z-index:300}}
.modal-content{{background:#fff;border-radius:12px;padding:22px;max-width:420px;width:92%;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,0.18)}}
.modal h3{{margin:0 0 12px;color:var(--accent);font-size:20px}}
.modal p{{color:#333;margin:12px 0 16px;font-size:15px;line-height:1.6}}
.modal .actions{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}}
.btn-primary{{background:linear-gradient(135deg,var(--accent),var(--accent-dark));color:#fff;border:none;padding:10px 18px;border-radius:999px;font-weight:700;cursor:pointer;font-size:15px}}

/* 📊 RESULTS */
      if(q.solution_text) reviewHTML += `<div class="explanation" style="display:block;margin-top:8px"><strong>Explanation:</strong> ${{q.solution_text}}</div>`;
    reviewHTML += `</div>`;
  }});

  el("results").innerHTML = reviewHTML;
  normalizeMathForQuiz(el("results"));
  renderMath();
  el("results").style.display = "block";
  el("quizCard").style.display = "none";
  el("floatBar").style.display = "none";
  LAST_RESULT_HTML = reviewHTML;

    // Prepare Firebase payload
  const firebasePayload = {{
    score: totalMarks,
    total: maxTotalMarks,
    correct,
    wrong,
    unattempted,
    timeTaken: timeTakenSeconds,
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

  // Save to Firebase
  saveResultFirebase(firebasePayload)
    .finally(() => {{
      saveAttemptHistory(firebasePayload);
      getRankAndPercentile("{quiz_name}", totalMarks, timeTakenSeconds, firebasePayload.submittedAt)
        .then(data => {{
          firebasePayload.rank = data.rank;
          firebasePayload.percentile = data.percentile;
          
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

          document.querySelector("#results .stats")
            ?.insertAdjacentHTML("beforeend", rankHTML);
          
          setTimeout(() => {{
            const finalResultHTML = document.getElementById("results").innerHTML;
            const headerHTML = document.getElementById("headerControls").innerHTML;
            LAST_RESULT_HTML = finalResultHTML;

            localStorage.setItem(QUIZ_RESULT_KEY, JSON.stringify({{
              submitted: true,
              resultHTML: finalResultHTML,
              headerHTML: headerHTML
            }}));
          }}, 0);
        }});
    }});

  // Update header
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

  const pdfBtn = document.createElement("button");
  pdfBtn.className = "pdf-btn";
  pdfBtn.innerHTML = "📄 Download PDF";
  pdfBtn.onclick = downloadPDF;

  const prevBtn = document.createElement("button");
  prevBtn.className = "btn-ghost";
  prevBtn.textContent = "Previous Attempts";
  prevBtn.onclick = () => {{
    const res = document.getElementById("results");
    res.innerHTML = "";
    res.style.display = "block";
    loadPreviousAttempts("{quiz_name}", DEVICE_ID);
  }};

  const backBtn = document.createElement("button");
  backBtn.className = "btn-ghost";
  backBtn.textContent = "Back to Results";
  backBtn.onclick = () => {{
    document.getElementById("results").innerHTML = LAST_RESULT_HTML;
    document.getElementById("results").style.display = "block";
  }};

  right.appendChild(retry);
  right.appendChild(pdfBtn);
  right.appendChild(prevBtn);
  right.appendChild(backBtn);

  header.appendChild(left);
  header.appendChild(right);
}}

// Download PDF
function downloadPDF(){{
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF('p', 'mm', 'a4');
  const margin = 10;
  let y = margin;
  
  // Add title
  doc.setFontSize(20);
  doc.text(QUIZ_TITLE, margin, y);
  y += 10;
  
  // Add summary
  doc.setFontSize(12);
  const stats = document.querySelectorAll('.stat');
  let statsText = '';
  stats.forEach(stat => {{
    const title = stat.querySelector('h4').textContent;
    const value = stat.querySelector('p').textContent;
    statsText += `${{title}}: ${{value}} | `;
  }});
  doc.text(statsText, margin, y);
  y += 15;
  
  // Add questions
  QUESTIONS.forEach((q, i) => {{
    if(y > 270) {{ // New page if needed
      doc.addPage();
      y = margin;
    }}
    
    doc.setFontSize(14);
    doc.text(`Q${{i+1}}: ${{q.question.replace(/<br>/g, ' ')}}`, margin, y);
    y += 10;
    
    // Add options
    doc.setFontSize(12);
    ['option_1', 'option_2', 'option_3', 'option_4', 'option_5'].forEach((optKey, idx) => {{
      if(q[optKey]) {{
        const qid = q.id ?? i;
        const userAns = answers[qid];
        const isCorrect = String(idx+1) === String(q.answer);
        const isUser = userAns && String(idx+1) === String(userAns);
        
        let prefix = '○';
        if(isCorrect) prefix = '✓';
        if(isUser && !isCorrect) prefix = '✗';
        
        doc.text(`  ${{prefix}} ${{q[optKey].replace(/<br>/g, ' ')}}`, margin, y);
        y += 6;
      }}
    }});
    
    // Add explanation
    if(q.solution_text) {{
      y += 5;
      doc.setFontSize(11);
      doc.setTextColor(100);
      doc.text(`Explanation: ${{q.solution_text.replace(/<br>/g, ' ')}}`, margin, y);
      y += 8;
      doc.setTextColor(0);
    }}
    
    y += 10;
  }});
  
  doc.save(`${{QUIZ_TITLE}}_results.pdf`);
}}

// Filter results
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

// Attach listeners
function attachListeners(){{
  el("saveNextBtn").addEventListener("click", ()=> {{ 
    if(current < QUESTIONS.length-1) renderQuestion(current+1); 
  }});
  el("prevBtn").addEventListener("click", ()=> {{ if(current > 0) renderQuestion(current-1); }});
  el("clearBtn").addEventListener("click", ()=> {{ 
    const qid = QUESTIONS[current].id ?? current;
    delete answers[qid];
    delete markedForReview[qid];
    renderQuestion(current);
    highlightPalette();
  }});
  el("markReviewBtn").addEventListener("click", markForReview);
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
    renderQuestion(current);
  }});
  
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

// Firebase functions
function saveResultFirebase(payload) {{
  const ref = db.ref("quiz_results/" + payload.quizId + "/" + payload.deviceId);
  return ref.once("value").then(snap => {{
    if (snap.exists()) return false;
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

function getRankAndPercentile(quizId, myScore, myTime, mySubmittedAt) {{
  return db.ref("attempt_history/" + quizId).once("value").then(snapshot => {{
    const quizData = snapshot.val() || {{}};
    let attempts = [];
    Object.values(quizData).forEach(deviceAttempts => {{
      Object.values(deviceAttempts).forEach(attempt => {{
        attempts.push(attempt);
      }});
    }});
    attempts.sort((a, b) => {{
      if (b.score !== a.score) return b.score - a.score;
      return a.timeTaken - b.timeTaken;
    }});
    const total = attempts.length;
    let rank = total + 1;
    for (let i = 0; i < attempts.length; i++) {{
      if (attempts[i].score === myScore && attempts[i].timeTaken === myTime && attempts[i].submittedAt === mySubmittedAt) {{
        rank = i + 1;
        break;
      }}
    }}
    const below = attempts.filter(a => a.score < myScore || (a.score === myScore && a.timeTaken > myTime)).length;
    const percentile = total ? ((below / total) * 100).toFixed(2) : "0.00";
    return {{ rank, percentile, total }};
  }});
}}

// Load previous attempts
function loadPreviousAttempts(quizId, deviceId) {{
  const box = document.getElementById("previousAttempts");
  box.innerHTML = "";
  box.style.display = "block";
  document.getElementById("results").style.display = "none";
  document.getElementById("attemptReplay").style.display = "none";

  db.ref("attempt_history/" + quizId + "/" + deviceId).once("value").then(snapshot => {{
    const data = snapshot.val();
    if (!data) {{
      box.innerHTML = "<p>No previous attempts found.</p>";
      return;
    }}
    ALL_ATTEMPTS_CACHE = data;
    const attempts = Object.values(data).sort((a, b) => b.submittedAt - a.submittedAt);
    let html = `<div class="card"><h3 style="color:var(--accent)">Previous Attempts</h3>`;
    attempts.forEach((a, i) => {{
      html += `<button class="btn-ghost" onclick="showAttempt('${{a.submittedAt}}')">
        Attempt ${{attempts.length - i}} — Score: ${{a.score}}
      </button>`;
    }});
    html += "</div>";
    box.innerHTML = html;
  }});
}}

// Utility functions
function normalizeMathForQuiz(container) {{
  if (!container) return;
  container.innerHTML = container.innerHTML
    .replace(/(\\S)\\s*\\$\\$(.+?)\\$\\*\\s*(\\S)/g, '$1 \\\\($2\\\\) $3')
    .replace(/\\$\\$(.+?)\\$\\$/g, function(match, math) {{
      if (/^<br>|<div>|<\/div>|<p>|<\/p>/.test(match)) {{
        return match;
      }}
      return '\\\\(' + math + '\\\\)';
    }});
}}

function renderMath(){{
  if (window.MathJax && MathJax.typesetPromise) {{
    MathJax.typesetPromise();
  }}
}}

// Start when DOM loads
window.addEventListener("DOMContentLoaded", init);
</script>
</body>
</html>"""
    
    # Generate QUESTIONS array
    questions_js = "[\n"
    for i, q in enumerate(quiz_data["questions"]):
        q["id"] = str(50000 + i + 1)
        q["quiz_id"] = quiz_data["name"]
        q["correct_score"] = str(quiz_data.get("marks", "3"))
        q["negative_score"] = str(quiz_data.get("negative", "1"))
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

# Bot Handlers (same as before, but with updated parsing)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    update_activity()
    await update.message.reply_text(
        "📚 *Quiz Generator Bot*\n\n"
        "Send me a TXT file with questions. Supported formats:\n\n"
        "1) Numbered format:\n"
        "1. Question\n"
        "a) Option 1\n"
        "b) Option 2\n"
        "Correct option:-a\n"
        "ex: Explanation\n\n"
        "2) Q. format:\n"
        "Q.1 Question\n"
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
        if not (document.mime_type == 'text/plain' or document.file_name.endswith('.txt')):
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

# Rest of the bot handlers remain the same...
# [Keep all the existing async functions: get_quiz_name, get_time, get_time_custom, 
# get_marks, get_marks_custom, get_negative, get_negative_custom, 
# get_creator, cancel, help_command, wake_command, status_command, error_handler]

# ... [Include all the remaining bot handlers exactly as they were] ...

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
                MessageHandler(filters.Document.ALL, handle_document),
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
