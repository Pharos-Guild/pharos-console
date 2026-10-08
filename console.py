cat << 'EOF' > $HOME/assistant/console.py
import json
import os
import subprocess
import sys
import sqlite3
import time
import queue
import threading
from collections import deque
import requests
from PIL import Image, ImageOps

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import Static, Input, Button
from textual import work

HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, "assistant")
LEDGER_DIR = os.path.join(BASE_DIR, "ledgers")
VOICE_DIR = os.path.join(BASE_DIR, "voice")
DATA_DIR = os.path.join(BASE_DIR, "data")

WORLD_PATH = os.path.join(LEDGER_DIR, "world_ledger.json")
STANCE_PATH = os.path.join(LEDGER_DIR, "stance_ledger.json")
GEO_DB = os.path.join(DATA_DIR, "geonames.db")
NOTES_PATH = os.path.join(DATA_DIR, "notes_archive.json")

def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def speak_audio(text):
    clean_text = text.replace('"', '').replace("'", "").replace("*", "").strip()
    if not clean_text:
        return
    cmd = (
        "piper",
        "-m",
        "en_GB-alba-medium",
        "-p"
    )
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True, cwd=VOICE_DIR)
        p.communicate(input=clean_text)
    except Exception:
        pass

def get_telemetry():
    total_ram = 0
    avail_ram = 0
    if os.path.exists("/proc/meminfo"):
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    k, v, *u = line.split()
                    total_ram = int(v) // 1024
                elif line.startswith("MemAvailable:"):
                    k, v, *u = line.split()
                    avail_ram = int(v) // 1024

    used_ram = total_ram - avail_ram

    load = "0.0"
    if os.path.exists("/proc/loadavg"):
        with open("/proc/loadavg", "r") as f:
            load = f.read().split().pop(0)

    pct = 70
    cap_path = "/sys/class/power_supply/battery/capacity"
    if os.path.exists(cap_path):
        try:
            with open(cap_path, "r") as f:
                pct = int(f.read().strip())
        except Exception:
            pass

    temp = 32.0
    temp_path = "/sys/class/power_supply/battery/temp"
    if os.path.exists(temp_path):
        try:
            with open(temp_path, "r") as f:
                raw_temp = float(f.read().strip())
                temp = raw_temp / 10.0 if raw_temp > 100 else raw_temp
        except Exception:
            pass

    return used_ram, avail_ram, total_ram, load, pct, round(temp, 1)

def extract_core_topic(user_text):
    clean = user_text.lower().replace("?", "").replace("!", "").replace(",", "").replace(".", "").replace("'", "")
    fluff_phrases = (
        "what does the ", "what do you know about ", "what is the ", "what are the ",
        "what is ", "what are ", "what movies has ", "what movies did ", "what films has ",
        "what parks are in ", "name more parks in ", "can you name more parks in ",
        "what medicines are taken for ", "what medications are for ", "what is used for ",
        "tell me about ", "tell me what ", "who is ", "who was ", "explain the ",
        "explain ", "can you name ", "can you tell me about ", "can you tell me "
    )
    for f in fluff_phrases:
        if clean.startswith(f):
            clean = clean.replace(f, "", 1)
            break

    for suffix in (" tell us about identity", " been in", " in new mexico", " nm", " new mexico"):
        if clean.endswith(suffix):
            prefix, sep, tail = clean.rpartition(suffix)
            clean = prefix

    return clean.strip()

def query_geo(target):
    if not os.path.exists(GEO_DB) or not target:
        return None

    clean = target.replace(",", " ").replace(".", " ")
    words = clean.split()
    search_terms = list()
    search_terms.append(target)
    if len(words) > 1:
        w1 = words.pop(0)
        w2 = words.pop(0)
        search_terms.append(f"{w1} {w2}")
        search_terms.append(w1)

    try:
        conn = sqlite3.connect(GEO_DB)
        cur = conn.cursor()
        row = None
        for term in search_terms:
            sql = (
                "SELECT name, state, lat, lon, elev FROM places "
                "WHERE name LIKE ? "
                "ORDER BY "
                "  CASE WHEN LOWER(name) = LOWER(?) THEN 0 "
                "       WHEN LOWER(name) LIKE LOWER(?) || '%' THEN 1 "
                "       ELSE 2 END, "
                "  LENGTH(name) ASC "
                "LIMIT 1"
            )
            cur.execute(sql, (f"%{term}%", term, term))
            row = cur.fetchone()
            if row:
                break
        conn.close()
        if row:
            p_name, p_state, p_lat, p_lon, p_elev = row
            return f"GEONAMES: {p_name}, {p_state} | Lat: {p_lat}, Lon: {p_lon} | Elev: {p_elev}m"
        return None
    except Exception:
        return None

def strip_tags(html_str):
    cleaned = html_str.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", "'")
    for tag_pair in (("<style", "</style>"), ("<script", "</script>"), ("<table", "</table>"), ("<TABLE", "</TABLE>")):
        open_t, close_t = tag_pair
        while open_t in cleaned:
            prefix, sep, rest = cleaned.partition(open_t)
            content, end_sep, tail = rest.partition(close_t)
            cleaned = prefix + " " + tail

    in_tag = False
    chars = list()
    for c in cleaned:
        if c == "<":
            in_tag = True
        elif c == ">":
            in_tag = False
        elif not in_tag:
            chars.append(c)
    return "".join(chars)

def query_kiwix(target):
    if not target:
        return None

    clean = target.strip()
    words = clean.split()

    search_candidates = list()
    search_candidates.append(clean)
    if len(words) > 1:
        t_copy = list(words)
        w1 = t_copy.pop(0)
        w2 = t_copy.pop(0)
        search_candidates.append(f"{w1} {w2}")
        search_candidates.append(w1)

    for cand in search_candidates:
        search_url = f"http://127.0.0.1:8081/search?pattern={requests.utils.quote(cand)}"
        try:
            r = requests.get(search_url, timeout=8)
            if r.status_code == 200:
                prefix, sep, tail = r.text.partition('href="/content/')
                if tail:
                    target_url, quote_sep, rest = tail.partition('"')
                    article_url = f"http://127.0.0.1:8081/content/{target_url}"
                    art = requests.get(article_url, timeout=8)
                    if art.status_code == 200:
                        plain = strip_tags(art.text)
                        words_plain = plain.split()
                        lead_words = " ".join(w for i, w in enumerate(words_plain) if i < 150)
                        raw_book, book_slash, book_rest = target_url.partition('/')
                        art_name = raw_book.replace("_en_all", "").replace("_top1m", "").replace("_maxi", "").replace("_nopic", "").upper()
                        return f"KIWIX // {art_name}:\n{lead_words}"
        except Exception:
            pass
    return None

def query_notes(target):
    notes = load_json(NOTES_PATH)
    if not notes:
        return None
    low_target = target.lower()
    for date_key, content in reversed(notes.items()):
        if low_target in content.lower():
            clipped = "".join(c for i, c in enumerate(content) if i < 250)
            return f"ARCHIVED RECORD ({date_key}):\n{clipped}"
    return None

def trigger_ocr():
    dcim_dir = os.path.expanduser("~/storage/dcim")
    camera_dir = os.path.join(dcim_dir, "Camera")
    screen_dir = os.path.join(dcim_dir, "Screenshots")

    image_paths = list()
    for folder in (camera_dir, screen_dir):
        if os.path.exists(folder):
            for f in os.listdir(folder):
                if f.lower().endswith((".jpg", ".png", ".jpeg")):
                    image_paths.append(os.path.join(folder, f))

    if not image_paths:
        return "No recent images found in Camera or Screenshots."

    latest_img = max(image_paths, key=os.path.getmtime)

    corrected_path = os.path.join(BASE_DIR, "data", "temp_ocr.jpg")
    try:
        with Image.open(latest_img) as img:
            transposed = ImageOps.exif_transpose(img)
            transposed.save(corrected_path, "JPEG")
        target_file = corrected_path
    except Exception:
        target_file = latest_img

    try:
        txt = subprocess.check_output(
            ("tesseract", target_file, "stdout", "-l", "eng", "--psm", "6"),
            text=True,
            timeout=10
        )
        return txt.strip()
    except Exception as e:
        return f"OCR Error: {e}"

def extract_sentence(buf):
    terminators = (
        ". ", ".\n", "! ", "!\n", "? ", "?\n",
        '". ', '".\n', "'. ", "'.\n"
    )
    earliest_pos = -1
    chosen_term = ""
    for t in terminators:
        pos = buf.find(t)
        if pos != -1:
            if earliest_pos == -1 or pos < earliest_pos:
                prefix, s, tail = buf.partition(t)
                low_prefix = prefix.lower().strip()
                if low_prefix.endswith("e.g") or low_prefix.endswith("i.e") or low_prefix.endswith("dr") or low_prefix.endswith("mr") or low_prefix.endswith("vs"):
                    continue
                earliest_pos = pos
                chosen_term = t

    if earliest_pos != -1 and chosen_term:
        prefix, sep, tail = buf.partition(chosen_term)
        return prefix + sep.strip(), tail

    # Clause Sluice: if buffer exceeds 8 words and has a natural comma break, dispatch immediately!
    words = buf.split()
    if len(words) >= 8:
        for clause_sep in (", ", "; ", " — "):
            if clause_sep in buf:
                prefix, sep, tail = buf.partition(clause_sep)
                if len(prefix.split()) >= 5:
                    return prefix.strip() + ",", tail

    return None, buf

class PharosConsole(App):
    CSS = """
    Screen {
        background: #000000;
        color: #cc5500;
    }

    #telemetry-crown {
        dock: top;
        height: auto;
        border: solid #ab855b;
        background: #000000;
        padding: 0 1;
    }

    #chat-stream {
        height: 1fr;
        overflow-y: scroll;
        padding: 0 1;
        background: #000000;
    }

    .data-card {
        border: solid #ab855b;
        background: #000000;
        color: #ab855b;
        margin: 1 0;
        padding: 0 1;
    }

    .voice-card {
        border: solid #ab855b;
        background: #000000;
        color: #cc5500;
        margin: 1 0;
        padding: 0 1;
    }

    .metric-card {
        color: #7d6b56;
        text-style: italic;
        margin: 0 0 1 0;
        padding: 0 1;
    }

    .user-card {
        color: #ab855b;
        margin: 1 0;
        text-style: bold;
    }

    #bottom-dock {
        dock: bottom;
        height: auto;
        background: #000000;
    }

    #tool-tray {
        height: 3;
        layout: horizontal;
        background: #000000;
    }

    .tool-btn {
        width: 1fr;
        height: 3;
        background: #ab855b;
        color: #000000;
        border: none;
        text-style: bold;
        margin: 0 1;
    }

    .tool-btn:hover {
        background: #cc5500;
        color: #000000;
    }

    .end-btn {
        background: #e84b3d;
        color: #000000;
        border: none;
        text-style: bold;
        margin: 0 1;
    }

    .end-btn:hover {
        background: #ff6600;
        color: #000000;
    }

    #input-bay {
        height: 3;
        border: solid #ab855b;
        background: #000000;
        color: #cc5500;
    }
    """

    BINDINGS = (("ctrl+c", "quit_console", "Quit"),)

    def compose(self) -> ComposeResult:
        yield Static(id="telemetry-crown")
        with VerticalScroll(id="chat-stream"):
            yield Static("CONSOLE ACTIVE // EXPEDITION DECK READY", classes="data-card")
        with Container(id="bottom-dock"):
            with Horizontal(id="tool-tray"):
                yield Button("SCAN", id="btn-scan", classes="tool-btn")
                yield Button("NAV", id="btn-nav", classes="tool-btn")
                yield Button("NOTE", id="btn-note", classes="tool-btn")
                yield Button("END", id="btn-end", classes="end-btn")
            yield Input(placeholder="Ask, command, or nav...", id="input-bay")

    def on_mount(self) -> None:
        self.world_ledger = load_json(WORLD_PATH)
        self.stance_ledger = load_json(STANCE_PATH)
        self.buffer = deque(maxlen=4)
        self.last_scan_buffer = ""
        self.speech_queue = queue.Queue()

        self.audio_worker = threading.Thread(target=self.audio_worker_loop, daemon=True)
        self.audio_worker.start()

        self.system_prompt = (
            "You are Lyric, a sovereign field companion and navigator in an air-gapped terminal. "
            "You speak with crisp Scottish enunciation (Alba), wry wit, and surgical factual discipline.\n"
            "WORLD: " + json.dumps(self.world_ledger) + "\n"
            "STANCE: " + json.dumps(self.stance_ledger) + "\n"
            "OPERATIONAL RULES // FIELD NAVIGATION:\n"
            "- CRITICAL CADENCE: The opening sentence MUST be brief, direct, and under 10 words. Deliver facts in punchy clauses. Never use long run-on compound sentences with multiple parentheses.\n"
            "- When FIELD TELEMETRY is provided, treat coordinates, names, and medical/historical excerpts as verified ground truth.\n"
            "- Synthesize telemetry with your broader intellect. Never loop or repeat titles or terms.\n"
            "- Never fabricate lawsuits, fictional books, or false coordinates. Admitting an absence of telemetry is an act of high operational integrity.\n"
            "- For philosophy, biology, and physics, synthesize concepts across non-linear dynamics and physical cycles (fire, tides, hydraulics). Respond in three to five sentences."
        )
        self.update_telemetry()
        self.set_interval(3.0, self.update_telemetry)

    def audio_worker_loop(self) -> None:
        while True:
            text = self.speech_queue.get()
            if text is None:
                break
            speak_audio(text)
            self.speech_queue.task_done()

    def update_telemetry(self) -> None:
        used_r, avail_r, tot_r, load, pct, temp = get_telemetry()
        status = "HOT" if temp >= 42.0 else "NORMAL"
        t_text = (
            f"PHAROS CONSOLE // GALAXY-A26 // AIR-GAPPED\n"
            f"CPU: {load}  |  TEMP: {temp}°C ({status})  |  BATT: {pct}%\n"
            f"RAM: {used_r}M / {tot_r}M (FREE: {avail_r}M)  |  LEDGERS: MOUNTED"
        )
        self.query_one("#telemetry-crown", Static).update(t_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-end":
            self.action_quit_console()
        elif btn_id == "btn-scan":
            self.process_scan()
        elif btn_id == "btn-nav":
            self.query_one("#input-bay", Input).value = "where is "
            self.query_one("#input-bay", Input).focus()
        elif btn_id == "btn-note":
            self.query_one("#input-bay", Input).value = "save to notes"
            self.query_one("#input-bay", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        user_text = event.value.strip()
        if not user_text:
            return
        event.input.value = ""
        self.process_dialogue(user_text)

    @work(thread=True)
    def process_scan(self) -> None:
        stream = self.query_one("#chat-stream", VerticalScroll)
        self.app.call_from_thread(stream.mount, Static("TESSERACT: SCANNING RECENT CAPTURE...", classes="data-card"))
        self.app.call_from_thread(stream.scroll_end, animate=False)
        ocr_text = trigger_ocr()
        self.last_scan_buffer = ocr_text
        clipped = "".join(c for i, c in enumerate(ocr_text) if i < 300)
        self.app.call_from_thread(stream.mount, Static(f"OPTICAL DATA:\n{clipped}", classes="data-card"))
        self.app.call_from_thread(stream.scroll_end, animate=False)
        self.process_dialogue(f"Inspect this scanned text: {clipped}")

    @work(thread=True)
    def process_dialogue(self, user_text: str) -> None:
        stream = self.query_one("#chat-stream", VerticalScroll)
        self.app.call_from_thread(stream.mount, Static(f"> {user_text}", classes="user-card"))
        self.app.call_from_thread(stream.scroll_end, animate=False)

        low = user_text.lower()

        # 1. Deliberate manual note archive
        if any(w in low for w in ("save to notes", "log this", "file this", "save receipt")):
            if self.last_scan_buffer:
                timestamp = time.strftime("%Y-%m-%d %H:%M")
                notes = load_json(NOTES_PATH)
                notes.update({timestamp: self.last_scan_buffer})
                save_json(NOTES_PATH, notes)
                self.app.call_from_thread(stream.mount, Static(f"SPIKED TO NOTES ARCHIVE // {timestamp}", classes="data-card"))
                self.app.call_from_thread(stream.scroll_end, animate=False)
                user_text = "Confirm that this record has been permanently committed to our notes archive."

        # 2. Instant keyword extraction
        topic = extract_core_topic(user_text)

        # 3. Parallel Sinks
        archive_cards = list()

        if any(w in low for w in ("receipt", "bill", "note", "spent")):
            note_match = query_notes(topic if topic else user_text)
            if note_match:
                archive_cards.append(note_match)
                self.app.call_from_thread(stream.mount, Static(note_match, classes="data-card"))
                self.app.call_from_thread(stream.scroll_end, animate=False)

        if any(w in low for w in ("where", "nav", "park", "city", "county", "mount", "lake", "location")):
            geo_match = query_geo(topic if topic else user_text)
            if geo_match:
                archive_cards.append(geo_match)
                self.app.call_from_thread(stream.mount, Static(geo_match, classes="data-card"))
                self.app.call_from_thread(stream.scroll_end, animate=False)

        kiwix_match = query_kiwix(topic if topic else user_text)
        if kiwix_match:
            archive_cards.append(kiwix_match)
            self.app.call_from_thread(stream.mount, Static(kiwix_match, classes="data-card"))
            self.app.call_from_thread(stream.scroll_end, animate=False)

        # 4. Formulate Augmented Context
        if archive_cards:
            telemetry_block = "\n\n".join(archive_cards)
            augmented_input = (
                f"FIELD TELEMETRY:\n{telemetry_block}\n\n"
                f"INQUIRY: Integrate the verified telemetry above as ground truth, and synthesize naturally with your broader knowledge: {user_text}"
            )
        else:
            augmented_input = user_text

        self.buffer.append({"role": "user", "content": augmented_input})
        messages_tuple = ({"role": "system", "content": self.system_prompt},) + tuple(self.buffer)

        # Mount live streaming voice card
        voice_card = Static("...", classes="voice-card")
        self.app.call_from_thread(stream.mount, voice_card)
        self.app.call_from_thread(stream.scroll_end, animate=False)

        payload = {
            "model": "qwen",
            "messages": messages_tuple,
            "temperature": 0.5,
            "max_tokens": 140,
            "presence_penalty": 0.5,
            "frequency_penalty": 0.6,
            "stream": True
        }

        t_start = time.time()
        t_first = None
        token_count = 0
        accumulated = list()
        sentence_buffer = ""
        stage_one_sent = False
        sentence_one_text = ""

        try:
            res = requests.post("http://127.0.0.1:8080/v1/chat/completions", json=payload, stream=True, timeout=60)
            if res.status_code == 200:
                for raw_line in res.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    if "data: " in raw_line:
                        prefix, sep, data_str = raw_line.partition("data: ")
                        clean_data = data_str.strip()
                        if "DONE" in clean_data and len(clean_data) < 15:
                            break
                        try:
                            chunk = json.loads(clean_data)
                            choices = chunk.get("choices", ())
                            first_c = next(iter(choices), {})
                            delta = first_c.get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                if t_first is None:
                                    t_first = time.time()
                                token_count += 1
                                accumulated.append(token)

                                current_stream = "".join(accumulated)
                                self.app.call_from_thread(voice_card.update, current_stream)
                                self.app.call_from_thread(stream.scroll_end, animate=False)

                                # Stage 1: The very moment the opening clause or sentence finishes, fire!
                                if not stage_one_sent:
                                    sentence_buffer += token
                                    ready_sentence, sentence_buffer = extract_sentence(sentence_buffer)
                                    if ready_sentence:
                                        sentence_one_text = ready_sentence
                                        self.speech_queue.put(ready_sentence)
                                        stage_one_sent = True

                        except Exception:
                            pass

                t_end = time.time()
                reply = "".join(accumulated).strip()

                # Stage 2: Queue all remaining thoughts in one smooth, continuous stream
                if stage_one_sent and sentence_one_text:
                    prefix, sep, remainder = reply.partition(sentence_one_text)
                    clean_remainder = remainder.strip()
                    if clean_remainder:
                        self.speech_queue.put(clean_remainder)
                elif not stage_one_sent:
                    if reply:
                        self.speech_queue.put(reply)

                # Telemetry instrumentation
                gen_time = (t_end - t_first) if (t_first and t_end > t_first) else (t_end - t_start)
                tps = (token_count / gen_time) if gen_time > 0 else 0.0
                total_duration = t_end - t_start

                metric_str = f"TELEMETRY // {token_count} tokens / {round(tps, 1)} tok/s / {round(total_duration, 1)}s"
                metric_card = Static(metric_str, classes="metric-card")
                self.app.call_from_thread(stream.mount, metric_card)
                self.app.call_from_thread(stream.scroll_end, animate=False)

                is_poisoned = any(phrase in reply.lower() for phrase in (
                    "fictional concept",
                    "as an ai",
                    "i am sorry"
                ))

                if not is_poisoned:
                    self.buffer.append({"role": "assistant", "content": reply})
                else:
                    self.buffer.clear()

            else:
                err_msg = f"Server Error: {res.status_code}"
                self.app.call_from_thread(stream.mount, Static(err_msg, classes="data-card"))
        except Exception as e:
            err_msg = f"Offline Fault: {e}"
            self.app.call_from_thread(stream.mount, Static(err_msg, classes="data-card"))

    def action_quit_console(self) -> None:
        self.speech_queue.put(None)
        self.exit()

if __name__ == "__main__":
    app = PharosConsole()
    app.run()
EOF
