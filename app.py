import os
import json
import hmac
import hashlib
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timedelta
import threading
import time
import dotenv

dotenv.load_dotenv()

# Supabase (pip install supabase)
try:
    from supabase import create_client
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None
    print("[SUPABASE]", "Connected" if supabase else "Not configured — using memory")
except Exception as e:
    supabase = None
    print(f"[SUPABASE] Disabled: {e}")

def log_brand_intelligence(event_type, data):
    """Log anonymised events to Supabase for brand intelligence."""
    if not supabase:
        return
    try:
        supabase.table("brand_intelligence").insert({
            "event_type": event_type,
            "data": data,
            "created_at": datetime.now().isoformat()
        }).execute()
    except Exception as e:
        print(f"[SUPABASE] Log failed: {e}")

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
VERIFY_TOKEN        = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN        = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID     = os.getenv("PHONE_NUMBER_ID")
APP_SECRET          = os.getenv("APP_SECRET")
BOOKING_LINK_WEEK1  = os.getenv("BOOKING_LINK_WEEK1")
BOOKING_LINK_WEEK3  = os.getenv("BOOKING_LINK_WEEK3")

# ─── EMAIL CONFIG ─────────────────────────────────────────────────────────────
DERMAT_EMAIL        = os.getenv("DERMAT_EMAIL", "priyankhvaidya@gmail.com")
SENDER_EMAIL        = os.getenv("SENDER_EMAIL", "priyankvaidya09@gmail.com")
SENDER_PASSWORD     = os.getenv("SENDER_PASSWORD", "oanfspibuvskbfet")

API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# ─── IN-MEMORY DB (replace with Supabase in production) ───────────────────────
users = {}
# users[phone] = {
#   name, concern, products, start_date,
#   state, onboarding_complete,
#   pre_consult: { q1, q2, photo_url },
#   consultations: []
# }

# ─── DERMAT MARKETPLACE ───────────────────────────────────────────────────────
dermats = {
    "dr_priya": {
        "id":           "dr_priya",
        "name":         "Dr. Priya Sharma",
        "speciality":   ["acne", "pigmentation"],
        "experience":   "8 years",
        "rating":       4.9,
        "reviews":      127,
        "languages":    "Hindi, English",
        "next_slot":    "Today 4:00 PM",
        "booking_week1": "https://calendly.com/priyankvaidya09/30min",
        "booking_week3": "https://calendly.com/priyankvaidya09/30min",
        "email":         "priyankhvaidya@gmail.com",
        "available":     True,
    },
    "dr_rohan": {
        "id":           "dr_rohan",
        "name":         "Dr. Rohan Mehta",
        "speciality":   ["pigmentation", "dryness"],
        "experience":   "5 years",
        "rating":       4.8,
        "reviews":      89,
        "languages":    "English, Gujarati",
        "next_slot":    "Tomorrow 11:00 AM",
        "booking_week1": "https://calendly.com/priyankvaidya09/30min",
        "booking_week3": "https://calendly.com/priyankvaidya09/30min",
        "email":         "priyankhvaidya@gmail.com",
        "available":     True,
    },
    "dr_sara": {
        "id":           "dr_sara",
        "name":         "Dr. Sara Iyer",
        "speciality":   ["rosacea", "dryness", "acne"],
        "experience":   "11 years",
        "rating":       5.0,
        "reviews":      214,
        "languages":    "English, Tamil",
        "next_slot":    "Today 6:30 PM",
        "booking_week1": "https://calendly.com/priyankvaidya09/30min",
        "booking_week3": "https://calendly.com/priyankvaidya09/30min",
        "email":         "priyankhvaidya@gmail.com",
        "available":     True,
    },
}

def get_dermats_for_concern(concern):
    """Return available dermats who specialise in this concern, best rated first."""
    matched = [
        d for d in dermats.values()
        if concern.lower() in d["speciality"] and d["available"]
    ]
    return sorted(matched, key=lambda x: x["rating"], reverse=True)
 
def get_dermat_by_id(dermat_id):
    return dermats.get(dermat_id)
 
 
CONCERN_TIMELINE = {
    "acne":         "Week 6–10",
    "pigmentation": "Week 10–14",
    "dryness":      "Week 2–4",
    "rosacea":      "Week 8–12",
}
 
# ─── CONVERSATION STATES ──────────────────────────────────────────────────────
STATE_AWAIT_NAME       = "await_name"
STATE_AWAIT_CONCERN    = "await_concern"
STATE_AWAIT_PRODUCTS   = "await_products"
STATE_AWAIT_DATE       = "await_date"
STATE_COMPLETE         = "complete"
STATE_PRECONSULT_Q1    = "preconsult_q1"
STATE_PRECONSULT_Q2    = "preconsult_q2"
STATE_PRECONSULT_PHOTO  = "preconsult_photo"
STATE_AWAIT_DERMAT      = "await_dermat"
STATE_REACTION_Q1       = "reaction_q1"
STATE_REACTION_Q2       = "reaction_q2"
STATE_REACTION_Q3       = "reaction_q3"
STATE_AWAIT_RATING      = "await_rating"
 
 
# ─── SIGNATURE VERIFICATION ───────────────────────────────────────────────────
def verify_signature(payload, signature):
    # Skip verification if APP_SECRET not configured (dev/testing)
    if not APP_SECRET or APP_SECRET == "your_app_secret":
        print("[SECURITY] Skipping signature check — dev mode")
        return True
    if not signature:
        print("[SECURITY] No signature received")
        return True  # Allow through during testing
    try:
        expected = "sha256=" + hmac.new(
            APP_SECRET.encode("utf-8"),
            payload,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        print(f"[SECURITY] Error: {e}")
        return True  # Allow through during testing
 
 
# ─── SEND HELPERS ─────────────────────────────────────────────────────────────
def send(phone, payload):
    payload["messaging_product"] = "whatsapp"
    payload["to"] = phone
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    r = requests.post(API_URL, json=payload, headers=headers)
    print(f"[SEND] {phone} → {r.status_code} {r.text[:200]}")
    return r
 
 
def send_text(phone, text):
    return send(phone, {"type": "text", "text": {"body": text, "preview_url": False}})
 
 
def send_buttons(phone, body, buttons):
    # buttons = [{"id": "btn_id", "title": "Button Text"}, ...]
    return send(phone, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in buttons
                ]
            }
        }
    })
 
 
def send_document(phone, url, filename, caption):
    return send(phone, {
        "type": "document",
        "document": {
            "link": url,
            "filename": filename,
            "caption": caption
        }
    })
 
 
# ─── WEBHOOK VERIFICATION ─────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
 
    # Debug — print what Meta is sending vs what you have
    print(f"[VERIFY] mode={mode}")
    print(f"[VERIFY] token received = '{token}'")
    print(f"[VERIFY] token expected = '{VERIFY_TOKEN}'")
    print(f"[VERIFY] challenge = '{challenge}'")
 
    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("[WEBHOOK] ✅ Verified successfully")
            return challenge, 200
        else:
            print("[WEBHOOK] ❌ Token mismatch")
            return "Forbidden", 403
 
    print("[WEBHOOK] ❌ Missing mode or token")
    return "Bad Request", 400
 
 
# ─── WEBHOOK RECEIVER ─────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, sig):
        print("[SECURITY] Signature mismatch — rejected")
        return "Unauthorized", 401
 
    data = request.get_json()
 
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]
 
        # Handle status updates (ignore)
        if "statuses" in changes:
            return "OK", 200
 
        messages = changes.get("messages", [])
        if not messages:
            return "OK", 200
 
        msg   = messages[0]
        phone = msg["from"]
        mtype = msg["type"]
 
        # Extract text or button reply
        if mtype == "text":
            text = msg["text"]["body"].strip()
            handle_message(phone, text, mtype)
 
        elif mtype == "interactive":
            reply_id    = msg["interactive"]["button_reply"]["id"]
            reply_title = msg["interactive"]["button_reply"]["title"]
            handle_message(phone, reply_id, mtype, reply_title)
 
        elif mtype == "image":
            image_id = msg["image"]["id"]
            print(f"[PHOTO] Received image from {phone}, id={image_id}, state={users.get(phone, {}).get('state')}")
            handle_photo(phone, image_id)
 
        else:
            send_text(phone, "Please send a text message or use the buttons below.")
 
    except (KeyError, IndexError) as e:
        print(f"[ERROR] Parsing webhook: {e}")
 
    return "OK", 200
 
 
# ─── MESSAGE ROUTER ───────────────────────────────────────────────────────────
def handle_message(phone, text, mtype, title=None):
    user = users.get(phone)
 
    # New user
    if not user:
        users[phone] = {
            "state": STATE_AWAIT_NAME,
            "onboarding_complete": False,
            "name": None,
            "concern": None,
            "products": None,
            "start_date": None,
            "pre_consult": {},
            "consultations": [],
            "skin_scores": [],
            "dermat_notes": None,
            "follow_through": [],
            "rating_given": False,
            "reactions": [],
        }
        send_text(
            phone,
            "👋 Hi! Welcome to *SkinTrack*.\n\n"
            "I'm your skin journey assistant. "
            "Let's set you up in 2 minutes.\n\n"
            "What's your name?"
        )
        return
 
    state = user["state"]
 
    # ── Button actions (must come first) ──
    if text == "action_week1":
        send_day5_reminder(phone)
        return
    if text == "action_week3":
        send_day19_reminder(phone)
        return
    if text == "action_passport":
        handle_passport_request(phone)
        return
 
    # Skin score buttons
    if handle_skin_score(phone, text):
        return
 
    # Rating buttons
    if text.startswith("rate_") and user.get("state") != STATE_AWAIT_RATING:
        user["state"] = STATE_AWAIT_RATING
        handle_message(phone, text, mtype, title)
        return
    if text.startswith("dermat_"):
        dermat_id = text.replace("dermat_", "")
        dermat    = get_dermat_by_id(dermat_id)
        if dermat and user:
            user["chosen_dermat"] = dermat_id
            user["state"]         = STATE_COMPLETE
            send_text(
                phone,
                f"Perfect choice! 🙌\n\n"
                f"*{dermat['name']}*\n"
                f"{dermat['experience']} experience · ⭐ {dermat['rating']} ({dermat['reviews']} reviews)\n"
                f"Speaks: {dermat['languages']}\n\n"
                f"Next available: {dermat['next_slot']}\n\n"
                "I'll message you on *Day 5* to book your first check-in. 🌱"
            )
        return
 
    # ── Keyword shortcuts (work from any state) ──
    text_lower = text.lower()
 
    if "passport" in text_lower or "my report" in text_lower:
        handle_passport_request(phone)
        return
 
    if text_lower in ["skip photo", "skip", "no photo"] and user.get("state") == STATE_PRECONSULT_PHOTO:
        complete_preconsult(phone, image_id=None)
        return
 
    if "change dermat" in text_lower or "change doctor" in text_lower or "choose dermat" in text_lower:
        user["state"] = STATE_AWAIT_DERMAT
        send_dermat_list(phone, user.get("concern", "acne"))
        return
 
    if text_lower in ["reaction", "side effect", "problem", "issue", "redness", "breakout"]:
        user["state"] = STATE_REACTION_Q1
        send_buttons(
            phone,
            "I'm sorry to hear that 😔 Let me help you figure this out.\n\n"
            "Which product do you think caused the reaction?",
            [
                {"id": "react_tretinoin", "title": "Tretinoin / Retinol"},
                {"id": "react_acid",      "title": "AHA/BHA/Vitamin C"},
                {"id": "react_other",     "title": "Other / Not sure"},
            ]
        )
        return
 
    if state in [STATE_COMPLETE, STATE_AWAIT_RATING]:
        if any(k in text_lower for k in ["book week 1", "day5", "week 1", "book1", "checkin1"]):
            send_day5_reminder(phone)
            return
    
        if any(k in text_lower for k in ["book week 3", "day19", "week 3", "book3", "checkin3"]):
            send_day19_reminder(phone)
            return

        if text_lower in ["menu", "options", "help", "hi", "hello"]:
            send_buttons(
                phone,
                "Your routine is being tracked. 🌿\n\nWhat would you like to do?",
                [
                    {"id": "action_week1",    "title": "Book Week 1 Check-in"},
                    {"id": "action_passport", "title": "My Skin Passport"},
                    {"id": "action_week3",    "title": "Book Week 3 Check-in"},
                ]
            )
            return

    if "test" in text_lower and "day" in text_lower:
        send_text(phone,
            "Test commands available:\n"
            "• *week 1* — get Week 1 booking link\n"
            "• *week 3* — get Week 3 booking link\n"
            "• *my passport* — get your Skin Passport\n"
            "• *reset* — restart onboarding"
        )
        return
 
    if text_lower == "reset":
        users.pop(phone, None)
        send_text(phone, "Resetting your journey. Send hi to start again.")
        return
 
    # ── Onboarding flow ──
    if state == STATE_AWAIT_NAME:
        user["name"] = title or text
        user["state"] = STATE_AWAIT_CONCERN
        send_buttons(
            phone,
            f"Nice to meet you, {user['name']}! 😊\n\nWhat's your primary skin concern?",
            [
                {"id": "concern_acne",         "title": "Acne"},
                {"id": "concern_pigmentation",  "title": "Pigmentation"},
                {"id": "concern_dryness",       "title": "Dryness"},
            ]
        )
        # Send rosacea separately (max 3 buttons per message)
        # WhatsApp limit: 3 buttons per interactive message
        # For 4 options, send a second message or use list message
        send_buttons(
            phone,
            "Or:",
            [{"id": "concern_rosacea", "title": "Rosacea"}]
        )
 
    elif state == STATE_AWAIT_CONCERN:
        concern_map = {
            "concern_acne":        "acne",
            "concern_pigmentation": "pigmentation",
            "concern_dryness":     "dryness",
            "concern_rosacea":     "rosacea",
        }
        concern = concern_map.get(text, text.lower())
        user["concern"] = concern
        user["state"] = STATE_AWAIT_PRODUCTS
        send_text(
            phone,
            "Got it! 🌿\n\n"
            "What products are you currently using?\n\n"
            "Type them separated by commas.\n"
            "_Example: Tretinoin 0.025%, Niacinamide 10%, SPF 50_"
        )
 
    elif state == STATE_AWAIT_PRODUCTS:
        user["products"] = text
        user["state"] = STATE_AWAIT_DATE
        send_buttons(
            phone,
            "When did you start using these products?",
            [
                {"id": "date_today",   "title": "Just started"},
                {"id": "date_3days",   "title": "3–5 days ago"},
                {"id": "date_week",    "title": "About a week ago"},
            ]
        )
 
    elif state == STATE_AWAIT_DATE:
        date_map = {
            "date_today": 0,
            "date_3days": 4,
            "date_week":  7,
        }
        days_back        = date_map.get(text, 0)
        start_date       = datetime.now() - timedelta(days=days_back)
        user["start_date"]           = start_date
        user["state"]                = STATE_COMPLETE
        user["onboarding_complete"]  = True
 
        concern   = user["concern"]
        timeline  = CONCERN_TIMELINE.get(concern, "Week 6–10")
        name      = user["name"]
 
        send_text(
            phone,
            f"You're all set, {name}! ✅\n\n"
            f"Based on your concern (*{concern.title()}*), "
            f"visible change typically happens between *{timeline}*.\n\n"
            "_Most people feel like nothing's happening in the first week. "
            "That feeling is completely normal._"
        )
 
        # Schedule reminders in background
        schedule_reminders(phone, start_date)
 
        # Log onboarding to Supabase
        log_brand_intelligence("onboarding_complete", {
            "concern":  concern,
            "products": user["products"],
            "phone_hash": str(hash(phone)),  # anonymised
        })
 
        # Show dermat marketplace
        import time as _time
        _time.sleep(1)
        user["state"] = STATE_AWAIT_DERMAT
        send_dermat_list(phone, concern)
 
    # ── Dermat selection ──
    elif state == STATE_AWAIT_DERMAT:
        dermat = None
 
        # Handle number reply (1, 2, 3) for text-based list
        if text.strip().isdigit():
            shortlist = user.get("dermat_shortlist", [])
            idx = int(text.strip()) - 1
            if 0 <= idx < len(shortlist):
                dermat = get_dermat_by_id(shortlist[idx])
        else:
            dermat = get_dermat_by_id(text)
 
        if dermat:
            user["chosen_dermat"] = dermat["id"]
            user["state"]         = STATE_COMPLETE
            send_text(
                phone,
                f"Perfect choice! 🙌\n\n"
                f"*{dermat['name']}*\n"
                f"{dermat['experience']} experience · ⭐ {dermat['rating']} ({dermat['reviews']} reviews)\n"
                f"Speaks: {dermat['languages']}\n"
                f"Next available: {dermat['next_slot']}\n\n"
                f"I'll message you on *Day 5* to book your first check-in with {dermat['name'].split()[0]}. 🌱"
            )
        else:
            send_text(phone, "Please choose from the options above.")
            send_dermat_list(phone, user.get("concern", "acne"))
 
    # ── Reaction hotline ──
    elif state == STATE_REACTION_Q1:
        user["reactions"].append({"product": title or text})
        user["state"] = STATE_REACTION_Q2
        send_buttons(
            phone,
            "How bad is the reaction?",
            [
                {"id": "severity_mild",   "title": "Mild — manageable"},
                {"id": "severity_medium", "title": "Moderate — uncomfortable"},
                {"id": "severity_severe", "title": "Severe — painful/swollen"},
            ]
        )
 
    elif state == STATE_REACTION_Q2:
        severity = title or text
        if user["reactions"]:
            user["reactions"][-1]["severity"] = severity
        user["state"] = STATE_REACTION_Q3
        send_buttons(
            phone,
            "When did it start?",
            [
                {"id": "when_today",  "title": "Today"},
                {"id": "when_few",    "title": "2–3 days ago"},
                {"id": "when_week",   "title": "About a week ago"},
            ]
        )
 
    elif state == STATE_REACTION_Q3:
        when = title or text
        if user["reactions"]:
            user["reactions"][-1]["when"] = when
        user["state"] = STATE_COMPLETE
 
        concern  = user.get("concern", "acne")
        product  = user["reactions"][-1].get("product", "")
        severity = user["reactions"][-1].get("severity", "")
 
        # Log to Supabase for brand intelligence
        log_brand_intelligence("reaction", {
            "concern": concern,
            "product": product,
            "severity": severity,
            "when": when
        })
 
        # Smart response based on severity
        if "severe" in severity.lower():
            send_text(
                phone,
                "⚠️ This sounds serious.\n\n"
                "Please *stop using the product immediately* and "
                "apply plain moisturiser only.\n\n"
                "I'm flagging your dermat right now. "
                "They will contact you within 24 hours.\n\n"
                "_If you experience swelling around eyes or throat, "
                "go to a doctor immediately._"
            )
            # Alert dermat via email
            threading.Thread(
                target=send_reaction_alert,
                args=(phone,),
                daemon=True
            ).start()
 
        elif "moderate" in severity.lower():
            send_text(
                phone,
                "This sounds like your skin adjusting — but let's be careful.\n\n"
                "For the next *3 days*:\n"
                "• Stop the suspected product\n"
                "• Use only gentle cleanser + plain moisturiser\n"
                "• Apply SPF in the morning\n\n"
                "If it doesn't improve in 3 days, type *reaction* again "
                "and I'll flag your dermat.\n\n"
                "_Logging this so your dermat sees it at your next check-in._"
            )
 
        else:
            send_text(
                phone,
                "This sounds like the normal adjustment phase 🌿\n\n"
                "Most people experience this in the first 2–3 weeks. "
                "It usually means the product is working.\n\n"
                "Keep going, but *reduce frequency* — use it every other day "
                "instead of daily for the next week.\n\n"
                "_Your dermat will review this at your next check-in._"
            )
 
    # ── Rating flow ──
    elif state == STATE_AWAIT_RATING:
        rating_map = {
            "rate_1": 1, "rate_2": 2, "rate_3": 3,
            "rate_4": 4, "rate_5": 5
        }
        score = rating_map.get(text)
        if score:
            user["rating_given"] = True
            dermat_id = user.get("chosen_dermat")
            if dermat_id and dermat_id in dermats:
                d = dermats[dermat_id]
                total    = d["rating"] * d["reviews"]
                d["reviews"] += 1
                d["rating"]  = round((total + score) / d["reviews"], 1)
 
            log_brand_intelligence("dermat_rating", {
                "dermat_id": dermat_id,
                "score": score,
                "concern": user.get("concern"),
            })
 
            user["state"] = STATE_COMPLETE
            if score >= 4:
                send_text(
                    phone,
                    f"⭐ Thank you! Glad the consultation was helpful.\n\n"
                    "Know someone struggling with their skin? "
                    "Share SkinTrack with them 🌿"
                )
            else:
                send_text(
                    phone,
                    "Thank you for the honest feedback. "
                    "We'll use this to improve.\n\n"
                    "If you'd like a different dermatologist next time, "
                    "type *change dermat* anytime."
                )
        else:
            send_buttons(
                phone,
                "Please rate your consultation:",
                [
                    {"id": "rate_5", "title": "⭐⭐⭐⭐⭐ Excellent"},
                    {"id": "rate_4", "title": "⭐⭐⭐⭐ Good"},
                    {"id": "rate_3", "title": "⭐⭐⭐ Average"},
                ]
            )
 
    # ── Pre-consult flow ──
    elif state == STATE_PRECONSULT_Q1:
        user["pre_consult"]["q1"] = title or text
        user["state"] = STATE_PRECONSULT_Q2
        send_buttons(
            phone,
            "Any reactions so far — redness, itching, unusual breakouts?",
            [
                {"id": "reaction_none",   "title": "No reactions"},
                {"id": "reaction_redness","title": "Some redness"},
                {"id": "reaction_worse",  "title": "Breakouts got worse"},
            ]
        )
 
    elif state == STATE_PRECONSULT_Q2:
        user["pre_consult"]["q2"] = title or text
        user["state"] = STATE_PRECONSULT_PHOTO
        print(f"[STATE] {phone} → STATE_PRECONSULT_PHOTO — waiting for photo")
        send_text(
            phone,
            "Last step 📸\n\n"
            "Please send one photo of your face in *natural light*, no filter.\n\n"
            "Your dermat will review this before the call.\n\n"
            "_No photo? Type 'skip photo' to continue without one._"
        )
 
    # ── Complete state — handle general messages ──
    elif state == STATE_COMPLETE:
        send_text(
            phone,
            "Your routine is tracking nicely 🌿\n\n"
            "_Type *menu* to see booking options or *passport* to view your Skin Passport._"
        )
 
 
 
 
# ─── PHOTO HANDLER ────────────────────────────────────────────────────────────
# ─── DERMAT LIST SENDER ───────────────────────────────────────────────────────
def send_dermat_list(phone, concern):
    matched = get_dermats_for_concern(concern)
 
    if not matched:
        send_text(phone,
            "We're onboarding more specialists right now. "
            "A dermatologist will be assigned to you shortly. 🙏"
        )
        return
 
    # WhatsApp supports max 3 buttons per message
    # For up to 3 dermats use buttons, more use text list
    if len(matched) <= 3:
        buttons = [
            {
                "id":    f"dermat_{d['id']}",
                "title": d["name"].split("Dr. ")[1][:20],  # max 20 chars
            }
            for d in matched[:3]
        ]
 
        # Build body text with dermat details
        details = ""
        for d in matched[:3]:
            details += (
                "\n*" + d["name"] + "*\n" +
                d["experience"] + " · ⭐ " + str(d["rating"]) +
                " (" + str(d["reviews"]) + " reviews)\n" +
                "Next slot: " + d["next_slot"] + "\n"
            )
 
        send_buttons(
            phone,
            "I've matched you with " + concern.title() + " specialists 🌿\n"
            "Choose your dermatologist:" + details,
            buttons
        )
 
    else:
        # Text-based list for 4+ dermats
        msg = "I've matched you with *" + concern.title() + "* specialists 🌿\n\n"
        for i, d in enumerate(matched, 1):
            msg += (
                "*" + str(i) + ". " + d["name"] + "*\n" +
                "   " + d["experience"] + " · ⭐ " + str(d["rating"]) +
                " (" + str(d["reviews"]) + " reviews)\n" +
                "   Next: " + d["next_slot"] + "\n\n"
            )
        msg += "Reply with the number to choose. Example: *1*"
        send_text(phone, msg)
 
        # Store list so we can map number → dermat
        user = users.get(phone)
        if user:
            user["dermat_shortlist"] = [d["id"] for d in matched]
            user["state"] = STATE_AWAIT_DERMAT
 
 
# ─── REACTION ALERT EMAIL ────────────────────────────────────────────────────
def send_reaction_alert(phone):
    user = users.get(phone)
    if not user:
        return
    name     = user.get("name", "Unknown")
    concern  = user.get("concern", "N/A").title()
    products = user.get("products", "N/A")
    reaction = user.get("reactions", [{}])[-1]
 
    dermat_id = user.get("chosen_dermat")
    dermat    = get_dermat_by_id(dermat_id) if dermat_id else None
    recipient = dermat["email"] if dermat else DERMAT_EMAIL
 
    subject = f"⚠️ URGENT — Severe Reaction Reported — {name}"
    html = f"""
    <html><body style="font-family:Arial;max-width:600px;margin:0 auto;padding:20px;">
    <div style="background:#c0392b;padding:20px;border-radius:10px 10px 0 0;">
        <h2 style="color:white;margin:0;">⚠️ Severe Reaction Alert</h2>
        <p style="color:#fadbd8;margin:6px 0 0;">Immediate attention required</p>
    </div>
    <div style="background:#f9f9f9;padding:24px;border:1px solid #e0e0e0;">
        <p><strong>Patient:</strong> {name}</p>
        <p><strong>Concern:</strong> {concern}</p>
        <p><strong>Products:</strong> {products}</p>
        <p><strong>Reaction product:</strong> {reaction.get('product','N/A')}</p>
        <p><strong>Severity:</strong> {reaction.get('severity','N/A')}</p>
        <p><strong>When started:</strong> {reaction.get('when','N/A')}</p>
        <p><strong>WhatsApp:</strong> +{phone}</p>
        <div style="background:#fadbd8;border-left:4px solid #c0392b;padding:14px;margin-top:16px;">
            <strong>Action required:</strong> Please contact this patient within 24 hours.
        </div>
    </div>
    </body></html>
    """
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = recipient
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient, msg.as_string())
        print(f"[EMAIL] ⚠️ Reaction alert sent to {recipient}")
    except Exception as e:
        print(f"[EMAIL] Reaction alert failed: {e}")
 
 
# ─── EMAIL TO DERMAT ──────────────────────────────────────────────────────────
def send_dermat_email(phone, consult_type="Week 1"):
    user = users.get(phone)
    if not user:
        return
 
    name     = user.get("name", "Unknown")
    concern  = user.get("concern", "N/A").title()
    products = user.get("products", "N/A")
    start    = user.get("start_date")
    start_str = start.strftime("%d %B %Y") if start else "N/A"
    pre      = user.get("pre_consult", {})
    q1       = pre.get("q1", "Not answered")
    q2       = pre.get("q2", "Not answered")
    today    = datetime.now().strftime("%d %B %Y, %I:%M %p")
 
    subject = f"SkinTrack | {consult_type} Check-in — {name} | {concern}"
 
    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; color: #1a1a1a;">
 
        <div style="background: #075E54; padding: 20px 24px; border-radius: 10px 10px 0 0;">
            <h2 style="color: white; margin: 0;">🌿 SkinTrack — Patient Pre-Consult Brief</h2>
            <p style="color: #a8d5c5; margin: 6px 0 0;">{consult_type} Consultation | Generated {today}</p>
        </div>
 
        <div style="background: #f9f9f9; padding: 24px; border: 1px solid #e0e0e0;">
 
            <h3 style="color: #075E54; border-bottom: 2px solid #DCF8C6; padding-bottom: 8px;">👤 Patient Details</h3>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px 0; color: #666; width: 40%;">Name</td><td style="padding: 8px 0; font-weight: bold;">{name}</td></tr>
                <tr><td style="padding: 8px 0; color: #666;">WhatsApp</td><td style="padding: 8px 0;">+{phone}</td></tr>
                <tr><td style="padding: 8px 0; color: #666;">Skin Concern</td><td style="padding: 8px 0; font-weight: bold;">{concern}</td></tr>
                <tr><td style="padding: 8px 0; color: #666;">Journey Started</td><td style="padding: 8px 0;">{start_str}</td></tr>
                <tr><td style="padding: 8px 0; color: #666;">Consultation Type</td><td style="padding: 8px 0;"><span style="background: #DCF8C6; color: #075E54; padding: 3px 10px; border-radius: 20px; font-weight: bold;">{consult_type}</span></td></tr>
            </table>
 
            <h3 style="color: #075E54; border-bottom: 2px solid #DCF8C6; padding-bottom: 8px; margin-top: 24px;">💊 Products In Use</h3>
            <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px;">
                <p style="margin: 0; line-height: 1.8;">{products}</p>
            </div>
 
            <h3 style="color: #075E54; border-bottom: 2px solid #DCF8C6; padding-bottom: 8px; margin-top: 24px;">📋 Pre-Consult Answers</h3>
 
            <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px; margin-bottom: 12px;">
                <p style="margin: 0 0 6px; color: #666; font-size: 13px;">Q: Have they been following their routine?</p>
                <p style="margin: 0; font-weight: bold; font-size: 15px;">→ {q1}</p>
            </div>
 
            <div style="background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px;">
                <p style="margin: 0 0 6px; color: #666; font-size: 13px;">Q: Any reactions — redness, itching, breakouts?</p>
                <p style="margin: 0; font-weight: bold; font-size: 15px;">→ {q2}</p>
            </div>
 
            <h3 style="color: #075E54; border-bottom: 2px solid #DCF8C6; padding-bottom: 8px; margin-top: 24px;">📸 Patient Photo</h3>
            <p style="color: #666;">Patient has submitted a photo via WhatsApp. Please check your WhatsApp Business account to view it before the call.</p>
 
            <div style="background: #fff8e1; border-left: 4px solid #FFC107; padding: 14px; border-radius: 0 8px 8px 0; margin-top: 24px;">
                <p style="margin: 0; font-size: 13px; color: #666;">
                    💡 <strong>Quick Note:</strong> This patient was onboarded via SkinTrack.
                    After your call, reply to this email with your consultation notes
                    and we will send them to the patient on WhatsApp automatically.
                </p>
            </div>
        </div>
 
        <div style="background: #075E54; padding: 14px 24px; border-radius: 0 0 10px 10px; text-align: center;">
            <p style="color: #a8d5c5; margin: 0; font-size: 12px;">SkinTrack — Closing the skincare follow-through gap</p>
        </div>
 
    </body>
    </html>
    """
 
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SENDER_EMAIL

        # Send to chosen dermat if available, else fallback to default
        dermat_id    = user.get("chosen_dermat")
        dermat       = get_dermat_by_id(dermat_id) if dermat_id else None
        recipient    = dermat["email"] if dermat else DERMAT_EMAIL

        msg["To"] = recipient
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, recipient, msg.as_string())
 
        print(f"[EMAIL] ✅ Sent to {DERMAT_EMAIL} for patient {name}")
 
    except Exception as e:
        print(f"[EMAIL] ❌ Failed: {e}")
 
 
def complete_preconsult(phone, image_id=None):
    """Complete pre-consult — called after photo OR skip."""
    user = users.get(phone)
    if not user:
        return
 
    if image_id:
        user["pre_consult"]["photo_id"] = image_id
        print(f"[PHOTO] Saved image_id {image_id} for {phone}")
 
    user["state"] = STATE_COMPLETE
    user["pre_consult"]["submitted_at"] = datetime.now().isoformat()
 
    send_text(
        phone,
        "✅ Pre-consult complete!\n\n"
        "Your dermat will review everything before the call.\n"
        "See you at the consultation. 🙏"
    )
 
    # Fire email to dermat with updated info
    consult_type = "Week 3" if user.get("week3_preconsult") else "Week 1"
    threading.Thread(
        target=send_dermat_email,
        args=(phone, consult_type),
        daemon=True
    ).start()
 
    # Log to Supabase
    log_brand_intelligence("pre_consult_submitted", {
        "concern":      user.get("concern"),
        "products":     user.get("products"),
        "q1":           user.get("pre_consult", {}).get("q1"),
        "q2":           user.get("pre_consult", {}).get("q2"),
        "photo":        "yes" if image_id else "skipped",
        "consult_type": consult_type,
    })
    print(f"[SUPABASE] pre_consult_submitted logged for {phone}")
 
 
def handle_photo(phone, image_id):
    user = users.get(phone)
    if not user:
        return
 
    state = user.get("state")
 
    # Accept photo in preconsult photo state OR complete state (user sent late)
    if state == STATE_PRECONSULT_PHOTO:
        complete_preconsult(phone, image_id)
 
    elif state in [STATE_PRECONSULT_Q1, STATE_PRECONSULT_Q2]:
        # User sent photo early — save it and continue the form
        user["pre_consult"]["photo_id"] = image_id
        send_text(phone, "📸 Photo saved! Let me finish a couple quick questions first.")
 
    else:
        # Photo sent outside pre-consult — still save it to passport
        user["pre_consult"]["photo_id"] = image_id
        send_text(phone, "📸 Got your photo! It's been saved to your Skin Passport.")
        log_brand_intelligence("photo_received_outside_preconsult", {
            "concern": user.get("concern"),
            "state":   state,
        })
 
 
# ─── SKIN PASSPORT ────────────────────────────────────────────────────────────
def handle_passport_request(phone):
    user = users.get(phone)
    if not user or not user.get("onboarding_complete"):
        send_text(phone, "Complete your onboarding first to generate your Skin Passport.")
        return
 
    name     = user.get("name", "User")
    concern  = user.get("concern", "N/A").title()
    products = user.get("products", "N/A")
    start    = user.get("start_date")
    start_str = start.strftime("%d %B %Y") if start else "N/A"
    today    = datetime.now().strftime("%d %B %Y")
 
    pre = user.get("pre_consult", {})
    q1  = pre.get("q1", "Not submitted")
    q2  = pre.get("q2", "Not submitted")
 
    # Skin score trend
    scores     = user.get("skin_scores", [])
    score_str  = " → ".join([
        "↑ Better" if s["feeling"] == "better"
        else ("↓ Worse" if s["feeling"] == "worse" else "→ Same")
        for s in scores
    ]) if scores else "No check-ins yet"
 
    # Dermat notes
    notes      = user.get("dermat_notes")
    notes_str  = notes if notes else "Consultation not completed yet"
 
    # Chosen dermat
    dermat_id  = user.get("chosen_dermat")
    dermat     = get_dermat_by_id(dermat_id) if dermat_id else None
    dr_name    = dermat["name"] if dermat else "Not selected yet"
 
    # Reactions
    reactions  = user.get("reactions", [])
    react_str  = ""
    for r in reactions:
        react_str += f"• {r.get('product','?')} — {r.get('severity','?')} ({r.get('when','?')})\n"
    if not react_str:
        react_str = "None reported\n"
 
    passport_text = (
        f"📄 *SKIN PASSPORT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Name:* {name}\n"
        f"*Generated:* {today}\n\n"
        f"*Concern:* {concern}\n"
        f"*Products:* {products}\n"
        f"*Journey started:* {start_str}\n"
        f"*Dermatologist:* {dr_name}\n\n"
        f"*Pre-Consult Answers:*\n"
        f"• Routine adherence: {q1}\n"
        f"• Reactions reported: {q2}\n\n"
        f"*Skin Score Trend:*\n"
        f"{score_str}\n\n"
        f"*Reactions Logged:*\n"
        f"{react_str}\n"
        f"*Dermat Consultation Notes:*\n"
        f"{notes_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Message 'my passport' anytime to get an updated version._"
    )
 
    # Also log to Supabase
    log_brand_intelligence("passport_requested", {
        "concern":  user.get("concern"),
        "products": user.get("products"),
        "scores":   [s["feeling"] for s in scores],
    })
 
    send_text(phone, passport_text)
 
 
# ─── REMINDER SCHEDULER ───────────────────────────────────────────────────────
def schedule_reminders(phone, start_date):
    reminders = [
        (5,  "day5",     send_day5_reminder),
        (7,  "week1_score",  lambda p: send_skin_score(p, week=1)),
        (10, "day10",    send_day10_message),
        (14, "week2_score",  lambda p: send_skin_score(p, week=2)),
        (19, "day19",    send_day19_reminder),
        (21, "week3_score",  lambda p: send_skin_score(p, week=3)),
        (25, "day25",    send_day25_message),
        (28, "week4_score",  lambda p: send_skin_score(p, week=4)),
    ]
 
    def run_reminder(day, rtype, fn):
        fire_at = start_date + timedelta(days=day)
        now     = datetime.now()
        delay   = (fire_at - now).total_seconds()
 
        if delay > 0:
            print(f"[SCHEDULER] {rtype} for {phone} in {delay:.0f} seconds")
            time.sleep(delay)
 
        print(f"[SCHEDULER] Firing {rtype} for {phone}")
        fn(phone)
 
    for day, rtype, fn in reminders:
        t = threading.Thread(
            target=run_reminder,
            args=(day, rtype, fn),
            daemon=True
        )
        t.start()
 
 
def send_day5_reminder(phone):
    user      = users.get(phone)
    dermat_id = user.get("chosen_dermat") if user else None
    dermat    = get_dermat_by_id(dermat_id) if dermat_id else None
    link      = dermat["booking_week1"] if dermat else BOOKING_LINK_WEEK1
    dr_name   = dermat["name"] if dermat else "your dermatologist"
 
    send_text(
        phone,
        f"📅 *Day 5 — Time to check in!*\n\n"
        "This is usually when people feel like nothing is happening.\n"
        "That feeling is completely normal.\n\n"
        f"👇 Book your free 15-minute Week 1 consultation with *{dr_name}*:\n{link}"
    )
 
    if user:
        user["week3_preconsult"] = False
        # Send pre-consult form right after booking link (2 min delay)
        def trigger_preconsult():
            time.sleep(5)
            send_preconsult_form(phone)
        threading.Thread(target=trigger_preconsult, daemon=True).start()
        threading.Thread(
            target=send_dermat_email,
            args=(phone, "Week 1"),
            daemon=True
        ).start()
        print(f"[EMAIL] Week 1 triggered — {phone} → {dr_name}")
 
 
def send_preconsult_form(phone):
    user = users.get(phone)
    if not user:
        return
    user["state"] = STATE_PRECONSULT_Q1
    send_buttons(
        phone,
        "Your Week 1 check-in is coming up! Quick pre-consult check-in 🙏\n\n"
        "Your dermat will review this before the call.\n\n"
        "Have you been using your products as directed?",
        [
            {"id": "adherence_yes",    "title": "Yes, every day"},
            {"id": "adherence_mostly", "title": "Mostly"},
            {"id": "adherence_no",     "title": "Skipped a few times"},
        ]
    )
 
 
def send_day10_message(phone):
    send_text(
        phone,
        "🌿 *Week 2*\n\n"
        "Nothing looks dramatically different yet.\n\n"
        "That's completely normal — your skin is still adjusting.\n\n"
        "No action needed. Your Week 3 check-in is coming up on Day 19.\n"
        "Stay consistent 💪"
    )
 
 
def send_day19_reminder(phone):
    user      = users.get(phone)
    dermat_id = user.get("chosen_dermat") if user else None
    dermat    = get_dermat_by_id(dermat_id) if dermat_id else None
    link      = dermat["booking_week3"] if dermat else BOOKING_LINK_WEEK3
    dr_name   = dermat["name"] if dermat else "your dermatologist"
 
    send_text(
        phone,
        f"📅 *Day 19 — Most important check-in!*\n\n"
        "By now your dermat can see what's working and what needs adjusting.\n\n"
        f"👇 Book your free Week 3 consultation with *{dr_name}*:\n{link}"
    )
 
    if user:
        user["week3_preconsult"] = True
        def trigger_preconsult_w3():
            time.sleep(120)
            send_preconsult_form(phone)
        threading.Thread(target=trigger_preconsult_w3, daemon=True).start()
        threading.Thread(
            target=send_dermat_email,
            args=(phone, "Week 3"),
            daemon=True
        ).start()
        print(f"[EMAIL] Week 3 triggered — {phone} → {dr_name}")
 
 
def send_day25_message(phone):
    send_text(
        phone,
        "🌿 *Week 4*\n\n"
        "You're past the halfway mark.\n\n"
        "Most people with your concern start seeing early changes "
        "around now.\n\n"
        "Keep going. Your skin is working. 💚"
    )
 
 
# ─── HEALTH CHECK ─────────────────────────────────────────────────────────────
def send_skin_score(phone, week=1):
    user = users.get(phone)
    if not user:
        return
    send_buttons(
        phone,
        f"Week {week} check-in 🌿\n\n"
        "One quick question — how does your skin feel compared to last week?",
        [
            {"id": f"score_better_{week}", "title": "Better 📈"},
            {"id": f"score_same_{week}",   "title": "Same →"},
            {"id": f"score_worse_{week}",  "title": "Worse 📉"},
        ]
    )
 
def handle_skin_score(phone, text):
    user = users.get(phone)
    if not user:
        return False
 
    score_map = {}
    for week in range(1, 9):
        score_map[f"score_better_{week}"] = ("better", week)
        score_map[f"score_same_{week}"]   = ("same", week)
        score_map[f"score_worse_{week}"]  = ("worse", week)
 
    if text not in score_map:
        return False
 
    feeling, week = score_map[text]
    user["skin_scores"].append({
        "week":    week,
        "feeling": feeling,
        "date":    datetime.now().isoformat()
    })
 
    log_brand_intelligence("skin_score", {
        "concern":  user.get("concern"),
        "products": user.get("products"),
        "week":     week,
        "feeling":  feeling,
    })
 
    scores = user["skin_scores"]
    if feeling == "worse" and week >= 2:
        send_text(
            phone,
            f"Week {week} can feel like a low point — "
            "and for most people it is.\n\n"
            "If you've been consistent, this is usually the adjustment phase. "
            "Your dermat will review everything at the next check-in.\n\n"
            "Type *reaction* if something specific is bothering you."
        )
    elif feeling == "better":
        send_text(
            phone,
            f"That's great to hear! 🎉 Week {week} improvement logged.\n\n"
            "Stay consistent — you're on track."
        )
    else:
        send_text(
            phone,
            f"Week {week} noted. Same is okay — "
            "skin changes slowly and consistently.\n\n"
            "Keep going 💪"
        )
 
    # Show trend at week 4+
    if len(scores) >= 3:
        trend = " → ".join([
            "↑" if s["feeling"] == "better"
            else ("↓" if s["feeling"] == "worse" else "→")
            for s in scores[-4:]
        ])
        send_text(
            phone,
            f"Your skin trend: *{trend}*\n\n"
            "_Keep this going._"
        )
 
    return True
 
 
def send_post_consult_follow_through(phone, notes):
    """Parse dermat notes and schedule follow-through messages."""
    user = users.get(phone)
    if not user:
        return
 
    user["dermat_notes"] = notes
    instructions = []
 
    notes_lower = notes.lower()
 
    if "tretinoin" in notes_lower or "retinol" in notes_lower:
        if "alternate" in notes_lower or "every other" in notes_lower:
            instructions.append("🌙 Tonight — *skip* tretinoin. Apply tomorrow night.")
        else:
            instructions.append("🌙 Tonight — apply tretinoin after moisturiser.")
 
    if "spf" in notes_lower or "sunscreen" in notes_lower:
        instructions.append("☀️ Morning — SPF before you leave. Non-negotiable.")
 
    if "vitamin c" in notes_lower and ("stop" in notes_lower or "pause" in notes_lower):
        instructions.append("⏸️ Pause Vitamin C for now — your dermat's advice.")
 
    if "moisturis" in notes_lower:
        instructions.append("💧 Moisturiser morning and night — don't skip.")
 
    if not instructions:
        instructions.append("Follow your updated routine as advised by your dermat.")
 
    user["follow_through"] = instructions
 
    # Send first follow-through immediately
    msg = "*Your updated routine from Dr. " +           (get_dermat_by_id(user.get("chosen_dermat")) or {}).get("name", "your dermat").split()[-1] +           ":*\n\n"
    for inst in instructions:
        msg += inst + "\n"
    msg += "\n_I'll remind you daily for the next 7 days._"
    send_text(phone, msg)
 
    # Schedule daily reminders for 7 days
    def daily_reminders():
        for day in range(1, 8):
            time.sleep(86400)  # 24 hours
            if not users.get(phone):
                break
            reminder = instructions[day % len(instructions)]
            send_text(
                phone,
                f"Day {day} reminder 🌿\n\n{reminder}"
            )
    threading.Thread(target=daily_reminders, daemon=True).start()
 
 
def send_rating_request(phone):
    user = users.get(phone)
    if not user or user.get("rating_given"):
        return
    dermat_id = user.get("chosen_dermat")
    dermat    = get_dermat_by_id(dermat_id)
    dr_name   = dermat["name"] if dermat else "your dermatologist"
    user["state"] = STATE_AWAIT_RATING
    send_buttons(
        phone,
        f"How was your consultation with *{dr_name}*? ⭐",
        [
            {"id": "rate_5", "title": "Excellent ⭐⭐⭐⭐⭐"},
            {"id": "rate_4", "title": "Good ⭐⭐⭐⭐"},
            {"id": "rate_3", "title": "Average ⭐⭐⭐"},
        ]
    )
 
 
# ─── DERMAT PORTAL ────────────────────────────────────────────────────────────
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD", "skintrack2024")
 
PORTAL_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>SkinTrack — Dermat Portal</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, sans-serif; background: #f0f4f0; min-height: 100vh; }
.header { background: #075E54; color: white; padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 20px; font-weight: 600; }
.header p  { font-size: 13px; opacity: 0.7; }
.container { max-width: 960px; margin: 24px auto; padding: 0 16px; }
.grid { display: grid; grid-template-columns: 320px 1fr; gap: 20px; }
.panel { background: white; border-radius: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); overflow: hidden; }
.panel-header { padding: 14px 18px; border-bottom: 1px solid #f0f0f0; font-weight: 600; font-size: 14px; color: #075E54; }
.patient-item { padding: 14px 18px; border-bottom: 1px solid #f8f8f8; cursor: pointer; transition: background .15s; }
.patient-item:hover { background: #f0f9f0; }
.patient-item.active { background: #e8f5e9; border-left: 3px solid #25D366; }
.patient-name { font-weight: 600; font-size: 14px; color: #1a1a1a; }
.patient-meta { font-size: 12px; color: #888; margin-top: 3px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; margin-left: 8px; }
.badge.week1 { background: #e8f5e9; color: #2e7d32; }
.badge.week3 { background: #fff3e0; color: #e65100; }
.detail-panel { padding: 20px; }
.section { margin-bottom: 20px; }
.section-title { font-size: 12px; font-weight: 600; color: #888; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 10px; }
.info-row { display: flex; gap: 8px; margin-bottom: 8px; font-size: 14px; }
.info-label { color: #888; min-width: 120px; }
.info-value { color: #1a1a1a; font-weight: 500; }
.answer-box { background: #f8f8f8; border-radius: 8px; padding: 12px 14px; margin-bottom: 8px; font-size: 14px; }
.answer-q { color: #888; font-size: 12px; margin-bottom: 4px; }
.answer-a { color: #1a1a1a; font-weight: 500; }
textarea { width: 100%; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px; font-size: 14px; font-family: inherit; resize: vertical; outline: none; min-height: 120px; }
textarea:focus { border-color: #25D366; }
.btn { display: inline-flex; align-items: center; gap: 8px; padding: 12px 20px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; border: none; transition: all .15s; }
.btn-primary { background: #25D366; color: white; width: 100%; justify-content: center; }
.btn-primary:hover { background: #1da851; }
.btn-primary:disabled { background: #ccc; cursor: not-allowed; }
.toast { position: fixed; bottom: 24px; right: 24px; background: #075E54; color: white; padding: 12px 20px; border-radius: 8px; font-size: 14px; opacity: 0; transition: opacity .3s; pointer-events: none; }
.toast.show { opacity: 1; }
.empty { text-align: center; padding: 60px 20px; color: #888; }
.no-selection { text-align: center; padding: 80px 20px; color: #ccc; font-size: 14px; }
</style>
</head>
<body>
 
<div class="header">
  <div style="width:36px;height:36px;background:#25D366;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;">🌿</div>
  <div>
    <h1>SkinTrack — Dermat Portal</h1>
    <p>Patient briefs & consultation notes</p>
  </div>
</div>
 
<div class="container">
  <div class="grid">
    <div class="panel">
      <div class="panel-header">Patients <span id="count" style="color:#888;font-weight:400;font-size:12px;"></span></div>
      <div id="patient-list"><div class="empty">No patients yet</div></div>
    </div>
    <div class="panel">
      <div id="detail-panel" class="no-selection">Select a patient to view their brief</div>
    </div>
  </div>
</div>
 
<div class="toast" id="toast"></div>
 
<script>
let patients = [];
let selected = null;
 
async function loadPatients() {
  const res  = await fetch('/api/patients');
  patients   = await res.json();
  document.getElementById('count').textContent = patients.length + ' total';
  const list = document.getElementById('patient-list');
  if (!patients.length) { list.innerHTML = '<div class="empty">No patients yet</div>'; return; }
  list.innerHTML = patients.map((p, i) => `
    <div class="patient-item ${selected === i ? 'active' : ''}" onclick="selectPatient(${i})">
      <div class="patient-name">
        ${p.name || 'Unknown'}
        <span class="badge ${p.week3_preconsult ? 'week3' : 'week1'}">${p.week3_preconsult ? 'Week 3' : 'Week 1'}</span>
      </div>
      <div class="patient-meta">${(p.concern || '').toUpperCase()} · Started ${p.start_date || 'N/A'} · +${p.phone}</div>
    </div>
  `).join('');
}
 
function selectPatient(i) {
  selected = i;
  const p  = patients[i];
  loadPatients();
  const pre = p.pre_consult || {};
  const scores = (p.skin_scores || []).map(s =>
    s.feeling === 'better' ? '↑' : s.feeling === 'worse' ? '↓' : '→'
  ).join(' → ') || 'None yet';
 
  document.getElementById('detail-panel').innerHTML = `
    <div class="detail-panel">
      <div class="section">
        <div class="section-title">Patient Details</div>
        <div class="info-row"><span class="info-label">Name</span><span class="info-value">${p.name || 'N/A'}</span></div>
        <div class="info-row"><span class="info-label">WhatsApp</span><span class="info-value">+${p.phone}</span></div>
        <div class="info-row"><span class="info-label">Concern</span><span class="info-value">${(p.concern || '').toUpperCase()}</span></div>
        <div class="info-row"><span class="info-label">Products</span><span class="info-value">${p.products || 'N/A'}</span></div>
        <div class="info-row"><span class="info-label">Started</span><span class="info-value">${p.start_date || 'N/A'}</span></div>
      </div>
      <div class="section">
        <div class="section-title">Pre-Consult Answers</div>
        <div class="answer-box"><div class="answer-q">Following routine?</div><div class="answer-a">${pre.q1 || 'Not submitted'}</div></div>
        <div class="answer-box"><div class="answer-q">Reactions?</div><div class="answer-a">${pre.q2 || 'Not submitted'}</div></div>
      </div>
      <div class="section">
        <div class="section-title">Skin Score Trend</div>
        <div class="answer-box"><div class="answer-a">${scores}</div></div>
      </div>
      ${(p.reactions||[]).length ? `
      <div class="section">
        <div class="section-title">Reactions Reported</div>
        ${p.reactions.map(r => `<div class="answer-box"><div class="answer-a">Product: ${r.product} · Severity: ${r.severity} · When: ${r.when}</div></div>`).join('')}
      </div>` : ''}
      <div class="section">
        <div class="section-title">Consultation Notes</div>
        <textarea id="notes-input" placeholder="Write your consultation notes here...&#10;&#10;Example:&#10;Continue Tretinoin — reduce to alternate nights&#10;Pause Vitamin C serum&#10;Add SPF 50 every morning">${p.dermat_notes || ''}</textarea>
        <br><br>
        <button class="btn btn-primary" onclick="sendNotes('${p.phone}')">
          💬 Send Notes via WhatsApp
        </button>
      </div>
    </div>
  `;
}
 
async function sendNotes(phone) {
  const notes = document.getElementById('notes-input').value.trim();
  if (!notes) { showToast('Please write consultation notes first'); return; }
  const btn = document.querySelector('.btn-primary');
  btn.disabled = true;
  btn.textContent = 'Sending...';
  const res = await fetch('/api/send-notes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ phone, notes })
  });
  const data = await res.json();
  if (data.success) {
    showToast('✅ Notes sent to patient via WhatsApp!');
    btn.textContent = '✅ Sent!';
    setTimeout(() => { btn.disabled = false; btn.innerHTML = '💬 Send Notes via WhatsApp'; }, 3000);
  } else {
    showToast('❌ Failed to send. Try again.');
    btn.disabled = false;
    btn.innerHTML = '💬 Send Notes via WhatsApp';
  }
}
 
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
 
loadPatients();
setInterval(loadPatients, 30000);
</script>
</body>
</html>
"""
 
@app.route("/portal")
def portal():
    pwd = request.args.get("pwd", "")
    if pwd != PORTAL_PASSWORD:
        return '<div style="font-family:sans-serif;padding:40px;text-align:center;">'                '<h2>SkinTrack Dermat Portal</h2>'                '<p style="color:#888;margin:16px 0">Access restricted</p>'                '<form><input name="pwd" type="password" placeholder="Enter password" '                'style="padding:10px;border:1px solid #ddd;border-radius:6px;font-size:14px;">'                '<button type="submit" style="margin-left:8px;padding:10px 16px;'                'background:#075E54;color:white;border:none;border-radius:6px;cursor:pointer;">'                'Enter</button></form></div>', 401
    return render_template_string(PORTAL_HTML)
 
@app.route("/api/patients")
def api_patients():
    pwd = request.args.get("pwd", "")
    patient_list = []
    for phone, u in users.items():
        if u.get("onboarding_complete"):
            start = u.get("start_date")
            patient_list.append({
                "phone":          phone,
                "name":           u.get("name"),
                "concern":        u.get("concern"),
                "products":       u.get("products"),
                "start_date":     start.strftime("%d %b %Y") if start else None,
                "pre_consult":    u.get("pre_consult", {}),
                "skin_scores":    u.get("skin_scores", []),
                "reactions":      u.get("reactions", []),
                "dermat_notes":   u.get("dermat_notes"),
                "week3_preconsult": u.get("week3_preconsult", False),
                "chosen_dermat":  u.get("chosen_dermat"),
            })
    return jsonify(patient_list)
 
@app.route("/api/send-notes", methods=["POST"])
def api_send_notes():
    data  = request.get_json()
    phone = data.get("phone")
    notes = data.get("notes", "").strip()
    user  = users.get(phone)
    if not user or not notes:
        return jsonify({"success": False, "error": "Invalid request"})
    try:
        # Send notes to patient via WhatsApp
        dermat_id = user.get("chosen_dermat")
        dermat    = get_dermat_by_id(dermat_id)
        dr_name   = dermat["name"] if dermat else "Your dermatologist"
        send_text(
            phone,
            f"📋 *Consultation Notes from {dr_name}*\n\n"
            f"{notes}\n\n"
            "_Your Skin Passport has been updated. "
            "Message 'my passport' anytime to view your full history._"
        )
        # Store notes + trigger follow-through + rating
        send_post_consult_follow_through(phone, notes)
        threading.Thread(
            target=lambda: (time.sleep(3600), send_rating_request(phone)),
            daemon=True
        ).start()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[PORTAL] Send notes error: {e}")
        return jsonify({"success": False, "error": str(e)})
 
 
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "SkinTrack is running 🌿"}), 200
 
 
# ─── RUN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 2000))
    print(f"[START] SkinTrack running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)