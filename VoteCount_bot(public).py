import logging
import re
import requests
import Levenshtein
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from datetime import datetime


# --- CONFIG ---
BOT_TOKEN = 'your_bot_token_here'
OCR_SPACE_API_KEY = 'your_ocr_space_api_key'
GOOGLE_APPS_SCRIPT_URL = 'your_google_apps_script_webhook_url'


# -------- STATES --------
(
    CHOOSE_INPUT_MODE,
    WAIT_FOR_IMAGE,
    WAIT_FOR_TEXT,
    EDIT_MENU,
    BULK_EDIT_ALL,
    EDIT_SINGLE_CANDIDATE,
    ADD_CANDIDATE_NAME,
    REMOVE_CANDIDATE_NAME,
    SELECT_REGION,
    SELECT_DISTRICT,
    CONFIRM_OVERRIDE,
) = range(11)

# -------- CANDIDATES --------
DEFAULT_CANDIDATES = [
    "Tobias", "Chilungo", "Chakwera", "Nankhumwa", "Bandawe", "Banda",
    "Muluzi", "Kaliya", "Mutharika", "Mwenifumbo", "Kabambe", "Chibambo",
    "Swira", "Mbewe", "Chilumpha", "Chipojola", "Dube"
]

REGIONS = ["Northern", "Central", "Southern"]

# -------- USER SESSIONS --------
# Stores session data per user
user_sessions = {}

# -------- LOGGING --------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------- HELPER FUNCTIONS --------

def extract_votes(text):
    logging.info("OCR TEXT:\n" + text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    matched = {}

    for line in lines:
        # Normalize spacing and remove extra symbols
        clean_line = re.sub(r'\s+', ' ', line).strip()

        # Try to extract using pattern: "Name: Vote" OR "Name Vote"
        match = re.match(r"([A-Za-z\.\-\s]+)[:\s]+([\d,]+)", clean_line)
        if match:
            raw_name = match.group(1).strip()
            vote_str = match.group(2).replace(",", "").strip()

            if vote_str.isdigit():
                vote_count = int(vote_str)

                # Fuzzy match name to DEFAULT_CANDIDATES
                best_match = None
                min_distance = float("inf")
                for candidate in DEFAULT_CANDIDATES:
                    dist = Levenshtein.distance(raw_name.lower(), candidate.lower())
                    if dist < min_distance:
                        min_distance = dist
                        best_match = candidate

                if min_distance <= 2:
                    matched[best_match] = vote_count
                else:
                    logging.warning(f"No close match for candidate name: '{raw_name}'")
            else:
                logging.warning(f"Invalid vote number: '{vote_str}' in line: '{line}'")
        else:
            logging.warning(f"Ignored line due to no match: '{line}'")

    logging.info(f"Candidate name matches found: {matched}")
    return matched


async def send_to_google_sheet(data: dict):
    try:
        response = requests.post(GOOGLE_APPS_SCRIPT_URL, json=data)
        response.raise_for_status()
        logger.info(f"Google Sheet response: {response.text}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error sending data to Google Sheet: {e}")
        return {"status": "error", "message": str(e)}

async def ocr_image(image_url: str):
    try:
        payload = {
            "apikey": OCR_SPACE_API_KEY,
            "url": image_url,
            "language": "eng",
            "isOverlayRequired": False,
        }
        response = requests.post("https://api.ocr.space/parse/image", data=payload)
        response.raise_for_status()
        result = response.json()
        if result and result.get("ParsedResults"):
            extracted_text = result["ParsedResults"][0]["ParsedText"]
            logger.info(f"OCR extracted text: {extracted_text}")
            return extracted_text
        else:
            logger.warning(f"OCR failed or returned no results: {result}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error during OCR API call: {e}")
        return None

def build_keyboard(button_rows):
    """
    Create InlineKeyboardMarkup from a list of button rows.
    Each row is a list of (button_text, callback_data) tuples.
    """
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=cb) for text, cb in row] for row in button_rows]
    )


def build_input_mode_keyboard():
    return build_keyboard([
        [("📷 Upload Image", "mode_image"), ("✍️ Paste Text", "mode_text")],
        [("❌ Cancel", "cancel")]
    ])



def build_edit_menu_keyboard():
    return build_keyboard([
        [("✏️ Bulk Edit All Votes", "bulk_edit")],
        [("✏️ Edit Individual Vote", "edit_individual")],
        [("➕ Add Candidate", "add_candidate")],
        [("🗑 Remove Candidate", "remove_candidate")],
        [("✅ Submit Votes", "submit_votes")],
        [("❌ Cancel", "cancel")],
    ])

def build_regions_keyboard():
    rows = [[(region, f"region_{region}")] for region in REGIONS]
    rows.append([("❌ Cancel", "cancel")])
    return build_keyboard(rows)

async def get_submitted_districts():
    """Query the Apps Script to get districts with existing data for override checking"""
    try:
        response = requests.get(GOOGLE_APPS_SCRIPT_URL, params={"action": "get_submitted_districts"})
        response.raise_for_status()
        result = response.json()
        districts = result.get("districts", [])
        logger.info(f"Submitted districts from sheet: {districts}")
        return [d.lower() for d in districts]
    except Exception as e:
        logger.error(f"Error fetching submitted districts: {e}")
        return []

def build_districts_keyboard(region, submitted_districts):
    districts_map = {
        "Northern": ["Chitipa", "Karonga", "Likoma", "Mzimba", "Nkhata Bay", "Rumphi"],
        "Central": ["Dedza", "Dowa", "Kasungu", "Lilongwe", "Mchinji", "Nkhotakota", "Ntcheu", "Ntchisi", "Salima"],
        "Southern": ["Balaka", "Blantyre", "Chikwawa", "Chiradzulu", "Machinga", "Mangochi", "Mulanje", "Mwanza", "Nsanje", "Thyolo", "Phalombe", "Zomba", "Neno"],
    }
    districts = districts_map.get(region, [])
    keyboard = []
    for dist in districts:
        text = dist
        if dist.lower() in submitted_districts:
            text += " ✅"
        keyboard.append([(text, f"district_{dist}")])
    keyboard.append([("↩️ Back to Regions", "back_to_regions")])
    keyboard.append([("❌ Cancel", "cancel")])
    return build_keyboard(keyboard)
# -------- HANDLERS --------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "candidates": {c: 0 for c in DEFAULT_CANDIDATES},
        "region": None,
        "district": None,
        "votes_confirmed": False,
    }
    await update.message.reply_text(
        "Welcome to the Vote Count Bot!\nChoose input mode:",
        reply_markup=build_input_mode_keyboard(),
    )
    return CHOOSE_INPUT_MODE

async def choose_input_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "mode_image":
        await query.edit_message_text("Please send a photo of the vote count sheet.")
        return WAIT_FOR_IMAGE
    elif data == "mode_text":
        await query.edit_message_text("Please paste the text containing vote counts.")
        return WAIT_FOR_TEXT
    elif data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END
    else:
        await query.edit_message_text("Please choose a valid option.")
        return CHOOSE_INPUT_MODE

async def receive_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return WAIT_FOR_IMAGE
    photo_file = await update.message.photo[-1].get_file()
    photo_url = photo_file.file_path
    await update.message.reply_text("Processing image, please wait...")
    text = await ocr_image(photo_url)
    if not text:
        await update.message.reply_text("OCR failed to extract text. Please try again or send text directly.")
        return WAIT_FOR_IMAGE
    votes = extract_votes(text)
    if not votes:
        await update.message.reply_text("Could not find votes in the text. Please enter votes manually.")
        return WAIT_FOR_TEXT
    user_sessions[user_id]["candidates"] = votes
    await update.message.reply_text(
        f"Parsed votes:\n" + "\n".join(f"{k}: {v}" for k,v in votes.items()),
        reply_markup=build_edit_menu_keyboard(),
    )
    return EDIT_MENU

async def receive_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    votes = extract_votes(text)
    if not votes:
        await update.message.reply_text("Could not parse votes from text. Please try again.")
        return WAIT_FOR_TEXT
    user_sessions[user_id]["candidates"] = votes
    await update.message.reply_text(
        f"Parsed votes:\n" + "\n".join(f"{k}: {v}" for k,v in votes.items()),
        reply_markup=build_edit_menu_keyboard(),
    )
    return EDIT_MENU

# --- EDITING FLOW ---

async def edit_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    action = query.data

    if action == "bulk_edit":
        await query.edit_message_text(
            "Send all candidates and votes in the format:\nCandidate: Votes\nOne per line.\nExample:\nChakwera: 12345\nMutharika: 67890"
        )
        return BULK_EDIT_ALL

    elif action == "edit_individual":
        keyboard = []
        for c in user_sessions[user_id]["candidates"]:
            keyboard.append([(c, f"edit_{c}")])
        keyboard.append([("↩️ Back to Edit Menu", "back_edit_menu")])
        keyboard.append([("❌ Cancel", "cancel")])
        await query.edit_message_text(
            "Select a candidate to edit:", reply_markup=build_keyboard(keyboard)
        )
        return EDIT_SINGLE_CANDIDATE

    elif action == "add_candidate":
        await query.edit_message_text("Send the new candidate's name:")
        return ADD_CANDIDATE_NAME

    elif action == "remove_candidate":
        keyboard = []
        for c in user_sessions[user_id]["candidates"]:
            keyboard.append([(c, f"remove_{c}")])
        keyboard.append([("↩️ Back to Edit Menu", "back_edit_menu")])
        keyboard.append([("❌ Cancel", "cancel")])
        await query.edit_message_text(
            "Select a candidate to remove:", reply_markup=build_keyboard(keyboard)
        )
        return REMOVE_CANDIDATE_NAME

    elif action == "submit_votes":
        # Go to region selection next
        await query.edit_message_text(
            "Choose the region for this vote count:", reply_markup=build_regions_keyboard()
        )
        return SELECT_REGION

    elif action == "back_edit_menu":
        await query.edit_message_text(
            f"Current votes:\n" + "\n".join(f"{k}: {v}" for k, v in user_sessions[user_id]["candidates"].items()),
            reply_markup=build_edit_menu_keyboard(),
        )
        return EDIT_MENU

    elif action == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END

    else:
        await query.edit_message_text("Please select a valid option.")
        return EDIT_MENU

async def bulk_edit_all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    new_votes = {}
    for line in text.splitlines():
        if ':' not in line:
            continue
        name, val = line.split(':', 1)
        name, val = name.strip(), val.strip()
        if not val.isdigit():
            continue
        new_votes[name] = int(val)
    if not new_votes:
        await update.message.reply_text("No valid candidate votes found. Please try again.")
        return BULK_EDIT_ALL
    user_sessions[user_id]["candidates"].update(new_votes)
    await update.message.reply_text(
        f"Updated votes:\n" + "\n".join(f"{k}: {v}" for k, v in user_sessions[user_id]["candidates"].items()),
        reply_markup=build_edit_menu_keyboard(),
    )
    return EDIT_MENU

async def edit_single_candidate_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data.startswith("edit_"):
        candidate = data.replace("edit_", "")
        user_sessions[user_id]["candidate_to_edit"] = candidate
        await query.edit_message_text(f"Send new vote count for {candidate}:")
        return EDIT_SINGLE_CANDIDATE
    elif data == "back_edit_menu":
        return await edit_menu_handler(update, context)
    elif data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END
    else:
        await query.edit_message_text("Please select a valid candidate or option.")
        return EDIT_SINGLE_CANDIDATE

async def receive_single_candidate_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    candidate = user_sessions[user_id].get("candidate_to_edit")
    if not candidate:
        await update.message.reply_text("No candidate selected. Returning to edit menu.")
        return EDIT_MENU
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Please send a valid integer vote count.")
        return EDIT_SINGLE_CANDIDATE
    user_sessions[user_id]["candidates"][candidate] = int(text)
    user_sessions[user_id].pop("candidate_to_edit", None)
    await update.message.reply_text(
        f"Updated {candidate} to {text} votes.",
        reply_markup=build_edit_menu_keyboard(),
    )
    return EDIT_MENU

async def add_candidate_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    new_name = update.message.text.strip()
    if not new_name or new_name in user_sessions[user_id]["candidates"]:
        await update.message.reply_text("Invalid or existing candidate name. Try again.")
        return ADD_CANDIDATE_NAME
    user_sessions[user_id]["candidates"][new_name] = 0
    await update.message.reply_text(
        f"Added candidate '{new_name}'.\nYou can now edit their votes.",
        reply_markup=build_edit_menu_keyboard(),
    )
    return EDIT_MENU

async def remove_candidate_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data.startswith("remove_"):
        candidate = data.replace("remove_", "")
        if candidate in user_sessions[user_id]["candidates"]:
            del user_sessions[user_id]["candidates"][candidate]
            await query.edit_message_text(
                f"Candidate '{candidate}' removed.",
                reply_markup=build_edit_menu_keyboard(),
            )
            return EDIT_MENU
        else:
            await query.edit_message_text("Candidate not found. Returning to edit menu.", reply_markup=build_edit_menu_keyboard())
            return EDIT_MENU
    elif data == "back_edit_menu":
        return await edit_menu_handler(update, context)
    elif data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END
    else:
        await query.edit_message_text("Please select a valid candidate or option.", reply_markup=build_edit_menu_keyboard())
        return REMOVE_CANDIDATE_NAME

# --- REGION & DISTRICT SELECTION ---

async def select_region_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data.startswith("region_"):
        region = query.data.replace("region_", "")
        user_sessions[user_id]["region"] = region
        submitted_districts = await get_submitted_districts()
        await query.edit_message_text(
            f"Region '{region}' selected.\nNow choose a district:",
            reply_markup=build_districts_keyboard(region, submitted_districts),
        )
        return SELECT_DISTRICT
    elif query.data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END
    else:
        await query.edit_message_text("Please select a region.", reply_markup=build_regions_keyboard())
        return SELECT_REGION

async def select_district_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if query.data.startswith("district_"):
        district = query.data.replace("district_", "")
        user_sessions[user_id]["district"] = district
        # Check if district data exists for override
        submitted_districts = await get_submitted_districts()
        if district.lower() in submitted_districts:
            await query.edit_message_text(
                f"Data already exists for district '{district}'. Override?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Yes, override", callback_data="override_yes")],
                        [InlineKeyboardButton("↩️ No, go back", callback_data="override_no")],
                    ]
                ),
            )
            return CONFIRM_OVERRIDE
        else:
            await query.edit_message_text(
                f"District '{district}' selected. Submitting data..."
            )
            return await submit_data(update, context)
    elif query.data == "back_to_regions":
        await query.edit_message_text(
            "Select the region for this vote count:", reply_markup=build_regions_keyboard()
        )
        return SELECT_REGION
    elif query.data == "cancel":
        user_sessions.pop(user_id, None)
        await query.edit_message_text("Operation cancelled. Use /start to begin again.")
        return ConversationHandler.END
    else:
        await query.edit_message_text(
            "Please select a district.",
            reply_markup=build_districts_keyboard(user_sessions[user_id]["region"], await get_submitted_districts()),
        )
        return SELECT_DISTRICT

async def confirm_override_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "override_yes":
        await query.edit_message_text("Overriding previous data. Submitting now...")
        return await submit_data(update, context)
    elif query.data == "override_no":
        # Go back to district selection
        submitted_districts = await get_submitted_districts()
        await query.edit_message_text(
            "Choose another district:",
            reply_markup=build_districts_keyboard(user_sessions[user_id]["region"], submitted_districts),
        )
        return SELECT_DISTRICT
    else:
        await query.edit_message_text("Please choose an option.")
        return CONFIRM_OVERRIDE

def escape_markdown_v2(text):
    """
    Escape characters for Telegram MarkdownV2 formatting.
    """
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def format_vote_results(votes: dict) -> str:
    lines = []
    # Sort candidates alphabetically or keep original order if you want
    for candidate in sorted(votes.keys()):
        line = f"{candidate}: {votes[candidate]:,}"
        lines.append(escape_markdown_v2(line))
    return "\n".join(lines)


from telegram.constants import ParseMode

async def submit_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    data = user_sessions.get(user_id)
    if not data:
        await update.callback_query.edit_message_text(
            "⚠️ Session expired. Use /start to begin again.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    votes = data.get("candidates", {})
    region = data.get("region")
    district = data.get("district")

    if not (region and district and votes):
        await update.callback_query.edit_message_text(
            "❌ Incomplete data. Please restart with /start.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

       
    user = update.callback_query.from_user
    sender = " ".join(filter(None, [user.first_name, user.last_name])) or user.username or str(user.id)


    payload = {
        "region": region,
        "district": district,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sender": sender,
        "votes": votes,
    }

    await update.callback_query.edit_message_text(
        "⏳ Submitting data, please wait...",
        
    )

    result = await send_to_google_sheet(payload)
    if result.get("success"):
        formatted_votes = format_vote_results(votes)
        await update.callback_query.edit_message_text(
            f"✅ *Results Submitted For {district}*\n\n📃 *Parsed Vote Results:*\n\n{formatted_votes}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.callback_query.edit_message_text(
            f"❌ Failed to submit data: {result.get('message', 'Unknown error')}\nPlease try again.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    user_sessions.pop(user_id, None)
    return ConversationHandler.END


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions.pop(user_id, None)
    await update.message.reply_text("Operation cancelled. Use /start to begin again. 🙏")
    return ConversationHandler.END


async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I didn't understand that. Please use /start to begin.")
    return ConversationHandler.END

# -------- MAIN --------

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_INPUT_MODE: [CallbackQueryHandler(choose_input_mode_handler)],
            WAIT_FOR_IMAGE: [MessageHandler(filters.PHOTO, receive_photo_handler)],
            WAIT_FOR_TEXT: [MessageHandler(filters.TEXT & (~filters.COMMAND), receive_text_handler)],
            EDIT_MENU: [CallbackQueryHandler(edit_menu_handler)],
            BULK_EDIT_ALL: [MessageHandler(filters.TEXT & (~filters.COMMAND), bulk_edit_all_handler)],
            EDIT_SINGLE_CANDIDATE: [
                CallbackQueryHandler(edit_single_candidate_handler),
                MessageHandler(filters.TEXT & (~filters.COMMAND), receive_single_candidate_vote),
            ],
            ADD_CANDIDATE_NAME: [MessageHandler(filters.TEXT & (~filters.COMMAND), add_candidate_name_handler)],
            REMOVE_CANDIDATE_NAME: [CallbackQueryHandler(remove_candidate_name_handler)],
            SELECT_REGION: [CallbackQueryHandler(select_region_handler)],
            SELECT_DISTRICT: [CallbackQueryHandler(select_district_handler)],
            CONFIRM_OVERRIDE: [CallbackQueryHandler(confirm_override_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler), MessageHandler(filters.COMMAND, fallback_handler)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)

    logger.info("Bot started.")
    application.run_polling()

if __name__ == "__main__":
    main()
